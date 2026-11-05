"""End-to-end tests for the evidence-first Claude/Codex/Taz card exchange."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from test_coord import Repo

RUNNER = Path(__file__).resolve().parents[2] / "tools" / "taz" / "exchange_result.py"


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _card(repo, tmp_path, card_id="T-1", state="oracle-ready", budget=50):
    base = repo.git(repo.main, "rev-parse", "HEAD")
    path = tmp_path / f"{card_id}.json"
    _write_json(path, {
        "id": card_id,
        "issue_number": 999,
        "title": "Bounded alpha change",
        "state": state,
        "risk": "medium",
        "base_ref": "main",
        "base_commit": base,
        "allowed_paths": ["src/alpha/**"],
        "oracle_tests": ["tests/test_alpha.py"],
        "oracle_commands": ["python3 tests/test_alpha.py"],
        "required_checks": ["dod"],
        "diff_budget": budget,
    })
    result = repo.coord(repo.main, "card", "import", str(path), "--json")
    assert result.returncode == 0, result.stderr
    return base


def _bundle(repo, tmp_path, card_id, base, changed_path="src/alpha/core.py", guard=True,
            tests=True, claimed_path=None):
    wt = repo.wt_a
    repo.write(wt, changed_path, "A = 2\n")
    patch_text = repo.git(wt, "diff", "--binary", "--", changed_path) + "\n"
    bundle = tmp_path / f"bundle-{card_id}"
    bundle.mkdir()
    patch = bundle / "candidate.patch"
    patch.write_text(patch_text, encoding="utf-8")
    digest = hashlib.sha256(patch.read_bytes()).hexdigest()
    manifest_path = claimed_path or changed_path
    _write_json(bundle / "result-v2.json", {
        "schema_version": 2,
        "card_id": card_id,
        "worker": "taz-glm-5.3",
        "served_model": {"id": "glm-5.3", "runtime": "vllm"},
        "base_commit": base,
        "head_commit": None,
        "patch_sha256": digest,
        "files_changed": [manifest_path],
        "summary": "small candidate",
        "self_review": {"limitations": [], "confidence": 0.72},
    })
    _write_json(bundle / "test-results.json", {
        "passed": tests,
        "commands": [{"command": "python3 tests/test_alpha.py", "exit_code": 0 if tests else 1}],
    })
    _write_json(bundle / "taz-guard.json", {
        "passed": guard, "served_model": {"id": "glm-5.3", "runtime": "vllm"},
    })
    (bundle / "diff-stat.txt").write_text("1 file changed, 1 insertion, 1 deletion\n", encoding="utf-8")
    return bundle


def test_candidate_requires_dual_review_and_ci(tmp_path):
    (tmp_path / "repo").mkdir()
    repo = Repo(tmp_path / "repo")
    base = _card(repo, tmp_path)
    export = repo.coord(repo.main, "card", "export", "T-1", str(tmp_path / "out"), "--archive", "--json")
    assert export.returncode == 0, export.stderr
    exported = json.loads(export.stdout)
    assert Path(exported["archive"]).is_file()
    worker_card = json.loads((Path(exported["package"]) / "card-v2.json").read_text())
    assert worker_card["autonomy"] == "candidate-only"

    bundle = _bundle(repo, tmp_path, "T-1", base)
    ingest = repo.coord(repo.main, "card", "ingest", "T-1", str(bundle), "--json")
    assert ingest.returncode == 0, ingest.stderr
    assert json.loads(ingest.stdout)["state"] == "guard-green"

    premature = repo.coord(repo.main, "card", "ci", "T-1", "--status", "green")
    assert premature.returncode != 0 and "ci-pending" in premature.stderr
    claude = repo.coord(repo.main, "card", "review", "T-1", "--reviewer", "claude",
                        "--decision", "approve", "--note", "implementation review clean", "--json")
    assert claude.returncode == 0 and json.loads(claude.stdout)["state"] == "independent-review"
    codex = repo.coord(repo.main, "card", "review", "T-1", "--reviewer", "codex",
                       "--decision", "approve", "--note", "risk review clean", "--json")
    assert codex.returncode == 0 and json.loads(codex.stdout)["state"] == "ci-pending"
    ci = repo.coord(repo.main, "card", "ci", "T-1", "--status", "green",
                    "--run-url", "https://ci.invalid/1", "--json")
    assert ci.returncode == 0 and json.loads(ci.stdout)["state"] == "ci-green"
    merged = repo.coord(repo.main, "card", "set-state", "T-1", "merged", "--actor", "owner", "--json")
    assert merged.returncode == 0 and json.loads(merged.stdout)["state"] == "merged"
    metrics = json.loads(repo.coord(repo.main, "card", "metrics").stdout)
    assert metrics["first_pass_acceptance_rate"] == 1.0
    assert metrics["guard_pass_rate"] == 1.0


def test_invalid_candidate_is_preserved_and_rejected(tmp_path):
    (tmp_path / "repo").mkdir()
    repo = Repo(tmp_path / "repo")
    base = _card(repo, tmp_path)
    assert repo.coord(repo.main, "card", "set-state", "T-1", "dispatched").returncode == 0
    bundle = _bundle(repo, tmp_path, "T-1", base, guard=False)
    result = repo.coord(repo.main, "card", "ingest", "T-1", str(bundle), "--json")
    assert result.returncode != 0
    shown = json.loads(repo.coord(repo.main, "card", "show", "T-1").stdout)
    assert shown["state"] == "rejected"
    assert shown["attempts"] == 1
    assert shown["results"][0]["status"] == "invalid"
    assert any("passed=true" in error for error in shown["results"][0]["errors"])


def test_backpressure_and_durable_watch_cursor(tmp_path):
    (tmp_path / "repo").mkdir()
    repo = Repo(tmp_path / "repo")
    for index in range(1, 5):
        _card(repo, tmp_path, card_id=f"T-{index}")
    for index in range(1, 4):
        assert repo.coord(repo.main, "card", "set-state", f"T-{index}", "dispatched").returncode == 0
        assert repo.coord(repo.main, "card", "set-state", f"T-{index}", "candidate-returned").returncode == 0
    ready = json.loads(repo.coord(repo.main, "card", "ready", "--json").stdout)
    assert ready["dispatch_allowed"] is False
    assert ready["review_queue"] == 3
    assert ready["cards"] == []

    first = json.loads(repo.coord(repo.main, "card", "watch", "--consumer", "codex").stdout)
    assert first["events"]
    second = json.loads(repo.coord(repo.main, "card", "watch", "--consumer", "codex").stdout)
    assert second["events"] == []


def test_vpn_profile_is_local_and_rejects_shell_syntax(tmp_path):
    (tmp_path / "repo").mkdir()
    repo = Repo(tmp_path / "repo")
    made = repo.coord(repo.main, "bridge", "init", "--target", "taz-worker@taz-vpn",
                      "--remote-root", "/srv/truenorth-exchange")
    assert made.returncode == 0, made.stderr
    profile = repo.main / ".coord" / "transport.local.json"
    assert profile.is_file()
    assert profile.stat().st_mode & 0o777 == 0o600
    assert json.loads(profile.read_text())["ssh_target"] == "taz-worker@taz-vpn"

    bad = repo.coord(repo.main, "bridge", "init", "--target", "taz;touch-pwned",
                     "--remote-root", "/srv/exchange", "--force")
    assert bad.returncode != 0
    assert "plain [user@]host" in bad.stderr


def test_card_import_rejects_forged_completion_and_unsafe_scope(tmp_path):
    (tmp_path / "repo").mkdir()
    repo = Repo(tmp_path / "repo")
    base = repo.git(repo.main, "rev-parse", "HEAD")
    forged = tmp_path / "forged.json"
    _write_json(forged, {
        "id": "T-unsafe", "title": "forged", "state": "merged", "base_commit": base,
        "allowed_paths": ["../outside/**"], "oracle_tests": ["tests/test_alpha.py"],
        "oracle_commands": ["python3 tests/test_alpha.py"],
    })
    result = repo.coord(repo.main, "card", "import", str(forged))
    assert result.returncode != 0
    assert "repository-relative" in result.stderr

    payload = json.loads(forged.read_text())
    payload["allowed_paths"] = ["src/alpha/**"]
    _write_json(forged, payload)
    result = repo.coord(repo.main, "card", "import", str(forged))
    assert result.returncode != 0
    assert "start only" in result.stderr


def test_supervised_taz_runner_builds_ingestible_bundle(tmp_path):
    (tmp_path / "repo").mkdir()
    repo = Repo(tmp_path / "repo")
    _card(repo, tmp_path)
    export = repo.coord(repo.main, "card", "export", "T-1", str(tmp_path / "out"), "--json")
    package = Path(json.loads(export.stdout)["package"])
    repo.write(repo.wt_a, "src/alpha/core.py", "A = 3\n")
    model = tmp_path / "served-model.json"
    review = tmp_path / "self-review.json"
    _write_json(model, {"id": "glm-5.3", "runtime": "vllm", "source": "runner /v1/models probe"})
    _write_json(review, {"summary": "bounded alpha update", "limitations": [], "confidence": 0.7})
    result_dir = tmp_path / "T-1-result"
    result = subprocess.run([
        sys.executable, str(RUNNER), "--card", str(package / "card-v2.json"),
        "--repo", str(repo.wt_a), "--output", str(result_dir),
        "--served-model-json", str(model), "--self-review-json", str(review),
        "--worker-id", "taz-test", "--archive",
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout
    assert result_dir.with_suffix(".tar.gz").is_file()
    ingest = repo.coord(repo.main, "card", "ingest", "T-1", str(result_dir), "--json")
    assert ingest.returncode == 0, ingest.stderr
    assert json.loads(ingest.stdout)["state"] == "guard-green"
