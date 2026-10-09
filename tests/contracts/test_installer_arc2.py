"""Installer contracts for ARC² Course Studio (install/roles/tn_arc2, playbooks/55-arc2.yml).

ARC² is optional and off by default. On, the installer runs tools/arc2/runner.py on the
platform host as a hardened systemd service whose jobs are bubblewrap-sandboxed, with a
pinned Claude Code and Node, the model key only in a root 0600 EnvironmentFile, and the api
wired to the runs directory. Off, nothing is installed and the api has no ARC² setting and
no host mount. Each test pins one of those properties (docs/arc2-course-studio.md
"Deployment"; install/README.md "ARC² Course Studio (optional)").
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install"
ROLE = INSTALL / "roles/tn_arc2"
DOCKER = ROOT / "infra/platform/docker"
KEY = "vault_arc2_anthropic_api_key"


def _yaml(path: Path):
    return yaml.safe_load(path.read_text())


def _group_vars() -> dict:
    return _yaml(INSTALL / "inventory/group_vars/all/main.yml")


def _defaults() -> dict:
    return _yaml(ROLE / "defaults/main.yml")


def _walk(items):
    for t in items or []:
        yield t
        for section in ("block", "rescue", "always"):
            yield from _walk(t.get(section))


def _tasks() -> list[dict]:
    return list(_walk(_yaml(ROLE / "tasks/install.yml")))


def _module(task: dict, name: str):
    return task.get(f"ansible.builtin.{name}")


def _argv(task: dict) -> list:
    cmd = _module(task, "command")
    return cmd.get("argv") or [] if isinstance(cmd, dict) else []


def _code(text: str) -> str:
    """The text without its comment lines (a variable named in a comment is not a use)."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _jinja() -> jinja2.Environment:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    env.filters.update(basename=os.path.basename, bool=lambda v: str(v).lower() in ("1", "true", "yes", "on"))
    return env


def _unit(**overrides) -> dict[str, list[str]]:
    """The rendered service unit as {key: [values]} (repeated keys keep every value)."""
    d = _defaults()
    ctx = {
        "ansible_managed": "Ansible managed",
        "tn_app_dir": "/srv/truenorth/app",
        "tn_arc2_runs_dir": "/srv/truenorth/arc2/runs",
        "tn_arc2_anthropic_base_url": "",
        "tn_arc2_egress_allow": "api.anthropic.com",
        "tn_arc2_inaccessible_paths": ["/srv/truenorth/config", "/srv/truenorth/tls", "/var/lib/docker"],
        "tn_arc2_ip_deny": ["10.0.0.0/8", "192.168.0.0/16"],
        **{k: d[k] for k in ("tn_arc2_user", "tn_arc2_home", "tn_arc2_runs_group", "tn_arc2_service",
                             "tn_arc2_job_timeout", "tn_arc2_memory_max", "tn_arc2_cpu_weight", "tn_arc2_tasks_max")},
        "tn_arc2_prefix": "/opt/truenorth-arc2",
        "tn_arc2_venv": "/opt/truenorth-arc2/venv",
        "tn_arc2_claude_dir": "/opt/truenorth-arc2/claude-code",
        "tn_arc2_claude_bin": "/opt/truenorth-arc2/claude-code/node_modules/.bin/claude",
        "tn_arc2_env_file": "/etc/truenorth-arc2/runner.env",
        "tn_arc2_repo_runs": "/srv/truenorth/app/build/arc2",
        "tn_arc2_repo_venv": "/srv/truenorth/app/.venv",
        **overrides,
    }
    text = _jinja().from_string((ROLE / "templates/truenorth-arc2-runner.service.j2").read_text()).render(ctx)
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        if line and not line.startswith(("#", "[")) and "=" in line:
            key, _, value = line.partition("=")
            out.setdefault(key, []).append(value)
    return out


def _one(unit: dict, key: str) -> str:
    assert len(unit.get(key, [])) == 1, f"{key}: {unit.get(key)}"
    return unit[key][0]


def _envs(unit: dict) -> dict[str, str]:
    return dict(v.split("=", 1) for v in unit["Environment"])


