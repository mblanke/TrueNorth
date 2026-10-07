#!/usr/bin/env python3
"""
coord.py - TrueNorth session coordinator.

Stdlib only, single file, runs on Python 3.9+ (macOS /usr/bin/python3 included).
Static config: .coord/modules.json (tracked, never written by this tool).
Mutable state: .coord/state.sqlite (gitignored, lives in the MAIN checkout only).
Driven by you (CLI) and by Claude Code hooks (guard / session-start / checkpoint).

Every linked worktree resolves the main checkout through `git rev-parse --git-common-dir`,
so all sessions share one config and one state db. COORD_ROOT overrides that (tests).

Commands
  claim <module> [--dir D]        lease a module to this worktree (alias: session start)
  release [--dir D]               end this worktree's lease, drop its locks (alias: session end)
  session list                    active leases
  status [--rescan]               leases, locks, modules, open handoffs, in-flight work
  modules                         module readiness table
  module set <id> [--stage N] [--note T] [--blocker T] [--clear-blockers]
  next                            what to work on next
  gate <module> [--stage N]       run a stage gate in the current worktree
  checkpoint [--quiet] [--force] [-m MSG]   module-scoped gate + local commit (Stop hook)
  handoff add <target> <text> [--why W] [--from M] | handoff list [--all] [--module M] | handoff done <id> [--note N]
  scan [--json] [--no-branches]   map every worktree / branch diff (vs github/main, else main) to modules
  dashboard [--json] [--cached]   write .coord/dashboard.html (gitignored)
  locks                           serialized-path locks
  usage [--days N]                token burn from ~/.claude/projects transcripts (best effort)
  init                            create the state db and check the install (writes nothing tracked)
  guard | session-start           hook entry points (stdin JSON)
"""
from __future__ import annotations

import argparse
import fnmatch
import glob
import html
import json
import os
import re
import select
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE_FILE = Path(__file__).resolve()
COORD_CMD = 'python3 "$(git rev-parse --git-common-dir)/../.coord/coord.py"'


def _main_root():
    """Repo root of the MAIN checkout, so every linked worktree shares one config + state."""
    env = os.environ.get("COORD_ROOT")
    if env:
        return Path(env).resolve()
    try:
        r = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=str(HERE_FILE.parent),
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return (HERE_FILE.parent / r.stdout.strip()).resolve().parent
    except OSError:
        pass
    return HERE_FILE.parent.parent


ROOT = _main_root()
# Config + state live in the main checkout's .coord/. Before the coordinator has landed on the
# main checkout's branch, fall back to the copy next to this file (state then stays local).
STATE_DIR = ROOT / ".coord" if (ROOT / ".coord" / "modules.json").exists() else HERE_FILE.parent
CFG = STATE_DIR / "modules.json"
DB = STATE_DIR / "state.sqlite"
DASH = STATE_DIR / "dashboard.html"
DEFAULT_SECRET = r"(AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{36}|xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY)"
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", "build", "dist", ".angular"}
NON_GATE_TEST_DIRS = ("tests/integration/", "tests/e2e/", "tests/load/")


class Blocked(Exception):
    pass


# ---------------------------------------------------------------- basics
def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cutoff(c):
    mins = int(c.get("stale_minutes", 90))
    return (datetime.now(timezone.utc) - timedelta(minutes=mins)).isoformat(timespec="seconds")


_CFG_CACHE = None


def cfg():
    global _CFG_CACHE
    if _CFG_CACHE is None:
        with open(CFG) as f:
            _CFG_CACHE = json.load(f)
    return _CFG_CACHE


def run(args, cwd=None, timeout=None):
    try:
        return subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(args, 124, e.stdout or "", f"timeout after {timeout}s")
    except OSError as e:
        return subprocess.CompletedProcess(args, 127, "", str(e))


def git(args, cwd):
    r = run(["git", "-c", "core.quotepath=off"] + list(args), cwd=cwd)
    return r.stdout.strip() if r.returncode == 0 else ""


def toplevel(path):
    p = Path(path)
    if not p.is_dir():
        p = p.parent
    if not p.exists():
        return None
    out = git(["rev-parse", "--show-toplevel"], p)
    return os.path.realpath(out) if out else None


def branch_of(top):
    return git(["rev-parse", "--abbrev-ref", "HEAD"], top) or "HEAD"


def base_ref(c, cwd):
    for ref in c.get("base_refs", ["github/main", "main"]):
        if git(["rev-parse", "--verify", "-q", ref + "^{commit}"], cwd):
            return ref
    return None


def fmt_table(rows, headers):
    w = [max(len(str(x)) for x in col) for col in zip(headers, *rows)] if rows else [len(h) for h in headers]

    def line(r):
        return "  ".join(str(x).ljust(w[i]) for i, x in enumerate(r)).rstrip()

    return "\n".join([line(headers), line(["-" * x for x in w])] + [line(r) for r in rows])


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def read_stdin_json(block):
    """Hook payload. block=False never waits on an interactive or idle stdin."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        if not block:
            ready, _, _ = select.select([sys.stdin], [], [], 0.3)
            if not ready:
                return {}
        data = sys.stdin.read()
        return json.loads(data) if data.strip() else {}
    except Exception:
        return {}


# ---------------------------------------------------------------- path -> module
def _has_glob(s):
    return any(ch in s for ch in "*?[")


def _base(pat):
    return pat[:-3] if pat.endswith("/**") else pat


def _match(rel, pat):
    base = _base(pat)
    if not _has_glob(base) and (rel == base or rel.startswith(base.rstrip("/") + "/")):
        return True
    return fnmatch.fnmatchcase(rel, pat)


def _specificity(pat):
    """Exact files beat globs; among globs the longest literal prefix wins."""
    base = _base(pat)
    if not _has_glob(base):
        return 10000 + len(base) if not pat.endswith("/**") else len(base) + 1
    return min(i for i, ch in enumerate(pat) if ch in "*?[")


def _best(rel, items):
    best, score = None, -1
    for key, pats in items:
        for p in pats:
            if _match(rel, p):
                s = _specificity(p)
                if s > score:
                    best, score = key, s
    return best


def owner_of(c, rel):
    rel = rel.replace(os.sep, "/")
    mid = _best(rel, [(m["id"], m.get("paths", [])) for m in c["modules"]])
    return next((m for m in c["modules"] if m["id"] == mid), None) if mid else None


def serialized_key(c, rel):
    rel = rel.replace(os.sep, "/")
    return _best(rel, [(p, [p]) for p in c.get("serialized_paths", [])])


def is_shared(c, rel):
    return any(_match(rel.replace(os.sep, "/"), p) for p in c.get("shared_paths", []))


def module_for(c, mid):
    for m in c["modules"]:
        if m["id"] == mid:
            return m
    die(f"[coord] unknown module '{mid}'. Known: {', '.join(m['id'] for m in c['modules'])}")


def classify(c, rel):
    """('serialized', key) | ('shared', None) | ('module', id) | ('unowned', None)."""
    k = serialized_key(c, rel)
    if k:
        return "serialized", k
    if is_shared(c, rel):
        return "shared", None
    m = owner_of(c, rel)
    return ("module", m["id"]) if m else ("unowned", None)


# ---------------------------------------------------------------- state
SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY, module TEXT, cwd TEXT, branch TEXT,
  started TEXT, heartbeat TEXT, ended TEXT, claude_session TEXT);
CREATE TABLE IF NOT EXISTS presence(cwd TEXT PRIMARY KEY, seen TEXT, claude_session TEXT);
CREATE TABLE IF NOT EXISTS locks(key TEXT PRIMARY KEY, path TEXT, cwd TEXT, module TEXT, acquired TEXT, seen TEXT);
CREATE TABLE IF NOT EXISTS module_state(id TEXT PRIMARY KEY, stage INTEGER, blockers TEXT, notes TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS handoff(id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, target TEXT,
  from_module TEXT, from_cwd TEXT, text TEXT, why TEXT, status TEXT, done_at TEXT, done_note TEXT);
CREATE TABLE IF NOT EXISTS events(ts TEXT, kind TEXT, module TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS scan_cache(id INTEGER PRIMARY KEY CHECK (id = 1), ts REAL, data TEXT);
"""


