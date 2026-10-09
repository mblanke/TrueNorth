"""``python -m arc2.runner --self-test``: could a job run here, without spending anything?

The installer (install/roles/tn_arc2) runs it before the runner service starts, so a host
whose sandbox cannot be built fails at install time instead of failing every job. A fake
``claude`` stands in for Claude Code: the self-test must call it with ``--version`` only (no
prompt, no model), inside the sandbox a job would get, and touch nothing in the runs root.
"""

from __future__ import annotations

import json
import stat

import pytest
from arc2 import confine, runner

FAKE = r"""#!/usr/bin/env python3
import json, os, sys
open(os.environ["FAKE_ARGS"], "w").write(json.dumps({"argv": sys.argv[1:], "home": os.environ.get("HOME"),
    "proxy": os.environ.get("HTTPS_PROXY"), "stdin": sys.stdin.read() if not sys.stdin.isatty() else ""}))
if os.environ.get("FAKE_MODE") == "broken":
    print("Error: cannot execute binary", file=sys.stderr)
    sys.exit(126)
print("2.1.286 (Claude Code)")
"""

# Every sandbox this host can build, plus `none`, which every host has.
BACKENDS = ["none", *(n for n in ("seatbelt", "bubblewrap") if confine.BACKENDS[n]().available())]


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text(FAKE)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    args = tmp_path / "fake-args.json"
    monkeypatch.setenv("FAKE_ARGS", str(args))
    monkeypatch.setenv("ARC2_JOB_ENV", "FAKE_ARGS,FAKE_MODE")  # the fake's own settings reach it
    monkeypatch.setenv("ARC2_JOB_HOMES", str(tmp_path / "homes"))
    return exe, args


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_engine_runs_in_the_jobs_sandbox_and_nothing_is_spent(tmp_path, fake_claude, monkeypatch, capsys, backend):
    exe, args = fake_claude
    monkeypatch.setenv("ARC2_CONFINE", backend)
    runs = tmp_path / "runs"
    runs.mkdir()
    # A confined engine may write nothing outside its own home, so there the fake reports
    # to /dev/null (its command line is checked in the next test).
    if backend != "none":
        monkeypatch.setenv("FAKE_ARGS", "/dev/null")
        args = None
    assert runner.main(["--runs", str(runs), "--claude", str(exe), "--self-test"]) == 0
    out = capsys.readouterr().out
    assert "self-test passed" in out and f"confinement: {backend}" in out and "2.1.286 (Claude Code)" in out
    assert list(runs.iterdir()) == [], "the self-test reads and writes nothing in the runs root"
    homes = tmp_path / "homes"
    assert not homes.exists() or not any(p.name.startswith("self-test-") for p in homes.iterdir())
    if args is not None:
        seen = json.loads(args.read_text())
        assert seen["argv"] == ["--version"], "only --version: no prompt, no model call"


def test_a_confined_engine_gets_a_fresh_home_and_the_egress_proxy(tmp_path, fake_claude, monkeypatch, capsys):
    exe, _ = fake_claude
    monkeypatch.setenv("ARC2_CONFINE", "auto")
    monkeypatch.setattr(confine.Seatbelt, "available", lambda self: True)
    monkeypatch.setattr(confine.Bubblewrap, "available", lambda self: False)
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, env=kw["env"], cwd=kw["cwd"])
        return runner.subprocess.CompletedProcess(cmd, 0, "2.1.286 (Claude Code)\n", "")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    assert runner.main(["--runs", str(tmp_path / "runs"), "--claude", str(exe), "--self-test"]) == 0
    assert seen["cmd"][0] == "sandbox-exec" and seen["cmd"][-2:] == [str(exe), "--version"]
    assert seen["env"]["HOME"].startswith(str(tmp_path / "homes" / "self-test-"))
    assert seen["env"]["HTTPS_PROXY"].startswith("http://127.0.0.1:")
    assert "egress: api.anthropic.com" in capsys.readouterr().out


def test_without_a_sandbox_the_self_test_fails_closed(tmp_path, fake_claude, monkeypatch, capsys):
    exe, args = fake_claude
    monkeypatch.setenv("ARC2_CONFINE", "auto")
    for backend in (confine.Seatbelt, confine.Bubblewrap):
        monkeypatch.setattr(backend, "available", lambda self: False)
    assert runner.main(["--runs", str(tmp_path / "runs"), "--claude", str(exe), "--self-test"]) == 2
    assert "self-test failed: no OS sandbox" in capsys.readouterr().err
    assert not args.exists(), "the engine never ran"


