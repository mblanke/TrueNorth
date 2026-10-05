"""ARC² Course Studio: the web side of the ARC² authoring engine.

The engine (``tools/arc2/``, the seven ``arc2-*`` agents and ``/arc2``) runs on the
host through ``tools/arc2/runner.py``; this router never imports it. It:

* reads runs from the runs directory (``manifest.json`` and the files the agents wrote),
* queues work for the runner (``_queue/``): **start** a course from a request, or
  **resume** a run with ``accept`` or feedback at the outline and preview gates,
* reads the runner's job records (``_jobs/``) to show progress.

Nothing here publishes, imports into the course library or edits ``manifest.json``; the
engine's own gate operations do that, driven by ``/arc2``. Off unless
``ARC2_STUDIO_ENABLED`` is set. See docs/arc2-course-studio.md (slice D, first version).

Ownership: a run belongs to the tenant recorded as ``tenant_id`` in its Studio metadata
(``_studio/<slug>.json``). Every route resolves the slug through ``_owned_run`` first, so
another tenant's run, or a run with no recorded owner (started from Claude Code, or made
before ownership was recorded), is a 404 and nothing is queued for it. Admins are tenant
scoped too, as in ``app/tenancy.py``. ``tools/arc2/assign_owner.py`` assigns unowned runs.
"""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..arc2_studio_schemas import RunDetail, RunFile, RunList
from ..auth import CurrentUser
from ..rbac import Permission, require_permission
from ..tenancy import tenant_uuid


def enabled() -> bool:
    return os.getenv("ARC2_STUDIO_ENABLED", "").lower() in {"1", "true", "yes"}


def _require_enabled() -> None:
    if not enabled():
        raise HTTPException(404, "Not Found")


router = APIRouter(prefix="/arc2", tags=["ARC² Course Studio"], dependencies=[Depends(_require_enabled)])
author = require_permission(Permission.COURSE_AUTHOR)

SLUG_RE = re.compile(r"^arc2-[a-z0-9-]{1,60}$")
# Request and feedback text goes on /arc2's argument line after the caller's own slug. A
# --slug or --resume in it would point the engine at another run, so it is refused here
# and again by the runner (tools/arc2/runner.py RUN_FLAG_RE).
RUN_FLAG_RE = re.compile(r"(?i)(?:^|\s)--(?:slug|resume)\b")
STAGES = [
    ("content-architect", "Content Architect", "01-blueprint"),
    ("code-generator", "Code Generator", "02-content"),
    ("range-engineer", "Range Engineer", "03-range"),
    ("artifact-creator", "Artifact Creator", "04-artifacts"),
    ("sensor-gateway", "Sensor Gateway", "05-sensor"),
    ("qa-tester", "QA Tester", "06-qa"),
    ("package-builder", "Package Builder", "07-bundle"),
]
TEXT_SUFFIXES = {".html", ".json", ".yaml", ".yml", ".md", ".csv", ".txt", ".xml", ".js"}
MAX_FILE_BYTES = 512 * 1024


def runs_dir() -> Path:
    default = Path(__file__).resolve().parents[4] / "build" / "arc2"
    return Path(os.getenv("ARC2_RUNS_DIR", str(default)))


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _contained(path: Path) -> bool:
    """True when ``path`` resolves inside the runs-root entry it is addressed through.

    Run directories are written by the engine, so a symlink in one (or a run directory
    that is itself a symlink) must not make this API read another run's files. Every
    read of a file under the runs root goes through this check.
    """
    root = runs_dir()
    try:
        top = root / path.relative_to(root).parts[0]
    except (ValueError, IndexError):
        return False
    real_top = top.resolve()
    if real_top != root.resolve() / top.name:
        return False  # the run directory itself is a link
    real = path.resolve()
    return real == real_top or real_top in real.parents


def _read_json(path: Path) -> dict | None:
    if not _contained(path):
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _run_path(slug: str) -> Path:
    if not SLUG_RE.match(slug):
        raise HTTPException(404, "No such run")
    return runs_dir() / slug