def db():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB), timeout=20, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    con.executescript(SCHEMA)
    return con


def log(con, kind, module="", detail=""):
    con.execute("INSERT INTO events VALUES(?,?,?,?)", (now(), kind, module or "", detail or ""))


def touch(con, cwd, sid=None):
    t = now()
    con.execute("INSERT OR REPLACE INTO presence(cwd, seen, claude_session) VALUES(?,?,?)", (cwd, t, sid))
    con.execute("UPDATE sessions SET heartbeat=? WHERE cwd=? AND ended IS NULL", (t, cwd))
    if sid:
        con.execute("UPDATE sessions SET claude_session=? WHERE cwd=? AND ended IS NULL", (sid, cwd))


def last_seen(con, cwd, *extra):
    r = con.execute("SELECT seen FROM presence WHERE cwd=?", (cwd,)).fetchone()
    vals = [v for v in [r["seen"] if r else None] + list(extra) if v]
    return max(vals) if vals else ""


def active(con, c):
    cut = cutoff(c)
    rows = con.execute("SELECT * FROM sessions WHERE ended IS NULL ORDER BY started").fetchall()
    return [(r, last_seen(con, r["cwd"], r["heartbeat"]) < cut) for r in rows]


def lease_for(con, cwd):
    return con.execute("SELECT * FROM sessions WHERE cwd=? AND ended IS NULL ORDER BY id DESC", (cwd,)).fetchone()


def lock_live(con, c, row):
    return last_seen(con, row["cwd"], row["seen"]) >= cutoff(c)


def mstate(con, m):
    r = con.execute("SELECT * FROM module_state WHERE id=?", (m["id"],)).fetchone()
    if not r:
        return {"stage": int(m.get("stage", 0)), "blockers": list(m.get("blockers", [])), "notes": list(m.get("notes", []))}
    return {"stage": r["stage"], "blockers": json.loads(r["blockers"] or "[]"), "notes": json.loads(r["notes"] or "[]")}


def save_mstate(con, mid, st):
    con.execute("INSERT OR REPLACE INTO module_state VALUES(?,?,?,?,?)",
                (mid, st["stage"], json.dumps(st["blockers"]), json.dumps(st["notes"]), now()))


def stage_name(c, n):
    return c["stages"].get(str(n), f"stage {n}")


def open_handoffs(con, target=None):
    if target:
        return con.execute("SELECT * FROM handoff WHERE status='open' AND target=? ORDER BY id", (target,)).fetchall()
    return con.execute("SELECT * FROM handoff WHERE status='open' ORDER BY id").fetchall()