# The installer (install/roles/tn_arc2) sets exactly one of these in the service's
# EnvironmentFile: a Claude subscription token (`claude setup-token`) or an API key.
AUTH_FAKE = r"""#!/usr/bin/env python3
import os
seen = ",".join(k for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY") if os.environ.get(k))
print(f"2.1.286 (Claude Code) auth={seen or 'none'}")
"""
AUTH_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("auth", AUTH_ENV)
def test_the_self_test_passes_with_either_credential_and_the_engine_gets_it(
        tmp_path, fake_claude, monkeypatch, capsys, backend, auth):
    """Run for real in every sandbox this host has: the engine sees the one credential, by
    the same name, and the self-test (no model call) passes either way."""
    exe, _ = fake_claude
    exe.write_text(AUTH_FAKE)
    for name in AUTH_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(auth, "sk-ant-test-value")
    monkeypatch.setenv("ARC2_OAUTH_TOKEN_FILE", str(tmp_path / "no-token-file"))
    monkeypatch.setenv("ARC2_CONFINE", backend)
    runs = tmp_path / "runs"
    runs.mkdir()
    assert runner.main(["--runs", str(runs), "--claude", str(exe), "--self-test"]) == 0
    out = capsys.readouterr().out
    assert "self-test passed" in out and f"auth={auth}" in out
    assert "sk-ant-test-value" not in out, "the self-test never prints the credential"


@pytest.mark.parametrize("auth", AUTH_ENV)
def test_on_the_platform_host_either_credential_reaches_the_bubblewrap_job(tmp_path, fake_claude, monkeypatch, auth):
    """The service's path on Linux (bubblewrap, ARC2_JOB_ENV as the unit sets it), simulated
    so it runs on any host: the runner's re-exec keeps the credential, and the job gets it
    without naming it in ARC2_JOB_ENV, exactly as on macOS (runner.AUTH_ENV)."""
    exe, _ = fake_claude
    unit_env = {
        "PATH": "/opt/truenorth-arc2/bin:/usr/bin:/bin", "HOME": "/var/lib/tn-arc2", "LANG": "C.UTF-8",
        "PYTHONPATH": "/srv/truenorth/app/tools", "ARC2_CONFINE": "auto", "ARC2_EGRESS": "proxy",
        "ARC2_FALLBACK": "off", "ARC2_JOB_HOMES": str(tmp_path / "homes"),
        "ARC2_OAUTH_TOKEN_FILE": str(tmp_path / "no-token-file"),
        "ARC2_JOB_ENV": "DISABLE_AUTOUPDATER,GIT_CONFIG_COUNT,GIT_CONFIG_KEY_0,GIT_CONFIG_VALUE_0",
        "DISABLE_AUTOUPDATER": "1", "INVOCATION_ID": "abc", auth: "sk-ant-test-value",
    }
    kept = runner.clean_runner_env(unit_env)
    assert kept[auth] == "sk-ant-test-value" and "INVOCATION_ID" not in kept
    for name in list(runner.os.environ):
        if name in AUTH_ENV or name.startswith("ARC2_"):
            monkeypatch.delenv(name)
    for name, value in kept.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ARC2_JOB_ENV", kept["ARC2_JOB_ENV"])  # the fake's own settings are not needed here
    monkeypatch.setattr(confine.Seatbelt, "available", lambda self: False)
    monkeypatch.setattr(confine.Bubblewrap, "available", lambda self: True)
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, env=kw["env"])
        return runner.subprocess.CompletedProcess(cmd, 0, "2.1.286 (Claude Code)\n", "")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    assert runner.main(["--runs", str(tmp_path / "runs"), "--claude", str(exe), "--self-test"]) == 0
    assert seen["cmd"][0] == "bwrap"
    other = next(n for n in AUTH_ENV if n != auth)
    assert seen["env"][auth] == "sk-ant-test-value" and other not in seen["env"]
    assert "INVOCATION_ID" not in seen["env"]


@pytest.mark.parametrize("mode", ["broken", "missing"])
def test_an_engine_that_cannot_run_fails_the_self_test(tmp_path, fake_claude, monkeypatch, capsys, mode):
    exe, _ = fake_claude
    monkeypatch.setenv("ARC2_CONFINE", "none")
    monkeypatch.setenv("FAKE_MODE", mode)
    claude = str(tmp_path / "no-such-claude") if mode == "missing" else str(exe)
    assert runner.main(["--runs", str(tmp_path / "runs"), "--claude", claude, "--self-test"]) == 2
    err = capsys.readouterr().err
    assert "self-test failed" in err and ("126" in err or "could not run" in err)