def _meta(slug: str) -> dict:
    return _read_json(runs_dir() / "_studio" / f"{slug}.json") or {}


def _is_owner(slug: str, user: CurrentUser) -> bool:
    """True only when the run's recorded tenant is the caller's; an unowned run has no owner."""
    owner = _meta(slug).get("tenant_id")
    return bool(owner) and str(owner).lower() == str(tenant_uuid(user))


def _owned_run(slug: str, user: CurrentUser) -> Path:
    """The run directory for ``slug`` if the caller's tenant owns it, else 404 (never 403)."""
    run = _run_path(slug)
    if not _is_owner(slug, user) or run.is_symlink():
        raise HTTPException(404, "No such run")
    return run


def _chat(slug: str) -> list[dict]:
    path = runs_dir() / "_studio" / f"{slug}.chat.jsonl"
    out = []
    try:
        for line in path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def _append_chat(slug: str, entry: dict) -> None:
    studio = runs_dir() / "_studio"
    studio.mkdir(parents=True, exist_ok=True)
    with (studio / f"{slug}.chat.jsonl").open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _jobs(slug: str | None = None) -> list[dict]:
    """Queued jobs (state 'queued') and the runner's records, oldest first."""
    root = runs_dir()
    jobs = []
    for path in sorted((root / "_queue").glob("*.json")) if (root / "_queue").is_dir() else []:
        job = _read_json(path)
        if job:
            jobs.append({**job, "state": "queued"})
    for path in (root / "_jobs").glob("*.json") if (root / "_jobs").is_dir() else []:
        job = _read_json(path)
        if job:
            jobs.append(job)
    if slug:
        jobs = [j for j in jobs if j.get("slug") == slug]
    return sorted(jobs, key=lambda j: j.get("created_at") or "")


def _active_job(slug: str) -> dict | None:
    return next((j for j in reversed(_jobs(slug)) if j.get("state") in ("queued", "running")), None)


def _enqueue(slug: str, action: str, text: str, user: CurrentUser) -> dict:
    queue = runs_dir() / "_queue"
    queue.mkdir(parents=True, exist_ok=True)
    job = {
        "id": secrets.token_hex(8), "action": action, "slug": slug, "text": text,
        "created_at": _now(), "requested_by": user.email or user.id, "tenant_id": str(tenant_uuid(user)),
    }
    tmp = queue / f".{job['id']}.tmp"
    tmp.write_text(json.dumps(job))
    tmp.replace(queue / f"{job['created_at'].replace(':', '')}-{job['id']}.json")
    return job


def _stage_view(manifest: dict | None) -> list[dict]:
    stages = (manifest or {}).get("stages") or {}
    return [
        {"key": key, "name": name, "state": (stages.get(key) or {}).get("state", "pending"),
         "stop_reason": (stages.get(key) or {}).get("stop_reason")}
        for key, name, _ in STAGES
    ]


def _phase(manifest: dict | None, job: dict | None) -> tuple[str, str]:
    """(phase key, words) for where the run stands."""
    if job and job.get("state") == "queued":
        return "queued", "Waiting for the runner"
    if job and job.get("state") == "running":
        agent = job.get("current_agent")
        return "running", f"{agent} is working" if agent else "Running"
    if not manifest:
        return "new", "Not started"
    gates = manifest.get("gates") or {}
    stages = manifest.get("stages") or {}
    qa = manifest.get("qa") or {}
    if any((s or {}).get("state") == "failed" for s in stages.values()):
        return "stopped", "Stopped: needs a person"
    if qa.get("result") == "human_takeover":
        return "takeover", "Stopped after 3 QA failures: needs a person"
    if (gates.get("outline") or {}).get("state") == "pending":
        return "outline", "Outline ready for your review"
    if (gates.get("preview") or {}).get("state") == "pending" and all(
        (stages.get(k) or {}).get("state") == "done" for k, *_ in STAGES[:5]
    ):
        return "preview", "Preview ready for your review"
    if (stages.get("package-builder") or {}).get("state") == "done":
        return "packaged", "Package candidate · not published"
    return "paused", "Paused between stages"


