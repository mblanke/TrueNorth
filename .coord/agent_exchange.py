#!/usr/bin/env python3
"""Durable, evidence-first broker for Claude, Codex, and bounded Taz workers."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

STATES = (
    "draft", "oracle-ready", "dispatched", "candidate-returned", "guard-green",
    "independent-review", "ci-pending", "ci-green", "merged", "blocked", "rejected",
)
RISKS = ("low", "medium", "high", "critical")
DECISIONS = ("approve", "changes", "reject")
IN_FLIGHT = ("dispatched", "candidate-returned", "guard-green", "independent-review", "ci-pending")
REVIEW_QUEUE = ("candidate-returned", "guard-green", "independent-review", "ci-pending")
REQUIRED_BUNDLE_FILES = (
    "result-v2.json", "candidate.patch", "test-results.json", "diff-stat.txt", "taz-guard.json",
)
SECRET_RE = re.compile(
    r"(AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{36}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY)"
)
TRANSITIONS = {
    "draft": {"oracle-ready", "blocked"},
    "oracle-ready": {"dispatched", "blocked"},
    "dispatched": {"candidate-returned", "blocked", "rejected"},
    "candidate-returned": {"guard-green", "blocked", "rejected"},
    "guard-green": {"independent-review", "blocked", "rejected"},
    "independent-review": {"ci-pending", "blocked", "rejected"},
    "ci-pending": {"ci-green", "blocked", "rejected"},
    "ci-green": {"merged", "blocked", "rejected"},
    "blocked": {"oracle-ready", "dispatched", "rejected"},
    "rejected": {"oracle-ready", "dispatched"},
    "merged": set(),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards(
  id TEXT PRIMARY KEY, issue_number INTEGER, title TEXT NOT NULL, state TEXT NOT NULL,
  risk TEXT NOT NULL, autonomy TEXT NOT NULL, review_policy TEXT NOT NULL,
  base_ref TEXT, base_commit TEXT NOT NULL, source_url TEXT, worker TEXT, reviewer TEXT,
  allowed_paths TEXT NOT NULL, oracle_tests TEXT NOT NULL, required_checks TEXT NOT NULL,
  oracle_commands TEXT NOT NULL DEFAULT '[]',
  diff_budget INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  latest_result_id INTEGER, created TEXT NOT NULL, updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS card_results(
  id INTEGER PRIMARY KEY AUTOINCREMENT, card_id TEXT NOT NULL, received TEXT NOT NULL,
  worker TEXT, bundle_path TEXT, patch_sha256 TEXT, head_commit TEXT, status TEXT NOT NULL,
  errors TEXT NOT NULL, manifest TEXT NOT NULL,
  FOREIGN KEY(card_id) REFERENCES cards(id)
);
CREATE TABLE IF NOT EXISTS card_reviews(
  id INTEGER PRIMARY KEY AUTOINCREMENT, card_id TEXT NOT NULL, result_id INTEGER,
  reviewer TEXT NOT NULL, decision TEXT NOT NULL, note TEXT, created TEXT NOT NULL,
  FOREIGN KEY(card_id) REFERENCES cards(id)
);
CREATE TABLE IF NOT EXISTS card_events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, card_id TEXT NOT NULL,
  actor TEXT NOT NULL, from_state TEXT, to_state TEXT, kind TEXT NOT NULL, detail TEXT
);
CREATE TABLE IF NOT EXISTS card_watch_cursors(
  consumer TEXT PRIMARY KEY, last_seq INTEGER NOT NULL
);
"""


