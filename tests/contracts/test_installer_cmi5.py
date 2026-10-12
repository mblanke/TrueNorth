"""The installer gives the api everything cmi5 needs in production (v1.2.0-rc4: staging had
no CMI5_LRS_AUTH, so every cmi5 launch answered 503).

- CMI5_LRS_AUTH, the AU's own LRS credential, is minted ON the LRS (roles/tn_cmi5, after
  the stack is up), persisted in config/secrets/cmi5_lrs_auth (0600, never echoed), kept on
  every re-run, rendered into .env.production and passed by compose to the api;
- the cmi5 / xAPI URLs (CMI5_WEB_BASE_URL, CMI5_API_BASE_URL, XAPI_ACCOUNT_HOMEPAGE,
  XAPI_IRI_BASE) are rendered with the values the api already derived;
- preflight refuses an unusable vault credential, 99-validate fails an install that ends
  without one, and 95-smoke-test proves launches would not answer 503.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import re
from pathlib import Path

import httpx
import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install"
DOCKER = ROOT / "infra/platform/docker"
ROLE = INSTALL / "roles/tn_cmi5"
FQDN = "tn.example.test"


def _yaml(path: Path):
    return yaml.safe_load(path.read_text())


def _task(path: Path, name: str) -> dict:
    return next(t for t in _yaml(path) if t.get("name") == name)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_tn_cmi5_{name}", ROLE / "files" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor("!reset", lambda loader, node: None)
_Loader.add_constructor("!override", lambda loader, node: None)


def _api_env() -> dict:
    return yaml.load((DOCKER / "compose.prod.yml").read_text(), Loader=_Loader)["services"]["api"]["environment"]  # noqa: S506


# ── 1. Every variable cmi5 and the LRS read reaches the api ──────────────────────────
CMI5_ENV = ("CMI5_LRS_AUTH", "CMI5_WEB_BASE_URL", "CMI5_API_BASE_URL", "XAPI_ACCOUNT_HOMEPAGE", "XAPI_IRI_BASE")


def test_the_api_reads_these_and_compose_passes_them():
    code = "".join(p.read_text() for p in (ROOT / "control-plane/api/app/cmi5").glob("*.py"))
    code += (ROOT / "control-plane/api/app/xapi.py").read_text()
    for name in CMI5_ENV:
        assert f'"{name}"' in code, name
    env = _api_env()
    for name in (*CMI5_ENV, "LRS_URL", "LRS_AUTH", "LMS_BACKEND", "TN_SECRETS_KEY"):
        assert env[name].startswith(f"${{{name}"), name
    # Empty means "derive" in the api (app/cmi5/structure.py), so compose passes empty.
    assert env["CMI5_WEB_BASE_URL"] == "${CMI5_WEB_BASE_URL:-}"
    assert env["CMI5_API_BASE_URL"] == "${CMI5_API_BASE_URL:-}"


def _rendered(**over) -> dict[str, str]:
    """The xAPI and cmi5 lines of env.production.j2, rendered with group_vars' defaults."""
    gv = _yaml(INSTALL / "inventory/group_vars/all/main.yml")
    j = jinja2.Environment(undefined=jinja2.StrictUndefined)
    ctx: dict = {"tn_domain_fqdn": FQDN}
    for key in (
        "tn_lti_web_base_url",
        "tn_xapi_account_homepage",
        "tn_xapi_iri_base",
        "tn_cmi5_web_base_url",
        "tn_cmi5_api_base_url",
    ):
        ctx[key] = j.from_string(gv[key]).render(**ctx)
    ctx["tn_cmi5_enabled"] = gv["tn_cmi5_enabled"]
    ctx.update(over)
    j.filters["bool"] = bool
    template = (INSTALL / "roles/tn_config/templates/env.production.j2").read_text()
    lines = "\n".join(line for line in template.splitlines() if line.startswith(CMI5_ENV))
    return dict(line.split("=", 1) for line in j.from_string(lines).render(**ctx).splitlines())


def test_the_env_file_renders_them_with_the_values_the_api_derived_before():
    env = _rendered(tn_cmi5_lrs_auth="QUtFWTpTRUNSRVQ=")
    assert set(env) == set(CMI5_ENV)
    # compose.prod.yml's defaults until now: https://${DOMAIN} and https://${DOMAIN}/xapi;
    # IRIs compare as strings, so an upgrade must not move them.
    assert env["XAPI_ACCOUNT_HOMEPAGE"] == f"https://{FQDN}"
    assert env["XAPI_IRI_BASE"] == f"https://{FQDN}/xapi"
    assert env["CMI5_WEB_BASE_URL"] == f"https://{FQDN}"
    assert env["CMI5_API_BASE_URL"] == f"https://{FQDN}/api"
    assert env["CMI5_LRS_AUTH"] == "QUtFWTpTRUNSRVQ="
    assert _yaml(INSTALL / "inventory/group_vars/all/main.yml")["tn_cmi5_enabled"] is True