def spawn_dashboard(max_age=10 ** 9):
    """Regenerate the dashboard in a detached child so hooks return immediately."""
    if os.environ.get("COORD_NO_BG"):
        return
    try:
        subprocess.Popen([sys.executable, str(HERE_FILE), "dashboard", "--quiet", "--max-age", str(max_age)],
                         cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


# ---------------------------------------------------------------- git working-tree helpers
def dirty_files(top):
    """Changed, staged, deleted, renamed (both sides) and untracked files, repo-relative."""
    r = run(["git", "-c", "core.quotepath=off", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=top)
    if r.returncode:
        return []
    parts = r.stdout.split("\0")
    out, i = [], 0
    while i < len(parts):
        e = parts[i]
        i += 1
        if len(e) < 4:
            continue
        out.append(e[3:])
        if e[0] in "RC" and i < len(parts):
            out.append(parts[i])
            i += 1
    return [f for f in dict.fromkeys(out) if "__pycache__" not in f and not f.startswith(".claude/worktrees/")]


def committed_files(top, base):
    if not base:
        return []
    out = git(["diff", "--name-only", base + "...HEAD"], top)
    return [f for f in out.splitlines() if f]


def changed_modules(c, files):
    cnt, ser, other = Counter(), set(), Counter()
    for f in files:
        kind, val = classify(c, f)
        if kind == "module":
            cnt[val] += 1
        elif kind == "serialized":
            ser.add(val)
        else:
            other[kind] += 1
    return cnt, ser, other


# ---------------------------------------------------------------- claim / release
def cmd_claim(a):
    c = cfg()
    m = module_for(c, a.module)
    con = db()
    top = toplevel(a.dir or os.getcwd())
    if not top:
        die("[coord] not inside a git worktree")
    br = branch_of(top)
    if br in c.get("protected_branches", []) or br == "HEAD":
        die(f"[coord] refusing to lease {m['id']} on '{br}' ({top}): protected or detached. "
            f"Work in a Claude worktree on a claude/* branch.")
    for r, stale in active(con, c):
        if r["module"] == m["id"] and r["cwd"] != top:
            if not stale:
                die(f"[coord] {m['id']} is already leased to {r['cwd']} (branch {r['branch']}, since {r['started']}). "
                    f"Coordinate through `coord handoff add {m['id']} ...`, or if that session is dead: "
                    f"coord release --dir {r['cwd']}")
            con.execute("UPDATE sessions SET ended=? WHERE id=?", (now(), r["id"]))
            log(con, "lease_takeover", m["id"], f"{r['cwd']} -> {top}")
    prev = lease_for(con, top)
    con.execute("UPDATE sessions SET ended=? WHERE cwd=? AND ended IS NULL", (now(), top))
    con.execute("INSERT INTO sessions(module,cwd,branch,started,heartbeat) VALUES(?,?,?,?,?)",
                (m["id"], top, br, now(), now()))
    touch(con, top)
    log(con, "claim", m["id"], top)
    if prev and prev["module"] != m["id"]:
        print(f"[coord] released previous lease {prev['module']}")
    print(f"[coord] leased {m['id']} -> {top} (branch {br})")
    hint = c.get("worktree_hint", ".claude/worktrees/")
    if hint not in top.replace(os.sep, "/") + "/" or not br.startswith("claude/"):
        print(f"[coord] note: leases are meant for Claude app worktrees ({hint}<name>, claude/* branches)")
    st = mstate(con, m)
    print(f"[coord] stage {st['stage']} ({stage_name(c, st['stage'])}); checkpoint gate from stage {c.get('gate_from_stage', 3)}")
    spawn_dashboard()


def cmd_release(a):
    con = db()
    top = toplevel(a.dir) if a.dir else toplevel(os.getcwd())
    top = top or os.path.realpath(a.dir or os.getcwd())
    r = lease_for(con, top)
    n = con.execute("DELETE FROM locks WHERE cwd=?", (top,)).rowcount
    if not r:
        die(f"[coord] no active lease for {top}" + (f" (dropped {n} locks)" if n else ""))
    con.execute("UPDATE sessions SET ended=? WHERE cwd=? AND ended IS NULL", (now(), top))
    log(con, "release", r["module"], top)
    print(f"[coord] released {r['module']} ({top})" + (f"; dropped {n} locks" if n else ""))
    spawn_dashboard()


def cmd_session_list(a):
    c = cfg()
    con = db()
    rows = [[r["module"], r["cwd"], r["branch"], r["started"][:16], "STALE" if s else "ok"] for r, s in active(con, c)]
    print(fmt_table(rows, ["module", "dir", "branch", "started", "state"]) if rows else "[coord] no active leases")


# ---------------------------------------------------------------- guard (PreToolUse)
def _handoff_hint(target):
    return f'`coord handoff add {target} "<change needed>" --why "<reason>"`'


def guard(payload):
    """Raise Blocked(msg) to refuse the edit; return to allow."""
    ti = payload.get("tool_input") or {}
    fp = ti.get("file_path") or ti.get("notebook_path")
    if not fp:
        return
    cwd = payload.get("cwd") or os.getcwd()
    top = toplevel(cwd)
    if not top:
        return
    fpa = os.path.realpath(fp if os.path.isabs(fp) else os.path.join(cwd, fp))
    c = cfg()
    con = db()
    touch(con, top, payload.get("session_id"))
    mine = lease_for(con, top)
    sessions = active(con, c)
    for r, stale in sessions:      # a file inside another session's worktree (incl. one nested under main)
        other = r["cwd"]
        if other == top or stale or not fpa.startswith(other + os.sep):
            continue
        if not fpa.startswith(top + os.sep) or len(other) > len(top):
            raise Blocked(f"[coord] BLOCKED: {fp} is inside the worktree leased to {r['module']} ({other}). "
                          f"Edit in your own worktree, or hand the change over with {_handoff_hint(r['module'])}.")
    if not fpa.startswith(top + os.sep):
        return                                            # outside this repo copy (scratchpad, memory, ...)
    rel = os.path.relpath(fpa, top).replace(os.sep, "/")
    if rel.startswith(".claude/worktrees/"):
        return
    key = serialized_key(c, rel)
    if key:
        _acquire_lock(con, c, key, rel, top, mine)
        return
    if is_shared(c, rel):
        return
    owner = owner_of(c, rel)
    if not owner:
        return
    if mine and mine["module"] == owner["id"]:
        return
    for r, stale in sessions:
        if r["module"] == owner["id"] and r["cwd"] != top and not stale:
            raise Blocked(f"[coord] BLOCKED: {rel} belongs to module '{owner['id']}', leased by another session "
                          f"({r['cwd']}, branch {r['branch']}). Record the change for them with "
                          f"{_handoff_hint(owner['id'])} and carry on with your own module.")
    if mine and c.get("strict_leases", True):
        raise Blocked(f"[coord] BLOCKED: this worktree is leased to '{mine['module']}' but {rel} belongs to "
                      f"'{owner['id']}'. Record the change with {_handoff_hint(owner['id'])}, or ask the user "
                      f"to re-scope the lease (`coord claim {owner['id']}`).")
    if not mine:
        log(con, "edit_unleased", owner["id"], f"{top}: {rel}")


def _acquire_lock(con, c, key, rel, top, mine):
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute("SELECT * FROM locks WHERE key=?", (key,)).fetchone()
        if row and row["cwd"] != top and lock_live(con, c, row):
            holder = lease_for(con, row["cwd"])
            who = f"{holder['module']} @ {row['cwd']}" if holder else row["cwd"]
            con.execute("COMMIT")
            raise Blocked(f"[coord] BLOCKED: {rel} is a serialized path ({key}) locked by {who} since "
                          f"{row['acquired']}. Locks clear on that session's next checkpoint, release, or after "
                          f"{c.get('stale_minutes', 90)} min idle. Wait, or hand the change over with "
                          f"{_handoff_hint(holder['module'] if holder else '<module>')}.")
        fresh = not row or row["cwd"] != top
        acquired = row["acquired"] if row and not fresh else now()
        con.execute("INSERT OR REPLACE INTO locks(key,path,cwd,module,acquired,seen) VALUES(?,?,?,?,?,?)",
                    (key, rel, top, mine["module"] if mine else "", acquired, now()))
        if fresh:
            log(con, "lock", mine["module"] if mine else "", f"{key} -> {top}")
        con.execute("COMMIT")
    except Blocked:
        raise
    except Exception:
        con.execute("ROLLBACK")
        raise
    if fresh:
        spawn_dashboard()


def cmd_guard(a):
    payload = read_stdin_json(block=True)
    try:
        guard(payload)
    except Blocked as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)
    except Exception as e:  # never wedge the editor on a coordinator bug
        print(f"[coord] guard error (edit allowed): {e!r}", file=sys.stderr)


def release_clean_locks(con, c, top):
    dirty = dirty_files(top)
    gone = []
    for row in con.execute("SELECT * FROM locks WHERE cwd=?", (top,)).fetchall():
        if not any(serialized_key(c, f) == row["key"] for f in dirty):
            con.execute("DELETE FROM locks WHERE key=? AND cwd=?", (row["key"], top))
            gone.append(row["key"])
    return gone


# ---------------------------------------------------------------- session-start brief
def cmd_session_start_hook(a):
    payload = read_stdin_json(block=False)
    try:
        brief(payload.get("cwd") or os.getcwd(), payload.get("session_id"))
    except Exception as e:
        print(f"[coord] brief unavailable: {e!r}")
    spawn_dashboard(max_age=300)


def brief(cwd, sid=None):
    top = toplevel(cwd)
    if not top:
        return
    c = cfg()
    con = db()
    touch(con, top, sid)
    mine = lease_for(con, top)
    br = branch_of(top)
    print("## Coordinator brief (.coord/coord.py)")
    print(f"`coord` = `{COORD_CMD}`. Worktree `{top}` on branch `{br}`.")
    if br in c.get("protected_branches", []):
        print(f"This is protected branch `{br}`: no leases, no checkpoints here.")
    if mine:
        m = module_for(c, mine["module"])
        st = mstate(con, m)
        paths = m.get("paths", [])
        print(f"Leased to module **{m['id']}** ({m['name']}), stage {st['stage']} ({stage_name(c, st['stage'])}).")
        print(f"Owned paths ({len(paths)}): " + ", ".join(paths[:14]) + (" ..." if len(paths) > 14 else ""))
        print("Strict leases: edits to other modules are blocked; use `coord handoff add <module> \"<change>\"`. "
              "Serialized god-files (models/schemas/main/seed, alembic, docs/interfaces, routes, workflows, "
              "baselines) lock on first edit until your next checkpoint.")
        gs = min(st["stage"], int(c.get("checkpoint_gate_max_stage", 4)))
        if st["stage"] >= int(c.get("gate_from_stage", 3)):
            print(f"Stop hook checkpoints commit your module's dirty files after the module-scoped stage-{gs} gate "
                  f"(not DoD; `bash scripts/dod.sh` is DoD).")
        if st["blockers"]:
            print("Blockers: " + "; ".join(st["blockers"]))
        if st["notes"]:
            print("Recent notes: " + " | ".join(st["notes"][-3:]))
        items = open_handoffs(con, m["id"])
        if items:
            print(f"Open handoff items for {m['id']}:")
            for h in items[:10]:
                print(f"  #{h['id']} from {h['from_module'] or '?'}: {h['text']}" + (f" ({h['why']})" if h["why"] else ""))
    else:
        base = base_ref(c, top)
        cnt, ser, _ = changed_modules(c, committed_files(top, base) + dirty_files(top))
        print("No lease for this worktree.", end=" ")
        if cnt:
            guess = ", ".join(f"{k} ({n} files)" for k, n in cnt.most_common(3))
            print(f"Changes on this branch vs {base or 'base'} map to: {guess}.")
            print(f"Before editing, propose to the user: `coord claim {cnt.most_common(1)[0][0]}` "
                  f"(or another module from `coord modules`).")
        else:
            print("Before editing, ask the user which module this session is for and propose `coord claim <module>` "
                  "(`coord modules` lists them).")
        if ser:
            print("Serialized paths touched on this branch: " + ", ".join(sorted(ser)))
    others = [(r, s) for r, s in active(con, c) if r["cwd"] != top]
    if others:
        print("Other leases: " + "; ".join(f"{r['module']}@{Path(r['cwd']).name}{' (stale)' if s else ''}" for r, s in others))
    locks = [r for r in con.execute("SELECT * FROM locks").fetchall() if r["cwd"] != top and lock_live(con, c, r)]
    if locks:
        print("Locked serialized paths: " + "; ".join(f"{r['key']} by {r['module'] or Path(r['cwd']).name}" for r in locks))
    n = len(open_handoffs(con))
    if n:
        print(f"{n} open handoff item(s) in total: `coord handoff list`.")


# ---------------------------------------------------------------- gates
def _python_ok(p):
    if not Path(p).exists():
        return False
    return run([str(p), "-c", "pass"], timeout=30).returncode == 0


def py_for(top):
    for cand in (Path(top) / ".venv" / "bin" / "python", ROOT / ".venv" / "bin" / "python"):
        if _python_ok(cand):
            return str(cand)
    return shutil.which("python3") or sys.executable


def _walk(top, base):
    full = Path(top) / base
    if full.is_file():
        yield base
        return
    if not full.is_dir():
        return
    for d, dirs, files in os.walk(full):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for f in files:
            yield os.path.relpath(os.path.join(d, f), top).replace(os.sep, "/")


def module_py_files(c, m, top):
    """Python files this module owns in `top` (serialized god-files excluded)."""
    bases = set()
    for p in m.get("paths", []):
        b = _base(p)
        if _has_glob(b):
            b = b[:min(i for i, ch in enumerate(b) if ch in "*?[")]
            b = b if b.endswith("/") else os.path.dirname(b)
        bases.add(b.rstrip("/") or ".")
    out = set()
    for b in sorted(bases):
        for rel in _walk(top, b):
            if rel.endswith(".py") and not serialized_key(c, rel):
                o = owner_of(c, rel)
                if o and o["id"] == m["id"]:
                    out.add(rel)
    return sorted(out)


def module_tests(pyfiles):
    return [f for f in pyfiles if f.startswith("tests/") and not f.startswith(NON_GATE_TEST_DIRS)
            and (os.path.basename(f).startswith("test_") or f.endswith("_test.py"))]


def expand(cmd, ctx):
    """Template -> argv, or (None, reason) when a list placeholder is empty."""
    argv = []
    for tok in shlex.split(cmd):
        if tok in ("{pypaths}", "{tests}", "{paths}"):
            lst = ctx[tok[1:-1]]
            if not lst:
                return None, f"no {tok[1:-1]} for this module"
            argv.extend(lst)
        else:
            argv.append(tok.replace("{py}", ctx["py"]).replace("{module}", ctx["module"]))
    return argv, ""


def run_gate(c, m, stage, top, out=print):
    g = c["gates"].get(str(stage))
    if not g:
        out(f"  (no gate defined for stage {stage})")
        return True
    pyfiles = module_py_files(c, m, top)
    ctx = {"py": py_for(top), "module": m["id"], "pypaths": pyfiles, "tests": module_tests(pyfiles),
           "paths": sorted({_base(p) for p in m.get("paths", []) if not _has_glob(_base(p)) and (Path(top) / _base(p)).exists()})}
    ok = True
    for cmd in g.get("commands", []):
        argv, why = expand(cmd, ctx)
        if argv is None:
            out(f"  SKIP  {cmd}  ({why})")
            continue
        shown = (cmd.replace("{pypaths}", f"<{len(ctx['pypaths'])} py files>")
                 .replace("{tests}", f"<{len(ctx['tests'])} test files>").replace("{py}", "python"))
        r = run(argv, cwd=top, timeout=int(c.get("gate_timeout_seconds", 900)))
        out(f"  {'PASS' if r.returncode == 0 else 'FAIL'}  {shown}")
        if r.returncode:
            ok = False
            tail = [x for x in (r.stdout + r.stderr).strip().splitlines() if x.strip()][-6:]
            for x in tail:
                out("        " + x[:200])
    for item in g.get("manual", []):
        out(f"  CHECK {item}")
    return ok


def cmd_gate(a):
    c = cfg()
    m = module_for(c, a.module)
    con = db()
    cur = mstate(con, m)["stage"]
    stage = a.stage if a.stage is not None else cur + 1
    top = toplevel(os.getcwd()) or str(ROOT)
    print(f"[coord] gate for {m['id']} in {top}: stage {cur} -> {stage} {stage_name(c, stage)}")
    ok = run_gate(c, m, stage, top)
    print("[coord] automated checks " + (f"PASSED. Promote with: coord module set {m['id']} --stage {stage}" if ok else "FAILED"))
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------- checkpoint (Stop hook)
def cmd_checkpoint(a):
    payload = read_stdin_json(block=False) if a.quiet else {}
    msgs = []

    def say(msg, refused=False):
        if a.quiet:
            msgs.append(msg)
        else:
            print(msg, file=sys.stderr if refused else sys.stdout)

    ok = True
    try:
        ok = checkpoint(a, payload.get("cwd") or os.getcwd(), payload.get("session_id"), say)
    except Exception as e:
        say(f"[coord] checkpoint error: {e!r}", True)
        ok = False
    if a.quiet:
        if msgs:
            print(json.dumps({"systemMessage": "\n".join(msgs)[-3000:]}))
        return
    sys.exit(0 if ok else 1)


def checkpoint(a, cwd, sid, say):
    top = toplevel(cwd)
    if not top:
        return True
    c = cfg()
    con = db()
    touch(con, top, sid)
    mine = lease_for(con, top)
    if not mine:
        release_clean_locks(con, c, top)
        if not a.quiet:
            say("[coord] checkpoint refused: no lease for this worktree (`coord claim <module>` first)", True)
            return False
        return True
    m = module_for(c, mine["module"])
    br = branch_of(top)
    if br in c.get("protected_branches", []) or br == "HEAD":
        say(f"[coord] checkpoint refused: '{br}' is protected/detached. Nothing committed.", True)
        return False
    dirty = dirty_files(top)
    if not dirty:
        release_clean_locks(con, c, top)
        if not a.quiet:
            say("[coord] clean tree; nothing to checkpoint")
        return True
    held = {r["key"] for r in con.execute("SELECT key FROM locks WHERE cwd=?", (top,)).fetchall()}
    take, left = [], []
    for f in dirty:
        kind, val = classify(c, f)
        if (kind == "module" and val == m["id"]) or (kind == "serialized" and val in held):
            take.append(f)
        else:
            left.append(f)
    if not take:
        if not a.quiet:
            say(f"[coord] nothing owned by {m['id']} is dirty; {len(left)} other file(s) left alone")
        return True
    pat = re.compile(c.get("secret_regex", DEFAULT_SECRET))
    for f in take:
        p = Path(top) / f
        if p.is_file() and p.stat().st_size < 2_000_000:
            blob = p.read_bytes()
            if b"\0" not in blob[:8192] and pat.search(blob.decode("utf-8", "ignore")):
                say(f"[coord] checkpoint refused: possible secret in {f}. Nothing committed.", True)
                return False
    st = mstate(con, m)
    gate_note = "not run (stage < %s)" % c.get("gate_from_stage", 3)
    if c.get("checkpoint_runs_gate", True) and st["stage"] >= int(c.get("gate_from_stage", 3)):
        gs = min(st["stage"], int(c.get("checkpoint_gate_max_stage", 4)))
        lines = []
        passed = run_gate(c, m, gs, top, out=lines.append)
        gate_note = f"stage-{gs} module gate {'PASS' if passed else 'FAIL'}"
        if not passed and not a.force:
            say(f"[coord] checkpoint skipped for {m['id']}: {gate_note} (module-scoped, not DoD). "
                f"Work stays uncommitted.\n" + "\n".join(x for x in lines if not x.startswith("  CHECK")), True)
            return False
        if not passed:
            gate_note += " (forced)"
    specs = [":(literal)" + f for f in take]
    r = run(["git", "add", "-A", "--"] + specs, cwd=top)
    if r.returncode:
        say(f"[coord] checkpoint failed at git add: {r.stderr.strip()[:300]}", True)
        return False
    summary = a.message or f"{len(take)} file(s): " + ", ".join(Path(p).name for p in take[:4]) + (" ..." if len(take) > 4 else "")
    body = f"[coord] module {m['id']} stage {st['stage']}; {gate_note}; lease {mine['id']}"
    r = run(["git", "commit", "-q", "-m", f"wip({m['id']}): {summary}", "-m", body, "--"] + specs, cwd=top)
    if r.returncode:
        say(f"[coord] checkpoint commit failed: {(r.stderr or r.stdout).strip()[:400]}", True)
        return False
    sha = git(["rev-parse", "--short", "HEAD"], top)
    gone = release_clean_locks(con, c, top)
    log(con, "checkpoint", m["id"], f"{sha} {summary}")
    extra = f"; released locks: {', '.join(gone)}" if gone else ""
    extra += f"; {len(left)} non-{m['id']} file(s) left uncommitted" if left else ""
    say(f"[coord] checkpoint {sha} on {br}: wip({m['id']}): {summary} [{gate_note}]{extra}")
    spawn_dashboard(max_age=60)
    return True


# ---------------------------------------------------------------- handoff
def cmd_handoff(a):
    c = cfg()
    con = db()
    if a.sub == "add":
        module_for(c, a.target)
        top = toplevel(os.getcwd()) or ""
        lease = lease_for(con, top) if top else None
        frm = a.from_module or (lease["module"] if lease else "")
        cur = con.execute("INSERT INTO handoff(created,target,from_module,from_cwd,text,why,status) VALUES(?,?,?,?,?,?,?)",
                          (now(), a.target, frm, top, a.text, a.why or "", "open"))
        log(con, "handoff_add", a.target, a.text)
        print(f"[coord] handoff #{cur.lastrowid} -> {a.target}: {a.text}")
    elif a.sub == "list":
        q = "SELECT * FROM handoff" + ("" if a.all else " WHERE status='open'")
        rows = [r for r in con.execute(q + " ORDER BY id").fetchall() if not a.module or r["target"] == a.module]
        if not rows:
            print("[coord] no handoff items")
            return
        print(fmt_table([[r["id"], r["status"], r["target"], r["from_module"] or "-", r["created"][:10],
                          (r["text"] + (f" ({r['why']})" if r["why"] else ""))[:90]] for r in rows],
                        ["id", "status", "target", "from", "created", "change"]))
    elif a.sub == "done":
        r = con.execute("SELECT * FROM handoff WHERE id=?", (a.id,)).fetchone()
        if not r:
            die(f"[coord] no handoff #{a.id}")
        con.execute("UPDATE handoff SET status='done', done_at=?, done_note=? WHERE id=?", (now(), a.note or "", a.id))
        log(con, "handoff_done", r["target"], str(a.id))
        print(f"[coord] handoff #{a.id} done")
    spawn_dashboard()


# ---------------------------------------------------------------- modules / next / module set / locks
def cmd_modules(a):
    c = cfg()
    con = db()
    leased = {r["module"]: (r["cwd"], s) for r, s in active(con, c)}
    locks = Counter(r["module"] for r in con.execute("SELECT module FROM locks").fetchall())
    hand = Counter(r["target"] for r in open_handoffs(con))
    rows = []
    for m in sorted(c["modules"], key=lambda m: (-m.get("priority", 1), m["id"])):
        st = mstate(con, m)
        who = leased.get(m["id"])
        sess = (("STALE " if who[1] else "") + Path(who[0]).name) if who else "-"
        rows.append([m["id"], m["name"][:30], f"{st['stage']} {stage_name(c, st['stage'])}", m.get("priority", 1),
                     sess, locks.get(m["id"], 0) or "-", hand.get(m["id"], 0) or "-",
                     ("; ".join(st["blockers"]) or "-")[:40]])
    print(fmt_table(rows, ["id", "module", "stage", "pri", "lease", "locks", "handoff", "blockers"]))
    print("\nstages: " + " | ".join(f"{k}={v}" for k, v in c["stages"].items()))


def cmd_module_set(a):
    c = cfg()
    m = module_for(c, a.id)
    con = db()
    st = mstate(con, m)
    if a.stage is not None:
        log(con, "stage", m["id"], f"{st['stage']} -> {a.stage}")
        st["stage"] = a.stage
    if a.note:
        st["notes"].append(f"{now()[:10]} {a.note}")
    if a.blocker:
        st["blockers"].append(a.blocker)
    if a.clear_blockers:
        st["blockers"] = []
    save_mstate(con, m["id"], st)
    print(f"[coord] {m['id']} stage={st['stage']} blockers={st['blockers']}")
    spawn_dashboard()


def cmd_next(a):
    c = cfg()
    con = db()
    leased = {r["module"] for r, s in active(con, c) if not s}
    top = max(int(k) for k in c["stages"])
    cands = []
    for m in c["modules"]:
        st = mstate(con, m)
        if m["id"] not in leased and st["stage"] < top and not st["blockers"]:
            cands.append((m, st))
    cands.sort(key=lambda x: (-x[0].get("priority", 1), x[1]["stage"], x[0]["id"]))
    print("Recommended next (unleased, unblocked; priority first, then lowest stage):")
    for m, st in cands[:6]:
        g = c["gates"].get(str(st["stage"] + 1), {})
        print(f"  {m['id']:<16} {st['stage']} -> {st['stage'] + 1} {stage_name(c, st['stage'] + 1):<14} gate: {g.get('desc', '-')}")
    blocked = [(m, mstate(con, m)) for m in c["modules"]]
    blocked = [(m, st) for m, st in blocked if st["blockers"]]
    if blocked:
        print("Blocked:")
        for m, st in blocked:
            print(f"  {m['id']:<16} {'; '.join(st['blockers'])}")


def cmd_locks(a):
    c = cfg()
    con = db()
    rows = [[r["key"], r["path"], r["module"] or "-", r["cwd"], r["acquired"][:16], "ok" if lock_live(con, c, r) else "STALE"]
            for r in con.execute("SELECT * FROM locks ORDER BY acquired").fetchall()]
    print(fmt_table(rows, ["key", "path", "module", "holder", "acquired", "state"]) if rows else "[coord] no locks")


# ---------------------------------------------------------------- scan
def worktrees():
    out = git(["worktree", "list", "--porcelain"], ROOT)
    items, cur = [], {}
    for line in out.splitlines() + [""]:
        if not line:
            if cur:
                items.append(cur)
            cur = {}
            continue
        k, _, v = line.partition(" ")
        if k == "worktree":
            cur["path"] = os.path.realpath(v)
        elif k == "branch":
            cur["branch"] = v.replace("refs/heads/", "", 1)
        elif k in ("detached", "bare", "locked", "prunable"):
            cur[k] = True
    return items


def scan(c, con, branches=True):
    base = base_ref(c, ROOT)
    leases = {r["cwd"]: (r, s) for r, s in active(con, c)}
    items, seen_branches = [], set()

    def entry(kind, where, branch, files, ahead, dirty):
        cnt, ser, other = changed_modules(c, files)
        flags = []
        if ahead == 0 and not dirty:
            flags.append("empty")
        if dirty:
            flags.append("dirty")
        lease = leases.get(where)
        return {"kind": kind, "where": where, "name": Path(where).name if kind == "worktree" else branch,
                "branch": branch, "ahead": ahead, "dirty": len(dirty), "modules": dict(cnt.most_common()),
                "serialized": sorted(ser), "shared": other.get("shared", 0), "unowned": other.get("unowned", 0),
                "flags": flags, "lease": lease[0]["module"] if lease else None,
                "lease_stale": bool(lease and lease[1])}

    for wt in worktrees():
        if wt.get("bare"):
            continue
        p, br = wt.get("path", ""), wt.get("branch") or "(detached)"
        seen_branches.add(br)
        if wt.get("prunable") or not Path(p).is_dir():
            items.append({"kind": "worktree", "where": p, "name": Path(p).name, "branch": br, "ahead": 0, "dirty": 0,
                          "modules": {}, "serialized": [], "shared": 0, "unowned": 0, "flags": ["missing"],
                          "lease": None, "lease_stale": False})
            continue
        ahead = int(git(["rev-list", "--count", base + "..HEAD"], p) or 0) if base else 0
        dirty = dirty_files(p)
        files = list(dict.fromkeys(committed_files(p, base) + dirty))
        items.append(entry("worktree", p, br, files, ahead, dirty))
    if branches and base:
        for br in git(["for-each-ref", "--format=%(refname:short)", "refs/heads/"], ROOT).splitlines():
            if br in seen_branches or br in c.get("protected_branches", []):
                continue
            ahead = int(git(["rev-list", "--count", base + ".." + br], ROOT) or 0)
            files = git(["diff", "--name-only", base + "..." + br], ROOT).splitlines() if ahead else []
            items.append(entry("branch", br, br, [f for f in files if f], ahead, []))
    data = {"ts": now(), "base": base, "items": items}
    con.execute("INSERT OR REPLACE INTO scan_cache(id, ts, data) VALUES(1, ?, ?)", (time.time(), json.dumps(data)))
    return data


def cached_scan(c, con, max_age, branches=True):
    r = con.execute("SELECT ts, data FROM scan_cache WHERE id=1").fetchone()
    if r and time.time() - r["ts"] <= max_age:
        return json.loads(r["data"])
    return scan(c, con, branches)


def _mods(d):
    return ", ".join(f"{k}:{v}" for k, v in d.items()) or "-"


def cmd_scan(a):
    c = cfg()
    con = db()
    data = scan(c, con, branches=not a.no_branches)
    if a.json:
        print(json.dumps(data, indent=2))
        return
    print(f"[coord] in-flight work vs {data['base'] or '(no base ref)'}")
    rows, empty = [], []
    for it in data["items"]:
        if it["flags"] == ["empty"] and it["kind"] == "branch":
            empty.append(it["name"])
            continue
        rows.append([it["kind"][0].upper(), it["name"][:34], it["branch"][:34], it["ahead"], it["dirty"] or "-",
                     _mods(it["modules"])[:60], ",".join(it["serialized"])[:30] or "-",
                     ",".join(it["flags"]) or "-", (it["lease"] or "-") + (" (stale)" if it["lease_stale"] else "")])
    print(fmt_table(rows, ["", "worktree/branch", "branch", "ahead", "dirty", "modules (files)", "serialized", "flags", "lease"]))
    if empty:
        print(f"\n{len(empty)} branch(es) with no commits beyond base (flag: empty): " + ", ".join(empty[:20]) + (" ..." if len(empty) > 20 else ""))


# ---------------------------------------------------------------- status
def cmd_status(a):
    c = cfg()
    con = db()
    if STATE_DIR != ROOT / ".coord":
        print(f"[coord] note: {ROOT}/.coord/modules.json not found; using {STATE_DIR} (coordinator not on the main checkout yet)")
    print("== Leases")
    cmd_session_list(a)
    print("\n== Locks")
    cmd_locks(a)
    print("\n== Modules")
    cmd_modules(a)
    hs = open_handoffs(con)
    print(f"\n== Open handoff ({len(hs)})")
    for h in hs:
        print(f"  #{h['id']} -> {h['target']} from {h['from_module'] or '?'}: {h['text']}")
    data = scan(c, con) if a.rescan else cached_scan(c, con, 600)
    busy = [it for it in data["items"] if it["modules"] or it["dirty"]]
    print(f"\n== In flight vs {data['base']} (scan {data['ts'][:16]}; `coord scan` for detail)")
    for it in busy:
        print(f"  {it['name'][:34]:<34} {_mods(it['modules'])[:70]}" + (f"  dirty:{it['dirty']}" if it["dirty"] else ""))


# ---------------------------------------------------------------- dashboard
def dashboard_data(c, con, data):
    sessions = active(con, c)
    locks = con.execute("SELECT * FROM locks ORDER BY acquired").fetchall()
    mods = []
    for m in c["modules"]:
        st = mstate(con, m)
        lease = next(((r, s) for r, s in sessions if r["module"] == m["id"]), None)
        mods.append({
            "id": m["id"], "name": m["name"], "priority": m.get("priority", 1), "stage": st["stage"],
            "blockers": st["blockers"], "notes": st["notes"][-3:],
            "lease": {"cwd": lease[0]["cwd"], "branch": lease[0]["branch"], "since": lease[0]["started"],
                      "stale": lease[1]} if lease else None,
            "locks": [{"key": r["key"], "path": r["path"], "since": r["acquired"], "live": lock_live(con, c, r)}
                      for r in locks if r["module"] == m["id"]],
            "inflight": [{"name": it["name"], "branch": it["branch"], "files": it["modules"][m["id"]],
                          "dirty": it["dirty"], "kind": it["kind"]}
                         for it in data["items"] if m["id"] in it["modules"]],
            "handoff": [{"id": h["id"], "from": h["from_module"], "text": h["text"], "why": h["why"]}
                        for h in open_handoffs(con, m["id"])],
        })
    return {"generated": now(), "root": str(ROOT), "stages": c["stages"], "base": data.get("base"),
            "scan_ts": data.get("ts"), "modules": mods,
            "locks": [{"key": r["key"], "path": r["path"], "module": r["module"], "holder": r["cwd"],
                       "since": r["acquired"], "live": lock_live(con, c, r)} for r in locks],
            "unowned_locks": [r["key"] for r in locks if not r["module"]],
            "flags": {"empty": [it["name"] for it in data["items"] if "empty" in it["flags"]],
                      "dirty": [it["name"] for it in data["items"] if "dirty" in it["flags"]]}}


CSS = """
:root{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1f;--mut:#6b6b70;--line:#e3e3e0;--acc:#2f6fdb;--ok:#2e7d32;--warn:#b26a00;--bad:#c62828;--pill:#ececea}
@media (prefers-color-scheme: dark){:root{--bg:#141416;--card:#1d1d20;--fg:#ececee;--mut:#9a9aa2;--line:#2c2c31;--acc:#6ea0ff;--ok:#66bb6a;--warn:#ffb74d;--bad:#ef5350;--pill:#2a2a2f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,system-ui,sans-serif;padding:16px}
h1{font-size:18px;margin:0 0 4px}.sub{color:var(--mut);font-size:12px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;min-width:0}
.card h2{font-size:14px;margin:0;display:flex;justify-content:space-between;gap:8px}.id{color:var(--mut);font-weight:400;font-size:12px}
.ladder{display:flex;gap:3px;margin:8px 0}.ladder span{flex:1;height:6px;border-radius:3px;background:var(--pill)}.ladder span.on{background:var(--acc)}
.stg{font-size:12px;color:var(--mut)}.row{font-size:12px;margin-top:6px;overflow-wrap:anywhere}.k{color:var(--mut)}
.tag{display:inline-block;padding:0 6px;border-radius:8px;background:var(--pill);font-size:11px;margin:1px 2px 1px 0}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}ul{margin:4px 0 0 16px;padding:0}
"""


def write_dashboard(d):
    e = html.escape
    nstages = len(d["stages"])
    cards = []
    for m in sorted(d["modules"], key=lambda m: (-m["priority"], m["id"])):
        ladder = "".join(f'<span class="{"on" if i <= m["stage"] else ""}" title="{i} {e(d["stages"].get(str(i), ""))}"></span>'
                         for i in range(nstages))
        rows = []
        if m["lease"]:
            L = m["lease"]
            rows.append(f'<div class="row"><span class="k">lease</span> <span class="{"warn" if L["stale"] else "ok"}">'
                        f'{e(Path(L["cwd"]).name)}</span> on {e(L["branch"])}{" (stale)" if L["stale"] else ""}</div>')
        else:
            rows.append('<div class="row"><span class="k">lease</span> none</div>')
        for lk in m["locks"]:
            rows.append(f'<div class="row"><span class="k">lock</span> <span class="{"warn" if lk["live"] else "k"}">{e(lk["key"])}</span></div>')
        if m["inflight"]:
            tags = "".join(f'<span class="tag" title="{e(x["branch"])}">{e(x["name"][:28])} · {x["files"]}'
                           f'{" · dirty" if x["dirty"] else ""}</span>' for x in m["inflight"][:12])
            more = f' +{len(m["inflight"]) - 12}' if len(m["inflight"]) > 12 else ""
            rows.append(f'<div class="row"><span class="k">in flight</span> {tags}{more}</div>')
        if m["blockers"]:
            rows.append('<div class="row bad">blocked: ' + e("; ".join(m["blockers"])) + "</div>")
        if m["handoff"]:
            rows.append('<div class="row"><span class="k">handoff</span><ul>' + "".join(
                f'<li>#{h["id"]} from {e(h["from"] or "?")}: {e(h["text"])}</li>' for h in m["handoff"]) + "</ul></div>")
        if m["notes"]:
            rows.append('<div class="row k">' + e(m["notes"][-1]) + "</div>")
        cards.append(f'<div class="card"><h2>{e(m["name"])}<span class="id">{e(m["id"])}</span></h2>'
                     f'<div class="ladder">{ladder}</div><div class="stg">stage {m["stage"]} · '
                     f'{e(d["stages"].get(str(m["stage"]), ""))}</div>{"".join(rows)}</div>')
    other = ""
    if d["unowned_locks"]:
        other += '<div class="row"><span class="k">locks held by unleased sessions:</span> ' + e(", ".join(d["unowned_locks"])) + "</div>"
    if d["flags"]["dirty"]:
        other += '<div class="row"><span class="k">dirty worktrees:</span> ' + e(", ".join(d["flags"]["dirty"])) + "</div>"
    if d["flags"]["empty"]:
        other += f'<div class="row"><span class="k">{len(d["flags"]["empty"])} empty worktrees/branches (nothing beyond base)</span></div>'
    page = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta http-equiv="refresh" content="60"><title>TrueNorth Coordinator</title><style>{CSS}</style></head><body>'
            f'<h1>TrueNorth coordinator</h1><div class="sub">generated {e(d["generated"])} · scan {e(d["scan_ts"] or "-")} '
            f'vs {e(d["base"] or "-")} · {e(d["root"])}</div>{other}<div class="grid">{"".join(cards)}</div></body></html>')
    tmp = DASH.with_suffix(".tmp")
    tmp.write_text(page, encoding="utf-8")
    os.replace(str(tmp), str(DASH))