# ── Off by default, and off means nothing ─────────────────────────────
def test_arc2_is_off_by_default_and_gates_the_whole_role():
    assert _group_vars()["tn_arc2_enabled"] is False
    main = _yaml(ROLE / "tasks/main.yml")
    include = next(t for t in main if "ansible.builtin.include_tasks" in t)
    assert include["ansible.builtin.include_tasks"] == "install.yml"
    assert include["when"] == "tn_arc2_enabled | bool"
    assert all("ansible.builtin.debug" in t or t is include for t in main), "nothing else runs when off"
    block = _yaml(ROLE / "tasks/install.yml")
    assert len(block) == 1 and block[0]["become"] is True and "block" in block[0]


def test_the_runner_is_installed_after_the_stack_is_up():
    imports = [entry["import_playbook"] for entry in _yaml(INSTALL / "site.yml")]
    assert imports.index("playbooks/50-stack-up.yml") < imports.index("playbooks/55-arc2.yml")
    play = _yaml(INSTALL / "playbooks/55-arc2.yml")[0]
    assert [r["role"] for r in play["roles"]] == ["tn_arc2"]


# ── Pinned versions ───────────────────────────────────────────────────
def test_claude_code_is_pinned_exactly_and_installed_from_the_lockfile():
    version = _group_vars()["tn_arc2_claude_code_version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    pkg = json.loads((ROLE / "files/claude-code/package.json").read_text())
    assert pkg["dependencies"] == {"@anthropic-ai/claude-code": version}, "exact, no range"
    lock = json.loads((ROLE / "files/claude-code/package-lock.json").read_text())["packages"]
    main = lock["node_modules/@anthropic-ai/claude-code"]
    assert main["version"] == version and main["integrity"].startswith("sha512-")
    natives = {k: v for k, v in lock.items() if k.startswith("node_modules/@anthropic-ai/claude-code-")}
    assert {"node_modules/@anthropic-ai/claude-code-linux-x64", "node_modules/@anthropic-ai/claude-code-linux-arm64"} <= set(natives)
    assert all(v["version"] == version and v["integrity"].startswith("sha512-") for v in natives.values())
    tasks = _tasks()
    npm = next(t for t in tasks if "/bin/npm" in str(_argv(t)))
    assert "'ci'" in npm["ansible.builtin.command"]["argv"], "npm ci: the lockfile, never a fresh resolve"
    check = next(t for t in tasks if (_module(t, "assert") or {}).get("that", "") ==
                 "tn_arc2_claude_now.stdout.startswith(tn_arc2_claude_code_version ~ ' ')")
    assert check


def test_node_is_pinned_and_checked_against_its_sha256():
    assert re.fullmatch(r"\d+\.\d+\.\d+", _group_vars()["tn_arc2_node_version"])
    sums = _defaults()["tn_arc2_node_sha256"]
    assert set(sums) == {"x64", "arm64"} and all(re.fullmatch(r"[0-9a-f]{64}", v) for v in sums.values())
    get = next(t for t in _tasks() if _module(t, "get_url"))
    assert get["ansible.builtin.get_url"]["checksum"] == "sha256:{{ tn_arc2_node_sha256[tn_arc2_node_arch] }}"


def test_the_engine_venv_pins_what_the_test_environment_pins():
    extra = _defaults()["tn_arc2_venv_extra"]
    reqs = (ROOT / "requirements-test.txt").read_text()
    pytest_pin = next(x for x in extra if x.startswith("pytest=="))
    assert re.search(rf"^{re.escape(pytest_pin)}\b", reqs, re.M)
    assert "ruff==0.16.3" in extra and "ruff==0.16.3" in (ROOT / ".github/workflows/ci.yml").read_text()


# ── The service ───────────────────────────────────────────────────────
def test_the_runner_service_is_hardened():
    unit = _unit()
    for key, want in {
        "User": "tn-arc2", "NoNewPrivileges": "yes", "ProtectSystem": "strict", "ProtectHome": "yes",
        "PrivateTmp": "yes", "PrivateDevices": "yes", "PrivateIPC": "yes", "CapabilityBoundingSet": "",
        "AmbientCapabilities": "", "RestrictSUIDSGID": "yes", "LockPersonality": "yes", "ProtectKernelModules": "yes",
        "ProtectControlGroups": "yes", "ProtectClock": "yes", "ProtectProc": "invisible", "UMask": "0027",
        "SystemCallArchitectures": "native", "KeyringMode": "private",
    }.items():
        assert _one(unit, key) == want, key
    # Writable: the runs directory (and the account's own StateDirectory), nothing else.
    assert _one(unit, "ReadWritePaths") == "/srv/truenorth/arc2/runs"
    assert _one(unit, "StateDirectory") == "tn-arc2"
    assert _one(unit, "BindPaths") == "/srv/truenorth/arc2/runs:/srv/truenorth/app/build/arc2"
    assert _one(unit, "BindReadOnlyPaths") == "/opt/truenorth-arc2/venv:/srv/truenorth/app/.venv"
    assert "-/srv/truenorth/config" in _one(unit, "InaccessiblePaths").split()
    # Only the namespaces bubblewrap creates; no realtime, no raw sockets.
    assert _one(unit, "RestrictNamespaces") == "user pid net ipc uts mnt"
    assert _one(unit, "RestrictAddressFamilies") == "AF_UNIX AF_INET AF_INET6 AF_NETLINK"
    assert unit["SystemCallFilter"] == [
        "@system-service @mount", "~@clock @cpu-emulation @debug @module @obsolete @raw-io @reboot @swap"]
    assert _one(unit, "IPAddressDeny") == "10.0.0.0/8 192.168.0.0/16"


def test_hardening_that_would_break_the_job_sandbox_stays_off():
    """Each of these overmounts part of /proc, and then bubblewrap cannot mount a fresh /proc
    for the job's PID namespace ("Can't mount proc on /newroot/proc"), observed on Ubuntu 24.04."""
    unit = _unit()
    for key in ("ProtectKernelTunables", "ProtectKernelLogs", "ProtectHostname", "ProcSubset", "PrivateUsers"):
        assert key not in unit, key


def test_the_runner_fails_closed_and_checks_itself_before_starting():
    unit = _unit()
    env = _envs(unit)
    assert env["ARC2_CONFINE"] == "auto", "never none: no sandbox means no runner"
    assert env["ARC2_EGRESS"] == "proxy"
    assert env["ARC2_EGRESS_ALLOW"] == "api.anthropic.com"
    assert env["ARC2_FALLBACK"] == "off"
    assert env["ARC2_RUNS_DIR"] == "/srv/truenorth/app/build/arc2", "/arc2 always writes <repo>/build/arc2"
    assert env["PATH"].startswith("/opt/truenorth-arc2/bin:"), "the runner's own bwrap comes first"
    assert _one(unit, "ExecStartPre") == "/opt/truenorth-arc2/venv/bin/python -m arc2.runner --self-test"
    assert _one(unit, "ExecStart").startswith("/opt/truenorth-arc2/venv/bin/python -m arc2.runner")
    assert set(_one(unit, "RestartPreventExitStatus").split()) == {"2", "3"}
    # Every ARC2_* setting the unit makes is one the runner reads.
    source = "".join(p.read_text() for p in (ROOT / "tools/arc2").glob("*.py"))
    for name in (k for k in env if k.startswith("ARC2_")):
        assert f'"{name}"' in source, f"{name} is not read by tools/arc2"
    assert "--self-test" in (ROOT / "tools/arc2/runner.py").read_text()


def test_jobs_get_only_the_extra_environment_they_need():
    forwarded = set(_envs(_unit())["ARC2_JOB_ENV"].split(","))
    assert forwarded == {"DISABLE_AUTOUPDATER", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"}
    gw = _unit(tn_arc2_anthropic_base_url="https://llm-gw.example.org/anthropic", tn_arc2_egress_allow="llm-gw.example.org")
    env = _envs(gw)
    assert env["ANTHROPIC_BASE_URL"] == "https://llm-gw.example.org/anthropic"
    assert "ANTHROPIC_BASE_URL" in env["ARC2_JOB_ENV"].split(",")
    assert env["ARC2_EGRESS_ALLOW"] == "llm-gw.example.org"
    assert "urlsplit('hostname')" in _defaults()["tn_arc2_egress_allow"]


# ── The key ───────────────────────────────────────────────────────────
def test_the_key_reaches_the_runner_only_through_a_root_0600_environment_file():
    unit = _unit()
    assert _one(unit, "EnvironmentFile") == "/etc/truenorth-arc2/runner.env", "required (no leading -)"
    assert not any("ANTHROPIC_API_KEY" in v for v in unit.get("Environment", []))
    assert not any(KEY in v for vs in unit.values() for v in vs)
    write = next(t for t in _tasks() if (_module(t, "template") or {}).get("src") == "runner.env.j2")
    tmpl = write["ansible.builtin.template"]
    assert (tmpl["dest"], tmpl["owner"], tmpl["group"], tmpl["mode"]) == ("{{ tn_arc2_env_file }}", "root", "root", "0600")
    assert write["no_log"] is True
    assert f"ANTHROPIC_API_KEY={{{{ {KEY} " in (ROLE / "templates/runner.env.j2").read_text()
    assert _defaults()["tn_arc2_etc"] == "/etc/truenorth-arc2"
    etc = next(t for t in _tasks() if _module(t, "file") and t.get("loop") and
               any(i.get("path") == "{{ tn_arc2_etc }}" for i in t["loop"]))
    assert next(i for i in etc["loop"] if i["path"] == "{{ tn_arc2_etc }}")["mode"] == "0700"


def test_the_key_is_nowhere_else():
    allowed = {ROLE / "templates/runner.env.j2", ROLE / "tasks/install.yml", INSTALL / "roles/tn_preflight/tasks/main.yml",
               INSTALL / "inventory/group_vars/all/vault.yml.example", ROLE / "templates/truenorth-arc2-runner.service.j2"}
    for path in [*INSTALL.rglob("*.yml"), *INSTALL.rglob("*.j2"), *DOCKER.glob("*.yml")]:
        if path not in allowed:
            assert KEY not in _code(path.read_text()), path
    # In the role it is only asserted on and templated, never on a command line.
    for task in _tasks():
        if KEY in str(task) and "block" not in task:
            assert set(task) & {"ansible.builtin.assert", "ansible.builtin.template"}, task.get("name")
    assert re.search(rf"^{KEY}: \"\"$", (INSTALL / "inventory/group_vars/all/vault.yml.example").read_text(), re.M)
    assert KEY not in _group_vars(), "no default key"


def test_preflight_requires_the_key_and_checks_the_host_when_enabled():
    tasks = _yaml(INSTALL / "roles/tn_preflight/tasks/main.yml")
    block = next(t for t in tasks if t.get("name") == "Preflight — ARC² Course Studio")
    assert block["when"] == "tn_arc2_enabled | default(false) | bool"
    inner = block["block"]
    key = next(t for t in inner if KEY in str((_module(t, "assert") or {}).get("that")))
    assert any("length > 0" in c for c in key["ansible.builtin.assert"]["that"])
    text = str(inner)
    for needle in ("max_user_namespaces", "apparmor_restrict_unprivileged_userns", "tn_arc2_min_free_gb",
                   "tn_arc2_min_free_system_gb", "getent group", "tn_arc2_anthropic_base_url"):
        assert needle in text, needle


# ── Sandbox: AppArmor scoped to the runner's bwrap ─────────────────────
def test_only_the_runners_own_bwrap_may_create_user_namespaces():
    d = _defaults()
    copy = next(t for t in _tasks() if (_module(t, "copy") or {}).get("dest") == "{{ tn_arc2_bwrap }}")
    c = copy["ansible.builtin.copy"]
    assert (c["src"], c["remote_src"], c["owner"], c["group"], c["mode"]) == (
        "/usr/bin/bwrap", True, "root", "{{ tn_arc2_user }}", "0750")
    assert d["tn_arc2_bwrap"] == "{{ tn_arc2_prefix }}/bin/bwrap"
    profile = (ROLE / "templates/apparmor-bwrap.j2").read_text()
    outer, job = profile.split("profile {{ tn_arc2_apparmor_profile }}-job", 1)
    assert "profile {{ tn_arc2_apparmor_profile }} {{ tn_arc2_bwrap }} " in outer
    assert "allow userns," in outer
    assert "userns" not in job.split("# Not allowed")[0] and "capability" not in job.split("# Not allowed")[0]
    assert "-> &{{ tn_arc2_apparmor_profile }}//&{{ tn_arc2_apparmor_profile }}-job," in outer, "stacked under no_new_privs"
    # The host-wide restriction is never switched off.
    for path in [*INSTALL.rglob("*.yml"), *INSTALL.rglob("*.j2")]:
        text = path.read_text()
        assert not re.search(r"apparmor_restrict_unprivileged_userns\S*\s*[:=]\s*['\"]?0", text), path


def test_bwrap_is_probed_as_the_runner_before_the_service_starts():
    tasks = _tasks()
    probe = next(t for t in tasks if _argv(t)[:1] == ["runuser"])
    argv = probe["ansible.builtin.command"]["argv"]
    assert argv[1:3] == ["-u", "{{ tn_arc2_user }}"] and "{{ tn_arc2_bwrap }}" in argv and "--unshare-net" in argv
    names = [t.get("name") for t in tasks]
    start = names.index("ARC² — the runner starts at boot and is running")
    assert names.index("ARC² — the sandbox can be built") < start
    health = next(t for t in tasks if t.get("name") == "ARC² — the runner is healthy")
    that = " ".join(health["ansible.builtin.assert"]["that"])
    assert "self-test passed (confinement: bubblewrap" in that and "arc2 runner: watching" in that


# ── Runs directory, and the api's wiring ──────────────────────────────
def test_the_runs_directory_lets_the_api_queue_and_the_runner_own_the_rest():
    dirs = next(t for t in _tasks() if t.get("name") == "ARC² — the runner's home, the runs directory and the install prefix")
    modes = {i["path"]: (i.get("owner"), i.get("group"), i.get("mode")) for i in dirs["loop"]}
    runner, group = "{{ tn_arc2_user }}", "{{ tn_arc2_runs_group }}"
    assert modes["{{ tn_arc2_runs_dir }}"] == (runner, group, "02750")
    for sub in ("_queue", "_studio"):  # the api writes these (app/routers/arc2_studio.py)
        assert modes[f"{{{{ tn_arc2_runs_dir }}}}/{sub}"] == (runner, group, "02770")
    for sub in ("_jobs", "_history"):  # the runner's alone
        assert modes[f"{{{{ tn_arc2_runs_dir }}}}/{sub}"] == (runner, group, "02750")
    assert modes["{{ tn_arc2_home }}"] == (runner, runner, "0700")
    grp = next(t for t in _tasks() if (_module(t, "group") or {}).get("name") == "{{ tn_arc2_runs_group }}")
    assert grp["ansible.builtin.group"]["gid"] == "{{ tn_arc2_runs_gid }}"
    user = next(t for t in _tasks() if _module(t, "user"))["ansible.builtin.user"]
    assert user["shell"] == "/usr/sbin/nologin" and user["system"] is True
    assert "docker" not in str(user["groups"]) and user["append"] is False


def _env_block(enabled: bool) -> str:
    text = (INSTALL / "roles/tn_config/templates/env.production.j2").read_text()
    block = re.search(r"# ── ARC² Course Studio.*?{% endif %}\n", text, re.S).group(0)
    return _jinja().from_string(block).render(
        tn_arc2_enabled=enabled, tn_arc2_runs_dir="/srv/truenorth/arc2/runs", tn_arc2_runs_gid=10010)


def test_the_env_file_wires_the_api_only_when_enabled():
    on = _env_block(True)
    assert "ARC2_STUDIO_ENABLED=true\n" in on
    assert "ARC2_RUNS_HOST_DIR=/srv/truenorth/arc2/runs\n" in on
    assert "ARC2_RUNS_GID=10010\n" in on
    off = _env_block(False)
    assert not re.search(r"^ARC2_", off, re.M), "off: no ARC2_* setting at all"
    assert KEY not in (INSTALL / "roles/tn_config/templates/env.production.j2").read_text()


def test_compose_mounts_the_runs_and_joins_the_group_only_when_enabled():
    prod = _yaml(DOCKER / "compose.prod.yml")
    api = prod["services"]["api"]
    env = api["environment"]
    assert env["ARC2_STUDIO_ENABLED"] == "${ARC2_STUDIO_ENABLED:-false}"
    assert env["ARC2_RUNS_DIR"] == "/srv/arc2/runs"
    assert "${ARC2_RUNS_HOST_DIR:-arc2-runs-unused}:/srv/arc2/runs" in api["volumes"]
    assert api["group_add"] == ["${ARC2_RUNS_GID:-app}"]
    assert prod["volumes"]["arc2-runs-unused"] == {"driver": "local"}, "off: an empty volume, no host path"
    # The fallback group is the image's own (adds nothing).
    assert re.search(r"useradd --system --uid 10001 .* app$", (ROOT / "control-plane/api/Dockerfile").read_text(), re.M)
    # No other service sees the runs.
    for name, svc in prod["services"].items():
        if name != "api":
            assert "arc2" not in json.dumps(svc).lower(), name


def test_the_installer_refuses_an_app_without_the_wiring():
    check = next(t for t in _tasks() if t.get("name") == "ARC² — the deployed app supports an installed Course Studio")
    that = " ".join(check["ansible.builtin.assert"]["that"])
    assert "ARC2_RUNS_HOST_DIR" in that and "--self-test" in that