class ExchangeError(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")  # noqa: UP017 -- Python 3.9


def ensure_schema(con: sqlite3.Connection):
    con.executescript(SCHEMA)
    columns = {row[1] for row in con.execute("PRAGMA table_info(cards)").fetchall()}
    if "oracle_commands" not in columns:
        con.execute("ALTER TABLE cards ADD COLUMN oracle_commands TEXT NOT NULL DEFAULT '[]'")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _loads(value, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


def _event(con, card_id, actor, kind, detail="", old=None, new=None):
    con.execute(
        "INSERT INTO card_events(ts,card_id,actor,from_state,to_state,kind,detail) VALUES(?,?,?,?,?,?,?)",
        (_now(), card_id, actor, old, new, kind, detail or ""),
    )


def _card(con, card_id):
    row = con.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    if not row:
        raise ExchangeError(f"unknown card '{card_id}'")
    return row


def _card_dict(row):
    d = dict(row)
    for name in ("allowed_paths", "oracle_tests", "oracle_commands", "required_checks"):
        d[name] = _loads(d[name], [])
    return d


def _transition(con, row, new_state, actor, detail=""):
    old = row["state"]
    if new_state == old:
        return row
    if new_state not in STATES:
        raise ExchangeError(f"invalid state '{new_state}'")
    if new_state not in TRANSITIONS.get(old, set()):
        raise ExchangeError(f"invalid transition {old} -> {new_state}")
    con.execute("UPDATE cards SET state=?,updated=? WHERE id=?", (new_state, _now(), row["id"]))
    _event(con, row["id"], actor, "state", detail, old, new_state)
    return _card(con, row["id"])


def _read_json(path, label):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExchangeError(f"invalid {label}: {exc}") from exc


def _normalize_card(raw):
    required = ("id", "title", "base_commit", "allowed_paths", "oracle_tests", "oracle_commands")
    missing = [key for key in required if raw.get(key) in (None, "", [])]
    if missing:
        raise ExchangeError("card missing required fields: " + ", ".join(missing))
    card_id = str(raw["id"])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", card_id):
        raise ExchangeError("card id may contain only letters, digits, dot, underscore, and dash")
    base_commit = str(raw["base_commit"])
    if not re.fullmatch(r"[0-9a-f]{40}", base_commit):
        raise ExchangeError("base_commit must be a full lowercase 40-character Git SHA")
    risk = raw.get("risk", "medium")
    if risk not in RISKS:
        raise ExchangeError(f"invalid risk '{risk}'")
    paths = raw["allowed_paths"]
    tests = raw["oracle_tests"]
    commands = raw["oracle_commands"]
    if not isinstance(paths, list) or not all(isinstance(x, str) and x for x in paths):
        raise ExchangeError("allowed_paths must be a non-empty string list")
    if any(x.startswith(("/", "-")) or ".." in Path(x).parts or "\x00" in x for x in paths):
        raise ExchangeError("allowed_paths must be safe repository-relative patterns")
    if not isinstance(tests, list) or not all(isinstance(x, str) and x for x in tests):
        raise ExchangeError("oracle_tests must be a non-empty string list")
    if not isinstance(commands, list) or not all(isinstance(x, str) and x for x in commands):
        raise ExchangeError("oracle_commands must be a non-empty string list")
    budget = int(raw.get("diff_budget", 500))
    if budget < 1:
        raise ExchangeError("diff_budget must be positive")
    return {
        "id": card_id, "issue_number": raw.get("issue_number"), "title": str(raw["title"]),
        "state": raw.get("state", "draft"), "risk": risk,
        "autonomy": raw.get("autonomy", "candidate-only"),
        "review_policy": raw.get("review_policy", "dual"), "base_ref": raw.get("base_ref", "github/main"),
        "base_commit": base_commit, "source_url": raw.get("source_url"),
        "worker": raw.get("worker", "taz"), "reviewer": raw.get("reviewer", "claude"),
        "allowed_paths": paths, "oracle_tests": tests, "oracle_commands": commands,
        "required_checks": raw.get("required_checks", []), "diff_budget": budget,
    }


def cmd_import(args, con, root, state_dir):
    raw = _read_json(args.file, "card JSON")
    items = raw if isinstance(raw, list) else raw.get("cards", [raw]) if isinstance(raw, dict) else []
    if not items:
        raise ExchangeError("card input contains no cards")
    imported = []
    with con:
        for item in items:
            c = _normalize_card(item)
            if c["state"] not in STATES:
                raise ExchangeError(f"invalid state '{c['state']}'")
            existing = con.execute("SELECT * FROM cards WHERE id=?", (c["id"],)).fetchone()
            stamp = _now()
            if existing:
                if existing["state"] not in ("draft", "oracle-ready", "blocked", "rejected"):
                    raise ExchangeError(f"cannot update in-flight card {c['id']} ({existing['state']})")
                con.execute(
                    """UPDATE cards SET issue_number=?,title=?,risk=?,autonomy=?,review_policy=?,base_ref=?,
                    base_commit=?,source_url=?,worker=?,reviewer=?,allowed_paths=?,oracle_tests=?,oracle_commands=?,required_checks=?,
                    diff_budget=?,updated=? WHERE id=?""",
                    (c["issue_number"], c["title"], c["risk"], c["autonomy"], c["review_policy"],
                     c["base_ref"], c["base_commit"], c["source_url"], c["worker"], c["reviewer"],
                     _json(c["allowed_paths"]), _json(c["oracle_tests"]), _json(c["oracle_commands"]), _json(c["required_checks"]),
                     c["diff_budget"], stamp, c["id"]),
                )
                _event(con, c["id"], args.actor, "import-update", "card specification updated")
            else:
                if c["state"] not in ("draft", "oracle-ready"):
                    raise ExchangeError("new cards may start only in draft or oracle-ready")
                con.execute(
                    """INSERT INTO cards(id,issue_number,title,state,risk,autonomy,review_policy,base_ref,
                    base_commit,source_url,worker,reviewer,allowed_paths,oracle_tests,oracle_commands,required_checks,diff_budget,
                    created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (c["id"], c["issue_number"], c["title"], c["state"], c["risk"], c["autonomy"],
                     c["review_policy"], c["base_ref"], c["base_commit"], c["source_url"], c["worker"],
                     c["reviewer"], _json(c["allowed_paths"]), _json(c["oracle_tests"]), _json(c["oracle_commands"]),
                     _json(c["required_checks"]), c["diff_budget"], stamp, stamp),
                )
                _event(con, c["id"], args.actor, "import", "card imported", None, c["state"])
            imported.append(c["id"])
    _emit(args, {"imported": imported})


def cmd_list(args, con, root, state_dir):
    sql, params = "SELECT * FROM cards", []
    if args.state:
        sql += " WHERE state=?"
        params.append(args.state)
    rows = [_card_dict(x) for x in con.execute(sql + " ORDER BY updated DESC,id", params).fetchall()]
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    print("ID                         STATE                RISK      ATTEMPTS  TITLE")
    for c in rows:
        print(f"{c['id'][:26]:26} {c['state']:20} {c['risk']:9} {c['attempts']:8}  {c['title']}")


def cmd_show(args, con, root, state_dir):
    card = _card_dict(_card(con, args.id))
    card["results"] = [dict(x) for x in con.execute(
        "SELECT id,received,worker,bundle_path,patch_sha256,head_commit,status,errors FROM card_results WHERE card_id=? ORDER BY id",
        (args.id,),
    ).fetchall()]
    for result in card["results"]:
        result["errors"] = _loads(result["errors"], [])
    card["reviews"] = [dict(x) for x in con.execute(
        "SELECT id,result_id,reviewer,decision,note,created FROM card_reviews WHERE card_id=? ORDER BY id",
        (args.id,),
    ).fetchall()]
    print(json.dumps(card, indent=2))


def _config(state_dir):
    path = Path(state_dir) / "exchange.json"
    if path.exists():
        return _read_json(path, "exchange config")
    return {"max_review_queue": 3, "max_in_flight": 5}


def cmd_ready(args, con, root, state_dir):
    conf = _config(state_dir)
    review_count = con.execute(
        f"SELECT count(*) FROM cards WHERE state IN ({','.join('?' for _ in REVIEW_QUEUE)})", REVIEW_QUEUE
    ).fetchone()[0]
    inflight = con.execute(
        f"SELECT count(*) FROM cards WHERE state IN ({','.join('?' for _ in IN_FLIGHT)})", IN_FLIGHT
    ).fetchone()[0]
    slots = min(max(0, int(conf.get("max_review_queue", 3)) - review_count),
                max(0, int(conf.get("max_in_flight", 5)) - inflight))
    cards = [_card_dict(x) for x in con.execute(
        "SELECT * FROM cards WHERE state='oracle-ready' ORDER BY CASE risk WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,updated,id LIMIT ?",
        (slots,),
    ).fetchall()]
    out = {"dispatch_allowed": slots > 0, "slots": slots, "in_flight": inflight,
           "review_queue": review_count, "cards": cards}
    _emit(args, out)


def cmd_set_state(args, con, root, state_dir):
    if args.state == "merged" and args.actor != "owner":
        raise ExchangeError("only --actor owner may record a merged card")
    with con:
        row = _transition(con, _card(con, args.id), args.state, args.actor, args.note or "")
    _emit(args, _card_dict(row))


def cmd_export(args, con, root, state_dir):
    """Create a transport-neutral, read-only work package for an untrusted worker."""
    row = _card(con, args.id)
    if row["state"] != "oracle-ready":
        raise ExchangeError(f"export requires oracle-ready state, got {row['state']}")
    card = _card_dict(row)
    destination = Path(args.destination).resolve()
    package_name = f"{card['id']}-{card['base_commit'][:12]}"
    package = destination / package_name
    if package.exists():
        raise ExchangeError(f"export already exists: {package}")
    destination.mkdir(parents=True, exist_ok=True)
    package.mkdir()
    worker_card = {
        "schema_version": 2,
        **{key: card[key] for key in (
            "id", "issue_number", "title", "risk", "autonomy", "base_ref", "base_commit",
            "source_url", "allowed_paths", "oracle_tests", "required_checks", "diff_budget",
            "oracle_commands",
        )},
        "result_contract": {
            "required_files": list(REQUIRED_BUNDLE_FILES),
            "state_claim": "candidate-only",
            "patch_format": "git diff --binary",
        },
    }
    (package / "card-v2.json").write_text(json.dumps(worker_card, indent=2) + "\n", encoding="utf-8")
    instructions = f"""# Taz worker package: {card['id']}

You are a bounded candidate producer, not the merge authority.

1. Check out exact base commit `{card['base_commit']}` in a disposable worktree.
2. Change only paths permitted by `allowed_paths` in `card-v2.json`.
3. Run every exact command in `oracle_commands`; do not weaken, skip, or rewrite an oracle.
4. Stay within the {card['diff_budget']}-changed-line budget.
5. Run the repository Taz guard before returning work.
6. Return all five required result files. Report limitations and uncertainty honestly.

A result is only a candidate. Claude, Codex, CI, and the human merge authority may reject it.
Never push, merge, change GitHub state, or use credentials from the repository.
"""
    (package / "WORKER.md").write_text(instructions, encoding="utf-8")
    sums = []
    for name in ("card-v2.json", "WORKER.md"):
        sums.append(f"{_sha256(package / name)}  {name}")
    (package / "checksums.sha256").write_text("\n".join(sums) + "\n", encoding="utf-8")
    archive = None
    if args.archive:
        archive = destination / f"{package_name}.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(package, arcname=package.name)
    with con:
        row = _transition(con, row, "dispatched", args.actor,
                          f"worker package exported to {package}")
        _event(con, row["id"], args.actor, "export", str(archive or package))
    _emit(args, {"card_id": row["id"], "state": row["state"], "package": str(package),
                 "archive": str(archive) if archive else None})


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _patch_stats(root, patch):
    proc = subprocess.run(["git", "apply", "--numstat", str(patch)], cwd=str(root), text=True,
                          capture_output=True)
    if proc.returncode:
        raise ExchangeError("candidate.patch is not a valid Git patch: " + proc.stderr.strip())
    files, changed = [], 0
    for line in proc.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        files.append(path)
        if added.isdigit():
            changed += int(added)
        if deleted.isdigit():
            changed += int(deleted)
    if not files:
        raise ExchangeError("candidate.patch contains no changed files")
    return files, changed


def _path_allowed(path, patterns):
    for pattern in patterns:
        base = pattern[:-3].rstrip("/") if pattern.endswith("/**") else pattern
        if path == base or fnmatch.fnmatchcase(path, pattern):
            return True
        if pattern.endswith("/**") and path.startswith(base + "/"):
            return True
    return False


def _validate_bundle(card, bundle, root):
    missing = [name for name in REQUIRED_BUNDLE_FILES if not (bundle / name).is_file()]
    if missing:
        raise ExchangeError("result bundle missing: " + ", ".join(missing))
    manifest = _read_json(bundle / "result-v2.json", "result-v2.json")
    tests = _read_json(bundle / "test-results.json", "test-results.json")
    guard = _read_json(bundle / "taz-guard.json", "taz-guard.json")
    errors = []
    if manifest.get("schema_version") != 2:
        errors.append("schema_version must be 2")
    if str(manifest.get("card_id")) != card["id"]:
        errors.append("manifest card_id does not match")
    if manifest.get("base_commit") != card["base_commit"]:
        errors.append("manifest base_commit does not match card")
    if not manifest.get("worker"):
        errors.append("manifest worker is required")
    served = manifest.get("served_model") or {}
    if not isinstance(served, dict) or not served.get("id") or not served.get("runtime"):
        errors.append("manifest served_model requires id and runtime")
    if not manifest.get("summary"):
        errors.append("manifest summary is required")
    self_review = manifest.get("self_review") or {}
    if not isinstance(self_review, dict) or not isinstance(self_review.get("limitations"), list):
        errors.append("manifest self_review.limitations must be a list")
    confidence = self_review.get("confidence") if isinstance(self_review, dict) else None
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        errors.append("manifest self_review.confidence must be between 0 and 1")
    patch = bundle / "candidate.patch"
    digest = _sha256(patch)
    if manifest.get("patch_sha256") != digest:
        errors.append("patch_sha256 does not match candidate.patch")
    patch_text = patch.read_text(encoding="utf-8", errors="replace")
    if SECRET_RE.search(patch_text):
        errors.append("candidate.patch contains a likely secret")
    files, changed = _patch_stats(root, patch)
    claimed_files = sorted(manifest.get("files_changed") or [])
    if claimed_files != sorted(files):
        errors.append("manifest files_changed does not match patch")
    outside = [path for path in files if not _path_allowed(path, _loads(card["allowed_paths"], []))]
    if outside:
        errors.append("paths outside card scope: " + ", ".join(outside))
    if changed > card["diff_budget"]:
        errors.append(f"diff budget exceeded: {changed} > {card['diff_budget']}")
    if tests.get("passed") is not True:
        errors.append("test-results.json does not report passed=true")
    if guard.get("passed") is not True:
        errors.append("taz-guard.json does not report passed=true")
    guard_model = (guard.get("served_model") or {}).get("id") if isinstance(guard, dict) else None
    if guard_model != served.get("id"):
        errors.append("runner-observed served model does not match manifest")
    commands = tests.get("commands") or []
    required = _loads(card["oracle_commands"], [])
    executed = {str(x.get("command")) for x in commands if isinstance(x, dict) and x.get("exit_code") == 0}
    missing_tests = [test for test in required if test not in executed]
    if missing_tests:
        errors.append("required oracle tests lack passing evidence: " + "; ".join(missing_tests))
    return manifest, digest, files, changed, errors


def cmd_ingest(args, con, root, state_dir):
    card = _card(con, args.id)
    if card["state"] != "dispatched":
        raise ExchangeError(f"ingest requires dispatched state, got {card['state']}")
    bundle = Path(args.bundle).resolve()
    if not bundle.is_dir():
        raise ExchangeError(f"bundle directory not found: {bundle}")
    manifest, digest, files, changed, errors = _validate_bundle(card, bundle, root)
    result_root = Path(state_dir) / "results" / card["id"]
    result_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ingest-", dir=str(result_root)) as tmp:
        tmp_path = Path(tmp)
        for name in REQUIRED_BUNDLE_FILES:
            shutil.copy2(bundle / name, tmp_path / name)
        stamp = _now()
        with con:
            cur = con.execute(
                """INSERT INTO card_results(card_id,received,worker,bundle_path,patch_sha256,head_commit,status,errors,manifest)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (card["id"], stamp, manifest.get("worker", "taz"), "", digest, manifest.get("head_commit"),
                 "invalid" if errors else "guard-green", _json(errors), _json(manifest)),
            )
            result_id = cur.lastrowid
            final = result_root / str(result_id)
            os.replace(str(tmp_path), str(final))
            con.execute("UPDATE card_results SET bundle_path=? WHERE id=?", (str(final), result_id))
            con.execute("UPDATE cards SET attempts=attempts+1,latest_result_id=?,updated=? WHERE id=?",
                        (result_id, stamp, card["id"]))
            row = _transition(con, _card(con, card["id"]), "candidate-returned", "broker",
                              f"result {result_id} received ({changed} changed lines, {len(files)} files)")
            if errors:
                row = _transition(con, row, "rejected", "broker", "; ".join(errors))
            else:
                row = _transition(con, row, "guard-green", "broker", f"result {result_id} evidence validated")
    out = {"card_id": card["id"], "result_id": result_id, "state": row["state"],
           "valid": not errors, "errors": errors, "patch_sha256": digest,
           "files": files, "changed_lines": changed}
    _emit(args, out)
    if errors:
        raise ExchangeError("candidate rejected: " + "; ".join(errors))


def cmd_review(args, con, root, state_dir):
    if args.reviewer not in ("claude", "codex"):
        raise ExchangeError("reviewer must be claude or codex")
    if args.decision not in DECISIONS:
        raise ExchangeError("invalid review decision")
    row = _card(con, args.id)
    expected = "guard-green" if args.reviewer == "claude" else "independent-review"
    if row["state"] != expected:
        raise ExchangeError(f"{args.reviewer} review requires {expected} state, got {row['state']}")
    with con:
        con.execute(
            "INSERT INTO card_reviews(card_id,result_id,reviewer,decision,note,created) VALUES(?,?,?,?,?,?)",
            (row["id"], row["latest_result_id"], args.reviewer, args.decision, args.note, _now()),
        )
        _event(con, row["id"], args.reviewer, "review", f"{args.decision}: {args.note}".rstrip())
        if args.decision == "approve":
            target = "independent-review" if args.reviewer == "claude" else "ci-pending"
        else:
            target = "rejected"
        row = _transition(con, row, target, args.reviewer, args.note)
    _emit(args, _card_dict(row))


def cmd_ci(args, con, root, state_dir):
    row = _card(con, args.id)
    if row["state"] != "ci-pending":
        raise ExchangeError(f"CI evidence requires ci-pending state, got {row['state']}")
    if args.status == "green" and not args.run_url:
        raise ExchangeError("green CI requires --run-url evidence")
    target = "ci-green" if args.status == "green" else "rejected"
    detail = f"CI {args.status}" + (f": {args.run_url}" if args.run_url else "")
    with con:
        _event(con, row["id"], args.actor, "ci", detail)
        row = _transition(con, row, target, args.actor, detail)
    _emit(args, _card_dict(row))


def cmd_watch(args, con, root, state_dir):
    current = con.execute("SELECT last_seq FROM card_watch_cursors WHERE consumer=?", (args.consumer,)).fetchone()
    after = args.after if args.after is not None else (current[0] if current else 0)
    rows = [dict(x) for x in con.execute(
        "SELECT * FROM card_events WHERE seq>? ORDER BY seq LIMIT ?", (after, args.limit)
    ).fetchall()]
    last = rows[-1]["seq"] if rows else after
    if not args.peek:
        with con:
            con.execute("INSERT OR REPLACE INTO card_watch_cursors(consumer,last_seq) VALUES(?,?)",
                        (args.consumer, last))
    print(json.dumps({"consumer": args.consumer, "after": after, "last_seq": last, "events": rows}, indent=2))


def cmd_metrics(args, con, root, state_dir):
    total = con.execute("SELECT count(*) FROM card_results").fetchone()[0]
    valid = con.execute("SELECT count(*) FROM card_results WHERE status='guard-green'").fetchone()[0]
    rejected_reviews = con.execute(
        "SELECT count(*) FROM card_reviews WHERE decision IN ('changes','reject')"
    ).fetchone()[0]
    merged = con.execute("SELECT count(*) FROM cards WHERE state='merged'").fetchone()[0]
    first_pass = con.execute(
        "SELECT count(*) FROM cards WHERE state IN ('ci-green','merged') AND attempts=1"
    ).fetchone()[0]
    reached_review = con.execute(
        "SELECT count(*) FROM cards WHERE attempts>0"
    ).fetchone()[0]
    out = {
        "candidates": total, "guard_valid": valid,
        "guard_pass_rate": round(valid / total, 3) if total else None,
        "review_rejections": rejected_reviews, "merged_cards": merged,
        "first_pass_acceptance_rate": round(first_pass / reached_review, 3) if reached_review else None,
        "cards_with_rework": con.execute("SELECT count(*) FROM cards WHERE attempts>1").fetchone()[0],
    }
    print(json.dumps(out, indent=2))


def _emit(args, value):
    if getattr(args, "json", False):
        print(json.dumps(value, indent=2))
    elif isinstance(value, dict):
        print("[exchange] " + " ".join(f"{k}={v}" for k, v in value.items() if not isinstance(v, (dict, list))))
    else:
        print(value)


def execute(args, con, root, state_dir):
    ensure_schema(con)
    try:
        return args.exchange_func(args, con, Path(root), Path(state_dir))
    except ExchangeError as exc:
        raise SystemExit(f"[exchange] {exc}") from exc