def _summary(slug: str) -> dict:
    run = _run_path(slug)
    manifest = _read_json(run / "manifest.json")
    meta = _meta(slug)
    jobs = _jobs(slug)
    job = _active_job(slug) or (jobs[-1] if jobs else None)
    if not manifest and job and job.get("state") == "failed":
        phase, words = "failed", "Could not start"
    else:
        phase, words = _phase(manifest, job)
    course = (manifest or {}).get("course") or {}
    actions = (manifest or {}).get("human_actions") or []
    open_actions = [a for a in actions if a.get("status") == "open"]
    return {
        "slug": slug,
        "name": meta.get("name") or course.get("title") or slug,
        "title": course.get("title"),
        "code": course.get("code"),
        "request": meta.get("request") or _safe_text(run / "request.txt"),
        "phase": phase,
        "phase_text": words,
        "stages": _stage_view(manifest),
        "gates": {g: {k: v for k, v in ((manifest or {}).get("gates") or {}).get(g, {}).items()
                      if k in ("state", "ts", "accepted_sha256", "rework_count")}
                  for g in ("outline", "preview")},
        "qa": {k: ((manifest or {}).get("qa") or {}).get(k) for k in ("result", "cycle", "rework_stage", "checked_at")},
        "actions_open": len(open_actions),
        "actions_blocking": sum(1 for a in open_actions if a.get("blocks_promotion")),
        "job": job and {k: job.get(k) for k in ("id", "action", "state", "current_agent", "error",
                                                 "created_at", "started_at", "finished_at")},
        "updated_at": max(filter(None, [meta.get("created_at"), job and (job.get("finished_at") or job.get("created_at")),
                                        _mtime(run / "manifest.json")]), default=None),
    }