def test_off_renders_no_credential_and_absent_renders_empty():
    assert _rendered(tn_cmi5_enabled=False, tn_cmi5_lrs_auth="QUtFWTpTRUNSRVQ=")["CMI5_LRS_AUTH"] == ""
    # The first render of a fresh install (nothing minted yet): empty, not an error.
    assert _rendered()["CMI5_LRS_AUTH"] == ""


def test_the_env_render_reads_the_persisted_credential_back():
    tasks = _yaml(INSTALL / "roles/tn_config/tasks/env.yml")
    read = next(
        t
        for t in tasks
        if str((t.get("ansible.builtin.slurp") or {}).get("src", "")).endswith("/secrets/cmi5_lrs_auth")
    )
    assert read["no_log"] is True and read["failed_when"] is False
    render = next(t for t in tasks if "ansible.builtin.template" in t)
    assert render["ansible.builtin.template"]["mode"] == "0600" and render["no_log"] is True
    main = _yaml(INSTALL / "roles/tn_config/tasks/main.yml")
    assert any(t.get("ansible.builtin.include_tasks") == "env.yml" for t in main)


# ── 2. The credential: minted on the LRS once, kept, never echoed ────────────────────
def test_the_script_mints_exactly_the_apps_au_scopes():
    from app.lms.lrsql_admin import AU_SCOPES

    assert _load("lrs_au_credential").AU_SCOPES == AU_SCOPES


class _FakeLrs:
    """lrsql's admin API as observed on yetanalytics/lrsql v0.9.9 (login, list, create)."""

    def __init__(self, creds: list[dict]):
        self.creds = creds
        self.minted = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/account/login":
            body = json.loads(request.content)
            if body["password"] != "admin-pass":
                return httpx.Response(401, json={"error": "Invalid Account Credentials"})
            return httpx.Response(200, json={"json-web-token": "jwt", "account-id": "a"})
        assert request.headers["Authorization"] == "Bearer jwt"
        if request.method == "GET":
            return httpx.Response(200, json=self.creds)
        self.minted += 1
        cred = {
            "api-key": f"k{self.minted}",
            "secret-key": f"s{self.minted}",
            "scopes": json.loads(request.content)["scopes"],
        }
        self.creds.append(cred)
        return httpx.Response(200, json=cred)


SERVER = {"api-key": "srv", "secret-key": "srvsecret", "scopes": ["all"]}
AU = {
    "api-key": "au",
    "secret-key": "ausecret",
    "scopes": ["agents_profile", "state", "activities_profile", "statements/write"],
}


def _ensure(creds: list[dict], candidate: str = "", source: str = "persisted", password: str = "admin-pass"):
    mod = _load("lrs_au_credential")
    lrs = _FakeLrs(creds)
    env = {
        "LRS_URL": "http://lrs:8080",
        "LRS_ADMIN_USER": "lrsadmin",
        "LRS_ADMIN_PASSWORD": password,
        "LRS_AUTH": _b64("srv:srvsecret"),
        "TN_CMI5_CANDIDATE": candidate,
        "TN_CMI5_SOURCE": source,
    }
    with httpx.Client(transport=httpx.MockTransport(lrs)) as client:
        return (*mod.ensure(client, env), lrs.minted)


def test_a_fresh_install_mints_one():
    status, new, _, minted = _ensure([SERVER])
    assert (status, minted) == ("MINTED", 1)
    assert base64.b64decode(new).decode() == "k1:s1"


def test_a_rerun_keeps_it():
    assert _ensure([SERVER, AU], _b64("au:ausecret")) == ("KEPT", "", "", 0)


@pytest.mark.parametrize(
    ("creds", "candidate", "why"),
    [
        ([SERVER], _b64("au:ausecret"), "no such credential"),  # the LRS database was rebuilt
        ([SERVER], _b64("srv:srvsecret"), "LRS_AUTH"),  # the server's own: provenance lost
        ([SERVER, {**AU, "scopes": ["all"]}], _b64("au:ausecret"), "scopes"),  # may read everything
        ([SERVER], "not base64!", "base64"),
    ],
)
def test_an_unusable_persisted_one_is_replaced(creds, candidate, why):
    status, new, reason, minted = _ensure(list(creds), candidate)
    assert (status, minted) == ("REMINTED", 1) and new and why in reason


@pytest.mark.parametrize("candidate", [_b64("nope:nope"), _b64("srv:srvsecret")])
def test_the_vaults_is_never_replaced(candidate):
    status, new, reason, minted = _ensure([SERVER], candidate, source="vault")
    assert (status, new, minted) == ("VAULT_REFUSED", "", 0) and reason