def cmd_dashboard(a):
    c = cfg()
    con = db()
    data = cached_scan(c, con, 10 ** 9 if a.cached else a.max_age)
    d = dashboard_data(c, con, data)
    if a.json:
        print(json.dumps(d, indent=2))
        return
    write_dashboard(d)
    if not a.quiet:
        print(f"[coord] dashboard -> {DASH}")


# ---------------------------------------------------------------- usage / init
def cmd_usage(a):
    """Sum usage fields in Claude Code transcripts. Field names vary by version; best effort."""
    since = datetime.now(timezone.utc) - timedelta(days=a.days)
    files = glob.glob(os.path.expanduser("~/.claude/projects/**/*.jsonl"), recursive=True)
    agg = {}
    for f in files:
        if datetime.fromtimestamp(os.path.getmtime(f), timezone.utc) < since:
            continue
        with open(f, errors="ignore") as fh:
            for line in fh:
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                msg = j.get("message") or {}
                u = msg.get("usage") if isinstance(msg, dict) else None
                u = u or j.get("usage")
                if not isinstance(u, dict):
                    continue
                ts = (j.get("timestamp") or "")[:10] or "unknown"
                model = (msg.get("model") if isinstance(msg, dict) else None) or j.get("model") or "unknown"
                d = agg.setdefault((ts, model), [0, 0, 0, 0])
                d[0] += u.get("input_tokens", 0) or 0
                d[1] += u.get("output_tokens", 0) or 0
                d[2] += u.get("cache_read_input_tokens", 0) or 0
                d[3] += u.get("cache_creation_input_tokens", 0) or 0
    if not agg:
        print(f"[coord] no usage records found in {len(files)} transcript files")
        return
    rows = [[k[0], k[1][:28], f"{v[0]:,}", f"{v[1]:,}", f"{v[2]:,}", f"{v[3]:,}"] for k, v in sorted(agg.items())]
    print(fmt_table(rows, ["day", "model", "input", "output", "cache_read", "cache_write"]))
    tot = [sum(v[i] for v in agg.values()) for i in range(4)]
    print(f"\n{a.days}-day totals: in {tot[0]:,}  out {tot[1]:,}  cache_read {tot[2]:,}  cache_write {tot[3]:,}")
    print("Account caps are not visible here; /usage in Claude Code is authoritative.")


