#!/usr/bin/env python3
"""Build a runner-attested result-v2 bundle from a completed Taz worktree."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "scripts" / "taz_guard.py"
POLICY = ROOT / "tools" / "taz" / "hooks" / "policy.json"
MAX_OUTPUT = 20_000


class BundleError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")  # noqa: UP017 -- Python 3.9


def read_json(path, label):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError(f"invalid {label}: {exc}") from exc


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(command, cwd, *, shell=False, timeout=3600):
    try:
        return subprocess.run(command, cwd=str(cwd), shell=shell, text=True,
                              capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BundleError(str(exc)) from exc


def git(repo, *args, check=True):
    result = run(["git", *args], repo)
    if check and result.returncode:
        raise BundleError((result.stderr or result.stdout).strip())
    return result


def verify_input_checksums(card_path):
    sums = card_path.parent / "checksums.sha256"
    if not sums.exists():
        raise BundleError("worker package has no checksums.sha256")
    expected = {}
    for line in sums.read_text(encoding="utf-8").splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            expected[parts[1].strip()] = parts[0]
    for name in ("card-v2.json", "WORKER.md"):
        path = card_path.parent / name
        if expected.get(name) != sha256(path):
            raise BundleError(f"worker package checksum failed for {name}")


def validate_inputs(card, model, review):
    if card.get("schema_version") != 2:
        raise BundleError("card schema_version must be 2")
    for key in ("id", "base_commit", "allowed_paths", "oracle_tests", "oracle_commands", "diff_budget"):
        if card.get(key) in (None, "", []):
            raise BundleError(f"card missing {key}")
    if not isinstance(model, dict) or not model.get("id") or not model.get("runtime") or not model.get("source"):
        raise BundleError("served-model JSON requires id, runtime, and independent observation source")
    if not isinstance(review, dict) or not isinstance(review.get("limitations"), list):
        raise BundleError("self-review JSON requires a limitations list")
    confidence = review.get("confidence")
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise BundleError("self-review confidence must be between 0 and 1")


def allowed_oracle(argv):
    name = Path(argv[0]).name
    if name.startswith("python"):
        return "-c" not in argv and (
            len(argv) >= 3 and argv[1:3] in (["-m", "pytest"], ["-m", "ruff"])
            or len(argv) >= 2 and argv[1].startswith(("tests/", "scripts/check_"))
        )
    return (
        name in ("pytest", "ruff")
        or name == "bash" and argv[1:] == ["scripts/dod.sh"]
        or name == "npm" and argv[1:] in (["test"], ["run", "test"], ["run", "build"])
        or name in ("ng", "npx") and any(x in argv[1:] for x in ("test", "lint", "build"))
    )


def run_oracles(card, repo):
    results = []
    for command in card["oracle_commands"]:
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise BundleError(f"invalid oracle command: {exc}") from exc
        if not argv or any(any(ch in token for ch in ("\n", "\r", "\x00")) for token in argv):
            raise BundleError("oracle command is empty or contains control characters")
        if not allowed_oracle(argv):
            raise BundleError(f"oracle command is not an allowed test runner: {command}")
        started = datetime.now(timezone.utc)  # noqa: UP017 -- Python 3.9
        result = run(argv, repo)
        duration = (datetime.now(timezone.utc) - started).total_seconds()  # noqa: UP017 -- Python 3.9
        combined = ((result.stdout or "") + (result.stderr or ""))[-MAX_OUTPUT:]
        results.append({"command": command, "exit_code": result.returncode,
                        "duration_seconds": round(duration, 3), "output_tail": combined})
    return {"passed": all(item["exit_code"] == 0 for item in results), "commands": results}


def local_commit(card, repo):
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    if head != card["base_commit"]:
        raise BundleError(f"worktree HEAD {head} is not card base {card['base_commit']}")
    if not git(repo, "status", "--porcelain").stdout.strip():
        raise BundleError("worktree contains no candidate changes")
    git(repo, "add", "-A")
    result = git(repo, "-c", "user.name=Taz Candidate Runner",
                 "-c", "user.email=taz-runner@invalid", "-c", "commit.gpgsign=false",
                 "commit", "--no-verify", "-m", f"taz candidate {card['id']}", check=False)
    if result.returncode:
        raise BundleError((result.stderr or result.stdout).strip())
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def guard(card, repo, head, destination, model):
    guard_card = {
        "allowed_paths": card["allowed_paths"], "oracle_tests": card["oracle_tests"],
        "diff_budget": card["diff_budget"], "base_ref": card["base_commit"],
    }
    card_path = destination / "guard-card.json"
    card_path.write_text(json.dumps(guard_card, indent=2) + "\n", encoding="utf-8")
    command = [sys.executable, str(GUARD), "--repo-dir", str(repo), "--base", card["base_commit"],
               "--head", head, "--card", str(card_path), "--policy", str(POLICY)]
    result = run(command, repo)
    card_path.unlink()
    return {"passed": result.returncode == 0, "exit_code": result.returncode,
            "served_model": model, "command": shlex.join(command),
            "output_tail": ((result.stdout or "") + (result.stderr or ""))[-MAX_OUTPUT:]}


def build(args):
    card_path = Path(args.card).resolve()
    repo = Path(args.repo).resolve()
    output = Path(args.output).resolve()
    verify_input_checksums(card_path)
    card = read_json(card_path, "card-v2.json")
    model = read_json(args.served_model_json, "served-model JSON")
    review = read_json(args.self_review_json, "self-review JSON")
    validate_inputs(card, model, review)
    if not (repo / ".git").exists() and not git(repo, "rev-parse", "--git-dir", check=False).returncode == 0:
        raise BundleError(f"not a Git worktree: {repo}")
    if output.exists():
        raise BundleError(f"output already exists: {output}")

    tests = run_oracles(card, repo)
    head = local_commit(card, repo)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="result-v2-", dir=str(output.parent)) as tmp:
        staging = Path(tmp)
        patch = staging / "candidate.patch"
        patch.write_bytes(git(repo, "diff", "--binary", card["base_commit"], head).stdout.encode())
        files = [line for line in git(repo, "diff", "--name-only", card["base_commit"], head).stdout.splitlines() if line]
        (staging / "diff-stat.txt").write_text(
            git(repo, "diff", "--stat", card["base_commit"], head).stdout, encoding="utf-8"
        )
        guard_result = guard(card, repo, head, staging, model)
        (staging / "test-results.json").write_text(json.dumps(tests, indent=2) + "\n", encoding="utf-8")
        (staging / "taz-guard.json").write_text(json.dumps(guard_result, indent=2) + "\n", encoding="utf-8")
        manifest = {
            "schema_version": 2, "card_id": card["id"], "worker": args.worker_id,
            "served_model": model, "base_commit": card["base_commit"], "head_commit": head,
            "patch_sha256": sha256(patch), "files_changed": files,
            "summary": review.get("summary") or "Taz candidate returned by the supervised runner.",
            "self_review": {"limitations": review["limitations"], "confidence": review["confidence"]},
            "produced_at": now(),
        }
        (staging / "result-v2.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.replace(staging, output)

    archive = None
    if args.archive:
        archive = output.with_suffix(".tar.gz")
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(output, arcname=output.name)
    print(json.dumps({"card_id": card["id"], "output": str(output),
                      "archive": str(archive) if archive else None,
                      "tests_passed": tests["passed"], "guard_passed": guard_result["passed"]}))
    return 0 if tests["passed"] and guard_result["passed"] else 2


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--card", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--served-model-json", required=True)
    parser.add_argument("--self-review-json", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--archive", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    try:
        return build(parse_args(argv))
    except BundleError as exc:
        print(f"taz exchange result: ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