def _mtime(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        return None


def _safe_text(path: Path, limit: int = 4000) -> str | None:
    if not _contained(path):
        return None
    try:
        return path.read_text()[:limit]
    except OSError:
        return None


def _safe_yaml(path: Path):
    if not _contained(path):
        return None
    try:
        return yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return None


def _messages(slug: str, manifest: dict | None) -> list[dict]:
    """The pipeline conversation: what people sent, and what each runner job reported."""
    msgs = [{"role": "user", "text": c.get("text", ""), "ts": c.get("ts")} for c in _chat(slug)]
    for job in _jobs(slug):
        if job.get("state") in ("done", "failed") and (job.get("result") or job.get("error")):
            msgs.append({"role": "pipeline", "text": job.get("result") or job.get("error"),
                         "ts": job.get("finished_at"), "error": job.get("state") == "failed"})
    if not msgs and manifest:  # a run started from Claude Code, before the Studio existed
        req = (manifest.get("request") or {})
        msgs.append({"role": "user", "text": _safe_text(runs_dir() / slug / "request.txt") or str(req), "ts": (manifest.get("provenance") or {}).get("created_at")})
        for gate in ("outline", "preview"):
            g = (manifest.get("gates") or {}).get(gate) or {}
            if g.get("state") == "accepted":
                msgs.append({"role": "user", "text": f"{gate.capitalize()} accepted.", "ts": g.get("ts")})
    return sorted(msgs, key=lambda m: m.get("ts") or "")


def _detail(slug: str) -> dict:
    run = _run_path(slug)
    if not run.is_dir() and not _jobs(slug) and not _meta(slug):
        raise HTTPException(404, "No such run")
    manifest = _read_json(run / "manifest.json") or {}
    out = _summary(slug)
    out["messages"] = _messages(slug, manifest)
    out["outline"] = _safe_yaml(run / "01-blueprint" / "outline.yaml")
    out["objectives"] = manifest.get("objectives") or []
    course_yaml = next(iter(sorted((run / "02-content").glob("*.yaml"))), None) if (run / "02-content").is_dir() else None
    course = _safe_yaml(course_yaml) if course_yaml else None
    out["modules"] = [
        {"title": m.get("title"), "quiz": (m.get("quiz") or {}).get("questions") or []}
        for m in ((course or {}).get("modules") or [])
    ]
    out["pages"] = sorted(str(p.relative_to(run)) for p in (run / "02-content").glob("mod_*/content/*.html")) if (run / "02-content").is_dir() else []
    scenario = (_safe_yaml(run / "05-sensor" / "scenario.yaml") or {}).get("scenario") or {}
    timeline = (_safe_yaml(run / "03-range" / "timeline.yaml") or {}).get("scenario") or {}
    out["lab"] = {
        "range": scenario.get("range_template") or timeline.get("range_template"),
        "injects": [{k: i.get(k) for k in ("id", "t_offset_min", "objective_id", "critical", "author_required", "description")}
                    for i in (timeline.get("injects") or [])],
        "noise_floor": [{k: n.get(k) for k in ("id", "description")} for n in (timeline.get("noise_floor") or [])],
    }
    out["findings"] = ((manifest.get("qa") or {}).get("findings")) or []
    out["human_actions"] = manifest.get("human_actions") or []
    out["files"] = sorted(
        ({"path": f.get("path"), "stage": f.get("stage"), "kind": f.get("kind")} for f in manifest.get("files") or []),
        key=lambda f: f["path"] or "",
    )
    out["package_ready"] = (run / "07-bundle" / "cmi5" / "cmi5.xml").is_file()
    return out


# ── routes ──────────────────────────────────────────────────────────────


class NewRun(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    request: str = Field(..., min_length=3, max_length=4000)


class Reply(BaseModel):
    action: str = Field(..., pattern=r"^(accept|feedback)$")
    text: str | None = Field(default=None, max_length=4000)


def slug_for(name: str) -> str:
    tokens = [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t][:4] or ["course"]
    base = ("arc2-" + "-".join(tokens))[:60].rstrip("-")
    slug, n = base, 2
    root = runs_dir()
    taken = {p.name for p in root.iterdir()} if root.is_dir() else set()
    taken |= {j.get("slug") for j in _jobs()}
    taken |= {p.stem for p in (root / "_studio").glob("*.json")} if (root / "_studio").is_dir() else set()
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def _refuse_run_flags(text: str) -> None:
    if RUN_FLAG_RE.search(text):
        raise HTTPException(422, "The text may not contain --slug or --resume.")


def _claim_slug(name: str, meta: dict) -> str:
    """Pick a free slug and write its metadata in one exclusive create.

    Slugs are one namespace on disk across tenants. ``slug_for`` alone is check-then-write:
    two concurrent sends with the same name could pick the same slug and the second would
    overwrite the first run's metadata, owner included. ``O_EXCL`` makes the claim atomic;
    on a lost race, pick again.
    """
    studio = runs_dir() / "_studio"
    studio.mkdir(parents=True, exist_ok=True)
    for _ in range(20):
        slug = slug_for(name)
        try:
            fd = os.open(studio / f"{slug}.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(meta))
        return slug
    raise HTTPException(503, "Could not allocate a name for the run; try again.")


@router.get("/runs", response_model=RunList)
def list_runs(user: CurrentUser = Depends(author)):
    root = runs_dir()
    slugs = {p.name for p in root.iterdir() if p.is_dir() and SLUG_RE.match(p.name)} if root.is_dir() else set()
    slugs |= {j["slug"] for j in _jobs() if SLUG_RE.match(str(j.get("slug") or ""))}
    slugs |= {p.stem for p in (root / "_studio").glob("*.json") if SLUG_RE.match(p.stem)} if (root / "_studio").is_dir() else set()
    runs = [_summary(s) for s in slugs if _is_owner(s, user)]
    return {"runs": sorted(runs, key=lambda r: r.get("updated_at") or "", reverse=True),
            "runner_seen": _runner_seen()}


def _runner_seen() -> str | None:
    """When the runner last touched a job, so the page can say if nobody is listening."""
    jobs_dir = runs_dir() / "_jobs"
    times = [p.stat().st_mtime for p in jobs_dir.glob("*.json")] if jobs_dir.is_dir() else []
    return datetime.fromtimestamp(max(times), UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if times else None


@router.get("/runs/{slug}", response_model=RunDetail)
def get_run(slug: str, user: CurrentUser = Depends(author)):
    _owned_run(slug, user)
    return _detail(slug)


@router.post("/runs", status_code=201, response_model=RunDetail)
def create_run(body: NewRun, user: CurrentUser = Depends(author)):
    """Send: create a project and queue stage 1. The run stops at the outline for review."""
    request = " ".join(body.request.split())
    _refuse_run_flags(request)
    slug = _claim_slug(body.name, {
        "name": body.name.strip(), "request": request, "created_by": user.email or user.id,
        "tenant_id": str(tenant_uuid(user)), "created_at": _now(),
    })
    _append_chat(slug, {"text": request, "ts": _now(), "by": user.email or user.id})
    _enqueue(slug, "start", request, user)
    return _detail(slug)


@router.post("/runs/{slug}/retry", response_model=RunDetail)
def retry(slug: str, user: CurrentUser = Depends(author)):
    """Queue the last job again when it failed (for example the runner could not sign in)."""
    _owned_run(slug, user)
    if _active_job(slug):
        raise HTTPException(409, "ARC² is still working on this run.")
    jobs = _jobs(slug)
    last = jobs[-1] if jobs else None
    if not last or last.get("state") != "failed":
        raise HTTPException(409, "Nothing failed, so there is nothing to retry.")
    _append_chat(slug, {"text": "Try again.", "ts": _now(), "by": user.email or user.id})
    _enqueue(slug, last["action"], last["text"], user)
    return _detail(slug)


@router.post("/runs/{slug}/reply", response_model=RunDetail)
def reply(slug: str, body: Reply, user: CurrentUser = Depends(author)):
    """Accept the pending review, or send feedback; the runner resumes /arc2 with it."""
    _owned_run(slug, user)
    if _active_job(slug):
        raise HTTPException(409, "ARC² is still working on this run. Wait for it to stop at a review.")
    manifest = _read_json(runs_dir() / slug / "manifest.json")
    phase, _ = _phase(manifest, None)
    if body.action == "accept":
        if phase not in ("outline", "preview", "stopped", "takeover", "paused"):
            raise HTTPException(409, "There is nothing to accept yet.")
        text = "accept"
        shown = {"outline": "Outline accepted.", "preview": "Approved: build the package."}.get(phase, "Continue.")
    else:
        text = " ".join((body.text or "").split())
        if not text:
            raise HTTPException(422, "Describe what should change.")
        if text.lower() == "accept":
            raise HTTPException(422, "Use Accept to accept.")
        _refuse_run_flags(text)
        shown = text
    _append_chat(slug, {"text": shown, "ts": _now(), "by": user.email or user.id})
    _enqueue(slug, "resume", text, user)
    return _detail(slug)


@router.get("/runs/{slug}/file", response_model=RunFile)
def get_file(slug: str, path: str = Query(..., max_length=300), user: CurrentUser = Depends(author)):
    run = _owned_run(slug, user).resolve()
    target = (run / path).resolve()
    if run not in target.parents or target.suffix not in TEXT_SUFFIXES or not target.is_file():
        raise HTTPException(404, "No such file")
    if target.stat().st_size > MAX_FILE_BYTES:
        raise HTTPException(413, "File too large to show")
    rel = target.relative_to(run)
    return {"path": str(rel), "text": target.read_text(errors="replace"),
            "instructor_only": "instructor" in rel.parts}


@router.get("/runs/{slug}/package.zip")
def package_zip(slug: str, user: CurrentUser = Depends(author)):
    run = _owned_run(slug, user).resolve()
    root = (run / "07-bundle" / "cmi5").resolve()
    if run not in root.parents or not (root / "cmi5.xml").is_file():
        raise HTTPException(404, "No package yet")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(root.rglob("*")):
            if f.is_file() and root in f.resolve().parents:
                z.write(f, f.relative_to(root))
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{slug}-cmi5.zip"'})