def cmd_init(a):
    db()
    checks = []
    s = ROOT / ".claude" / "settings.json"
    checks.append((".claude/settings.json has coord hooks", s.exists() and "coord.py" in s.read_text()))
    gi = ROOT / ".gitignore"
    checks.append((".gitignore covers .coord/state.sqlite", gi.exists() and ".coord/state.sqlite" in gi.read_text()))
    cm = ROOT / "CLAUDE.md"
    checks.append(("CLAUDE.md includes .coord/CLAUDE.coord.md", cm.exists() and "@.coord/CLAUDE.coord.md" in cm.read_text()))
    checks.append(("modules.json found at main checkout", STATE_DIR == ROOT / ".coord"))
    for name, ok in checks:
        print(f"  {'ok  ' if ok else 'MISS'} {name}")
    print(f"[coord] state db -> {DB}")


# ---------------------------------------------------------------- main
def main(argv=None):
    p = argparse.ArgumentParser(prog="coord")
    s = p.add_subparsers(dest="cmd")
    s.required = True
    s.add_parser("init").set_defaults(f=cmd_init)
    x = s.add_parser("status")
    x.add_argument("--rescan", action="store_true")
    x.set_defaults(f=cmd_status)
    s.add_parser("modules").set_defaults(f=cmd_modules)
    s.add_parser("next").set_defaults(f=cmd_next)
    s.add_parser("locks").set_defaults(f=cmd_locks)
    m = s.add_parser("module")
    ms = m.add_subparsers(dest="sub")
    ms.required = True
    x = ms.add_parser("set")
    x.add_argument("id")
    x.add_argument("--stage", type=int)
    x.add_argument("--note")
    x.add_argument("--blocker")
    x.add_argument("--clear-blockers", action="store_true")
    x.set_defaults(f=cmd_module_set)
    x = s.add_parser("claim")
    x.add_argument("module")
    x.add_argument("--dir")
    x.set_defaults(f=cmd_claim)
    x = s.add_parser("release")
    x.add_argument("--dir")
    x.set_defaults(f=cmd_release)
    se = s.add_parser("session")
    ss = se.add_subparsers(dest="sub")
    ss.required = True
    x = ss.add_parser("start")
    x.add_argument("module")
    x.add_argument("--dir")
    x.set_defaults(f=cmd_claim)
    x = ss.add_parser("end")
    x.add_argument("--dir")
    x.set_defaults(f=cmd_release)
    ss.add_parser("list").set_defaults(f=cmd_session_list)
    x = s.add_parser("gate")
    x.add_argument("module")
    x.add_argument("--stage", type=int)
    x.set_defaults(f=cmd_gate)
    x = s.add_parser("checkpoint")
    x.add_argument("--quiet", action="store_true")
    x.add_argument("--force", action="store_true")
    x.add_argument("-m", "--message")
    x.set_defaults(f=cmd_checkpoint)
    h = s.add_parser("handoff")
    hs = h.add_subparsers(dest="sub")
    hs.required = True
    x = hs.add_parser("add")
    x.add_argument("target")
    x.add_argument("text")
    x.add_argument("--why")
    x.add_argument("--from", dest="from_module")
    x.set_defaults(f=cmd_handoff)
    x = hs.add_parser("list")
    x.add_argument("--all", action="store_true")
    x.add_argument("--module")
    x.set_defaults(f=cmd_handoff)
    x = hs.add_parser("done")
    x.add_argument("id", type=int)
    x.add_argument("--note")
    x.set_defaults(f=cmd_handoff)
    x = s.add_parser("scan")
    x.add_argument("--json", action="store_true")
    x.add_argument("--no-branches", action="store_true")
    x.set_defaults(f=cmd_scan)
    x = s.add_parser("dashboard")
    x.add_argument("--json", action="store_true")
    x.add_argument("--cached", action="store_true")
    x.add_argument("--quiet", action="store_true")
    x.add_argument("--max-age", type=float, default=0)
    x.set_defaults(f=cmd_dashboard)
    x = s.add_parser("usage")
    x.add_argument("--days", type=int, default=7)
    x.set_defaults(f=cmd_usage)
    s.add_parser("guard").set_defaults(f=cmd_guard)
    s.add_parser("session-start").set_defaults(f=cmd_session_start_hook)
    a = p.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
