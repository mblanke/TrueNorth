#!/usr/bin/env python3
"""Load everything under content/ into a running TrueNorth API, so every page has data.

A fresh `make dev` database is empty: nothing loads content at start-up. This puts in,
through the API's own endpoints (so the same validation applies):

  1. the curriculum: QSP spine, NICE/NIST CSF crosswalk, programme catalogue, all
     course files, and the generated learning paths;
  2. the golden-image (VM ISO) catalogue;
  3. range templates (content/ranges/*/template.yaml);
  4. scenarios (content/scenarios/*/scenario.yaml);
  5. detection rules (content/detections/*.yaml, Sigma);
  6. demo people, and two demo ranges and exercises so the operational pages are not
     empty. Dev provisions with the mock backend, so no hypervisor is touched.

Every step is idempotent: importers upsert, and create-style steps skip anything that
already exists by name. Re-running is safe.

content/inject-packs/ and content/datasets/ are not loaded: nothing in the API or the
worker reads them yet (the worker reads a scenario's `inject_packs` list and discards
it), so there is nowhere for them to appear.

Standard library only, and Python 3.9 compatible, so it runs on a stock macOS python3:

    python3 scripts/load_content.py                       # dev: http://127.0.0.1:8081
    python3 scripts/load_content.py --api URL --token T   # a real deployment
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]
CONTENT = ROOT / "content"


class Api:
    def __init__(self, base: str, token: str | None) -> None:
        self.base = base.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    def _send(self, method: str, path: str, body: bytes | None, ctype: str | None):
        headers = dict(self.headers)
        if ctype:
            headers["Content-Type"] = ctype
        req = request.Request(self.base + path, data=body, method=method, headers=headers)
        try:
            with request.urlopen(req, timeout=120) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            return exc.code, detail

    def get(self, path: str):
        return self._send("GET", path, None, None)

    def post_json(self, path: str, payload: dict | None = None):
        body = json.dumps(payload).encode() if payload is not None else None
        return self._send("POST", path, body, "application/json" if body else None)

    def post_files(self, path: str, files: dict[str, Path]):
        boundary = uuid.uuid4().hex
        parts = []
        for field, fp in files.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
                f'filename="{fp.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
                + fp.read_bytes()
                + b"\r\n"
            )
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        return self._send("POST", path, body, f"multipart/form-data; boundary={boundary}")


class Report:
    def __init__(self) -> None:
        self.failures = 0

    def line(self, status: int | str, what: str, detail: object = "") -> None:
        ok = isinstance(status, int) and 200 <= status < 300
        mark = "ok  " if ok else ("skip" if status == "skip" else "FAIL")
        if mark == "FAIL":
            self.failures += 1
        suffix = f"  {detail}" if (detail and mark == "FAIL") else ""
        print(f"  {mark} {what}{suffix}")


# ── tiny YAML readers (top-level keys only; enough for these files) ─────────────────


def top_scalar(text: str, key: str) -> str | None:
    m = re.search(rf"^{re.escape(key)}:\s*(.+?)\s*$", text, re.M)
    if not m:
        return None
    val = m.group(1).split(" #")[0].strip()
    return None if val in ("|", ">", "") else val.strip("'\"")


def top_list(text: str, key: str) -> list[str]:
    m = re.search(rf"^{re.escape(key)}:\s*\n((?:[ \t]+-.*\n?)+)", text, re.M)
    if not m:
        return []
    items = re.findall(r"^[ \t]+-\s*(.+?)\s*$", m.group(1), re.M)
    return [i.split(" #")[0].strip().strip("'\"") for i in items]


def nested_scalar(text: str, parent: str, key: str) -> str | None:
    m = re.search(rf"^{re.escape(parent)}:\s*\n((?:[ \t]+.*\n?)+)", text, re.M)
    if not m:
        return None
    inner = re.search(rf"^[ \t]+{re.escape(key)}:\s*(.+?)\s*$", m.group(1), re.M)
    return inner.group(1).strip("'\"") if inner else None


def names_of(api: Api, path: str, field: str) -> dict[str, dict]:
    status, body = api.get(path)
    if status != 200 or not isinstance(body, list):
        return {}
    return {str(item.get(field)): item for item in body}


# ── steps ───────────────────────────────────────────────────────────────────────────


def load_curriculum(api: Api, rep: Report) -> None:
    print("Curriculum")
    pack = ROOT / "truenorth-content-pack/truenorth-content/crosswalk.csv"
    rep.line(api.post_files("/qsp/import-crosswalk", {"file": pack})[0], "QSP spine")
    s, d = api.post_files(
        "/qsp/import-competency-crosswalk",
        {
            "taxonomy": CONTENT / "catalogue/nist_csf_2_0_taxonomy.csv",
            "crosswalk": CONTENT / "catalogue/qsp_competency_crosswalk.csv",
        },
    )
    rep.line(s, "NICE / NIST CSF crosswalk", d)
    s, d = api.post_files(
        "/courses/import-programme", {"file": CONTENT / "catalogue/cyber_operator_programme.csv"}
    )
    rep.line(s, "programme catalogue", d)
    courses = sorted((CONTENT / "courses").glob("*.yaml"))
    bad = []
    for fp in courses:
        s, d = api.post_files("/courses/import-course-content", {"file": fp})
        if not (isinstance(s, int) and 200 <= s < 300):
            bad.append((fp.name, s, d))
    rep.line(200 if not bad else 500, f"{len(courses) - len(bad)}/{len(courses)} course files")
    for name, s, d in bad:
        rep.line(s, f"  {name}", d)
    s, d = api.post_json("/qsp/generate-learning-paths")
    rep.line(s, "CFITES learning paths", d)
    s, d = api.post_json("/courses/generate-programme-paths")
    rep.line(s, "programme delivery paths", d)


def load_golden_images(api: Api, rep: Report) -> None:
    print("VM image catalogue")
    s, d = api.post_files(
        "/golden-images/import-catalogue", {"file": CONTENT / "catalogue/vm_iso_catalogue.csv"}
    )
    rep.line(s, "golden-image catalogue", d)


def load_yaml_items(api: Api, rep: Report, label: str, path: str, files: list[Path]) -> dict[str, str]:
    """Templates and scenarios: POST {name, yaml}; skip names that already exist."""
    print(label)
    existing = names_of(api, path, "name")
    ids = {name: str(item["id"]) for name, item in existing.items()}
    for fp in files:
        text = fp.read_text(encoding="utf-8")
        name = top_scalar(text, "name") or fp.parent.name
        slug = top_scalar(text, "id") or fp.parent.name
        if name in existing:
            rep.line("skip", f"{name} (exists)")
        else:
            s, d = api.post_json(
                path,
                {"name": name, "version": top_scalar(text, "version") or "1.0", "yaml": text, "is_public": True},
            )
            rep.line(s, name, d)
            if isinstance(d, dict) and d.get("id"):
                ids[name] = str(d["id"])
        ids.setdefault(slug, ids.get(name, ""))
        ids.setdefault(fp.parent.name, ids.get(name, ""))
    return ids


LEVELS = {"informational", "low", "medium", "high", "critical"}
STATUSES = {"draft", "testing", "stable", "deprecated"}


def load_detections(api: Api, rep: Report) -> None:
    print("Detection rules")
    existing = names_of(api, "/detection-rules", "title")
    for fp in sorted((CONTENT / "detections").glob("*.yaml")):
        text = fp.read_text(encoding="utf-8")
        title = top_scalar(text, "title") or fp.stem
        if title in existing:
            rep.line("skip", f"{title} (exists)")
            continue
        level = (top_scalar(text, "level") or top_scalar(text, "severity") or "medium").lower()
        status = (top_scalar(text, "status") or "draft").lower()
        desc = re.search(r"^description:\s*\|?\s*\n((?:[ \t]+.*\n?)+)", text, re.M)
        payload = {
            "title": title,
            "sigma_id": top_scalar(text, "id"),
            "status": status if status in STATUSES else "draft",
            "description": " ".join(desc.group(1).split()) if desc else top_scalar(text, "description"),
            "level": level if level in LEVELS else "medium",
            "logsource_category": nested_scalar(text, "logsource", "category"),
            "logsource_product": nested_scalar(text, "logsource", "product"),
            "logsource_service": nested_scalar(text, "logsource", "service"),
            "detection_yaml": text,
            "mitre_attack_ids": top_list(text, "mitre_technique") or top_list(text, "tags") or None,
            "false_positives": top_list(text, "falsepositives") or None,
            "tags": top_list(text, "data_source") or None,
        }
        s, d = api.post_json("/detection-rules", payload)
        rep.line(s, title, d)


DEMO_PEOPLE = [
    ("instructor.demo@truenorth.test", "Demo Instructor", "instructor"),
    ("trainee1.demo@truenorth.test", "Demo Trainee 1", "student"),
    ("trainee2.demo@truenorth.test", "Demo Trainee 2", "student"),
    ("trainee3.demo@truenorth.test", "Demo Trainee 3", "student"),
    ("observer.demo@truenorth.test", "Demo Observer", "observer"),
]


def load_demo(api: Api, rep: Report, templates: dict[str, str], scenarios: dict[str, str]) -> None:
    print("Demo people, ranges and exercises")
    have = names_of(api, "/users", "email")
    for email, name, role in DEMO_PEOPLE:
        if email in have:
            rep.line("skip", f"{name} (exists)")
            continue
        s, d = api.post_json("/users", {"email": email, "display_name": name, "role": role})
        rep.line(s, f"{name} ({role})", d)

    # Pair each demo exercise with the scenario that names the range template it needs.
    pairs = [
        ("Demo — SOC analyst lab", "soc-training", "incident-response-drill"),
        ("Demo — Small enterprise", "small-enterprise", "ransomware-lite"),
    ]
    ranges = names_of(api, "/ranges", "name")
    exercises = names_of(api, "/exercises", "name")
    for range_name, tpl_key, scn_key in pairs:
        tpl_id, scn_id = templates.get(tpl_key), scenarios.get(scn_key)
        if not tpl_id or not scn_id:
            rep.line("skip", f"{range_name} (template or scenario missing)")
            continue
        if range_name in ranges:
            rng_id = str(ranges[range_name]["id"])
            rep.line("skip", f"range {range_name} (exists)")
        else:
            s, d = api.post_json("/ranges", {"name": range_name, "template_id": tpl_id})
            rep.line(s, f"range {range_name}", d)
            rng_id = str(d["id"]) if isinstance(d, dict) and d.get("id") else ""
            if rng_id:
                # Dev provisions with the mock backend; nothing real is created.
                rep.line(api.post_json(f"/ranges/{rng_id}/provision")[0], f"  provision (mock) {range_name}")
        ex_name = f"{range_name} — exercise"
        if ex_name in exercises:
            rep.line("skip", f"exercise {ex_name} (exists)")
        elif rng_id:
            s, d = api.post_json("/exercises", {"name": ex_name, "range_id": rng_id, "scenario_id": scn_id})
            rep.line(s, f"exercise {ex_name}", d)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--api", default="http://127.0.0.1:8081", help="API base URL (dev default)")
    ap.add_argument("--token", default=None, help="bearer token, for a deployment with auth on")
    ap.add_argument("--no-demo", action="store_true", help="skip demo people, ranges and exercises")
    args = ap.parse_args()

    api = Api(args.api, args.token)
    status, _ = api.get("/health")
    if status != 200:
        print(f"Cannot reach the API at {args.api} (is `make dev` running?)")
        return 2

    rep = Report()
    load_curriculum(api, rep)
    load_golden_images(api, rep)
    templates = load_yaml_items(
        api, rep, "Range templates", "/templates", sorted((CONTENT / "ranges").glob("*/template.yaml"))
    )
    scenarios = load_yaml_items(
        api, rep, "Scenarios", "/scenarios", sorted((CONTENT / "scenarios").glob("*/scenario.yaml"))
    )
    load_detections(api, rep)
    if not args.no_demo:
        load_demo(api, rep, templates, scenarios)

    if rep.failures:
        print(f"\n{rep.failures} step(s) failed — see FAIL lines above.")
        return 1
    print("\n✓ Everything loaded. Open http://localhost:4200 (press Refresh on Learning → Career path).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