def test_a_wrong_admin_password_fails_rather_than_minting():
    with pytest.raises(httpx.HTTPStatusError):
        _ensure([SERVER], password="wrong")


def test_the_role_persists_it_like_the_other_secrets():
    task = _task(ROLE / "tasks/main.yml", "cmi5 — ensure the AU's LRS credential (minted once, then kept)")
    cmd = task["ansible.builtin.shell"]["cmd"]
    assert task["no_log"] is True
    assert "umask 077" in cmd and 'f="{{ tn_config_dir }}/secrets/cmi5_lrs_auth"' in cmd
    assert 'if [ -s "$f" ]' in cmd  # an empty file counts as missing
    assert 'mv "$f.tmp" "$f"' in cmd  # never a half-written credential
    # Secrets never on a command line: the vault's on stdin, the rest through the
    # environment of `docker compose run -e NAME` (no value), as the migration does.
    assert task["ansible.builtin.shell"]["stdin"] == "{{ vault_cmi5_lrs_auth | default('') }}"
    assert "-e LRS_ADMIN_USER -e LRS_ADMIN_PASSWORD -e TN_CMI5_SOURCE -e TN_CMI5_CANDIDATE" in cmd
    # No secret is templated into the command (the admin user name is not a secret).
    assert set(re.findall(r"vault_\w+", cmd)) == {"vault_lrs_admin_user"} and "tn_secrets" not in cmd
    # Not Ansible's task environment either: -vvv prints it, and it prefixes the command line.
    assert "environment" not in task
    assert 'LRS_ADMIN_PASSWORD="$(cat "{{ tn_config_dir }}/secrets/lrs_admin_password")"' in cmd
    assert "lookup('ansible.builtin.file', 'lrs_au_credential.py') | quote" in cmd
    # Only a status word and the script's reason are echoed, never $new or a candidate.
    echoes = re.findall(r"^\s*echo (.*)$", cmd, re.M)
    assert all("new" not in e and "CANDIDATE" not in e and "vault" not in e for e in echoes), echoes
    assert task["until"] == "tn_cmi5_cred.rc == 0" and task["retries"] >= 6  # the LRS may be starting
    assert "'MINTED', 'REMINTED', 'WRITTEN'" in task["changed_when"]  # KEPT is no change


def test_then_the_env_file_is_rendered_and_the_api_recreated():
    tasks = _yaml(ROLE / "tasks/main.yml")
    names = [t["name"] for t in tasks]
    render = names.index("cmi5 — render .env.production with it")
    assert tasks[render]["ansible.builtin.include_role"] == {"name": "tn_config", "tasks_from": "env"}
    up = tasks[render + 1]["community.docker.docker_compose_v2"]
    assert up["services"] == ["api"] and up["dependencies"] is False
    assert up["pull"] == "never" and up["build"] == "never" and up["wait"] is True
    refuse = _task(ROLE / "tasks/main.yml", "cmi5 — the vault's credential is usable on this LRS")
    assert "VAULT_REFUSED" in refuse["ansible.builtin.assert"]["that"]


def test_stack_up_runs_it_after_the_stack_is_up():
    roles = _yaml(INSTALL / "playbooks/50-stack-up.yml")[0]["roles"]
    names = [r["role"] for r in roles]
    assert names.index("tn_config") < names.index("tn_migrate") < names.index("tn_cmi5")
    assert roles[names.index("tn_cmi5")]["when"] == "tn_cmi5_enabled | bool"


def test_backups_escrow_it():
    # backup.env's SECRETS_DIR is the whole of config/secrets/, so the credential is in it.
    assert "SECRETS_DIR={{ tn_config_dir }}/secrets" in (INSTALL / "roles/tn_config/tasks/main.yml").read_text()


# ── 3. Preflight, validate, smoke ─────────────────────────────────────────────────────
def _preflight(**vault) -> bool:
    task = _task(INSTALL / "roles/tn_preflight/tasks/main.yml", "Preflight — cmi5 can get its AU credential")
    j = jinja2.Environment()
    j.filters.update(b64encode=lambda s: _b64(s), b64decode=lambda s: base64.b64decode(s).decode())
    j.tests["match"] = lambda v, p: re.match(p, str(v)) is not None
    ctx = {"vault_cmi5_lrs_auth": "", "vault_lrs_api_key": "srv", "vault_lrs_api_secret": "srvsecret", **vault}
    for name, expr in task["vars"].items():
        ctx[name] = j.from_string(expr).render(**ctx).strip()
    return bool(j.compile_expression(task["ansible.builtin.assert"]["that"].strip())(**ctx))


def test_preflight_accepts_minting_or_a_usable_vault_credential():
    assert _preflight()
    assert _preflight(vault_cmi5_lrs_auth=_b64("au:ausecret"))


@pytest.mark.parametrize("given", [_b64("srv:srvsecret"), _b64("no-colon"), _b64("key:"), "abc", "not base64!"])
def test_preflight_refuses_an_unusable_vault_credential(given):
    assert not _preflight(vault_cmi5_lrs_auth=given)


def test_validate_fails_the_install_without_it():
    play = _yaml(INSTALL / "playbooks/99-validate.yml")[0]
    inc = next(t for t in play["tasks"] if t.get("name") == "Validate — cmi5")
    assert inc["ansible.builtin.include_role"] == {"name": "tn_cmi5", "tasks_from": "validate"}
    assert inc["when"] == "tn_cmi5_enabled | bool"
    tasks = _yaml(ROLE / "tasks/validate.yml")
    work = [t for t in tasks if "ansible.builtin.assert" not in t]
    assert all(t["no_log"] is True for t in work)  # the env file and the secret are read, never shown
    gate = next(t for t in tasks if "ansible.builtin.assert" in t)
    assert "no_log" not in gate  # its message must reach the operator
    assert gate["ansible.builtin.assert"]["that"] == "tn_cmi5_validate_problem | length == 0"
    problem = next(t for t in tasks if "ansible.builtin.set_fact" in t)["ansible.builtin.set_fact"]
    text = problem["tn_cmi5_validate_problem"]
    assert "has no CMI5_LRS_AUTH" in text and "LRS_AUTH, the server credential" in text and "disagree" in text


def test_smoke_proves_launches_would_not_answer_503():
    play = _yaml(INSTALL / "playbooks/95-smoke-test.yml")[0]
    inc = next(t for t in play["tasks"] if t.get("name") == "Smoke — cmi5")
    assert inc["ansible.builtin.include_role"]["tasks_from"] == "smoke"
    tasks = _yaml(ROLE / "tasks/smoke.yml")
    fetch = next(t for t in tasks if "ansible.builtin.uri" in t)["ansible.builtin.uri"]
    assert "/api/cmi5/fetch/" in fetch["url"] and fetch["method"] == "POST"
    secret = fetch["url"].rsplit("/", 1)[1]
    assert 20 <= len(secret) <= 128  # the route's own bounds: shorter is a 422, not cmi5
    checks = " ".join(str(t["ansible.builtin.assert"]["that"]) for t in tasks if "ansible.builtin.assert" in t)
    assert "tn_cmi5_fetch.status == 200" in checks and "'error-code'" in checks
    run = next(t for t in tasks if "ansible.builtin.command" in t)["ansible.builtin.command"]["argv"]
    assert run[run.index("compose") + 1 :][4:8] == ["exec", "-T", "api", "python"]  # the RUNNING api


def _smoke(monkeypatch, au: str, state: int, read: int):
    mod = _load("smoke_au_credential")
    seen = []

    def lrs(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(state if "/activities/state" in request.url.path else read)

    monkeypatch.setenv("CMI5_LRS_AUTH", au)
    monkeypatch.setenv("LRS_AUTH", _b64("srv:srvsecret"))
    real = httpx.Client
    monkeypatch.setattr(mod.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(lrs)))
    return mod.main(), seen


def test_the_smoke_script_passes_a_working_least_privilege_credential(monkeypatch, capsys):
    rc, seen = _smoke(monkeypatch, _b64("au:ausecret"), 404, 403)
    assert rc == 0 and capsys.readouterr().out.startswith("OK ")
    assert all(r.headers["Authorization"] == "Basic " + _b64("au:ausecret") for r in seen)


@pytest.mark.parametrize(
    ("au", "state", "read", "says"),
    [
        ("", 404, 403, "503"),  # unset: what staging served
        (_b64("srv:srvsecret"), 404, 403, "503"),  # the server's own
        (_b64("au:gone"), 401, 401, "401"),  # the LRS does not know it
        (_b64("au:ausecret"), 404, 200, "least-privilege"),  # it may read statements
    ],
)
def test_the_smoke_script_fails_otherwise(monkeypatch, capsys, au, state, read, says):
    rc, _ = _smoke(monkeypatch, au, state, read)
    out = capsys.readouterr().out
    assert rc == 1 and out.startswith("FAIL") and says in out
    assert "ausecret" not in out and "srvsecret" not in out


# ── 4. Documented ────────────────────────────────────────────────────────────────────
def test_readme_and_changelog_name_them():
    readme = (INSTALL / "README.md").read_text()
    for var in ("tn_cmi5_enabled", "vault_cmi5_lrs_auth", "tn_cmi5_web_base_url", "tn_xapi_iri_base"):
        assert f"`{var}`" in readme, var
    assert "vault_cmi5_lrs_auth" in (INSTALL / "inventory/group_vars/all/vault.yml.example").read_text()
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert "CMI5_LRS_AUTH" in changelog.split("## v1.1.0", 1)[0]
