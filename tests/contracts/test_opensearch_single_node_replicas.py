"""Single-node OpenSearch: the installer keeps every index at 0 replicas.

TN-MGMT01 (2026-10-15): a plugin index (.opendistro-job-scheduler-lock) was created with
1 replica on the one-node cluster, which went yellow, and 95-smoke-test's readiness failed
the install. OpenSearch 2.13 accepts compose's node setting cluster.default_number_of_replicas
but ignores it at index creation; as a persistent cluster setting it is used.

These hold the installer's fix (roles/tn_telemetry/tasks/replicas.yml and its script):
where it runs, what it sets, that it is idempotent and credential-free, and that readiness
stays strict. The OpenSearch calls themselves were exercised against opensearch:2.13.0
(security on and off) when this was written; no cluster is needed here.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install"
ROLE = INSTALL / "roles/tn_telemetry"
TASKS = ROLE / "tasks/replicas.yml"
SCRIPT = ROLE / "files/opensearch_replicas.sh"
COMPOSE = ROOT / "infra/platform/docker/compose.prod.yml"

_SECRET = re.compile(r"pass(word)?|tn_secrets|vault_|OPENSEARCH_PASS", re.IGNORECASE)


def _tasks(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text()) or []


def _replica_task() -> dict:
    tasks = [t for t in _tasks(TASKS) if "ansible.builtin.command" in t]
    assert len(tasks) == 1, "replicas.yml runs exactly one command"
    return tasks[0]


def _code(text: str) -> str:
    """The script without its comment lines."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_compose_runs_opensearch_as_a_single_node():
    # The premise of the fix: if this changes, revisit replicas.yml.
    assert "discovery.type=single-node" in COMPOSE.read_text()


def test_task_runs_the_script_in_the_opensearch_container():
    task = _replica_task()
    cmd = task["ansible.builtin.command"]
    assert "exec -T opensearch sh -s" in " ".join(cmd["cmd"].split())
    assert "opensearch_replicas.sh" in cmd["stdin"]
    assert SCRIPT.is_file()


def test_script_targets_zero_replicas_for_new_and_existing_indices():
    code = _code(SCRIPT.read_text())
    # New indices: the persistent cluster default, not the node setting 2.13 ignores.
    assert '{"persistent":{"cluster.default_number_of_replicas":0}}' in code
    # Existing indices, hidden and system ones included, only those above 0.
    assert "_cat/indices?h=index,rep&expand_wildcards=all" in code
    assert "awk '$2 > 0" in code
    assert '{"index":{"number_of_replicas":0}}' in code
    assert "_settings?expand_wildcards=all" in code


def test_script_does_nothing_on_a_multi_node_cluster():
    code = _code(SCRIPT.read_text())
    assert "_cat/nodes" in code
    assert re.search(r'if \[ "\$nodes" -ne 1 \]; then\s+echo "multi-node \$nodes"\s+exit 0', code)


def test_script_authenticates_with_the_admin_certificate_not_a_password():
    code = _code(SCRIPT.read_text())
    assert "--cert config/certs/admin.pem --key config/certs/admin-key.pem" in code
    assert "--cacert config/certs/ca.pem" in code
    # The lab override (security plugin off) is plain http with no auth.
    assert 'DISABLE_SECURITY_PLUGIN:-false}" = true' in code
    assert not _SECRET.search(code), "the script must carry no password"
    # Failures stop it (a non-zero rc fails the task) rather than reading as "unchanged".
    assert "set -eu" in code and "--fail" in code


def test_credentials_never_reach_the_task_or_are_no_log():
    for task in _tasks(TASKS):
        body = yaml.safe_dump(task)
        if _SECRET.search(body):
            assert task.get("no_log") is True, task["name"]
    # The command task itself carries none: auth is the admin certificate in the container.
    assert not _SECRET.search(yaml.safe_dump(_replica_task()))


def test_change_is_reported_only_when_something_changed():
    task = _replica_task()
    assert task["changed_when"] == "tn_os_replicas.stdout is search('^changed:', multiline=True)"
    code = _code(SCRIPT.read_text())
    # Idempotence: the cluster default is written only when not already 0.
    assert '*\'"cluster.default_number_of_replicas":"0"\'*) ;;' in code
    assert 'echo "unchanged"' in code and 'echo "changed:$changed"' in code


def test_runs_after_the_telemetry_bootstrap_and_before_smoke_readiness():
    main = _tasks(ROLE / "tasks/main.yml")
    names = [t["name"] for t in main]
    imported = [i for i, t in enumerate(main) if t.get("ansible.builtin.import_tasks") == "replicas.yml"]
    assert imported, "70-telemetry imports replicas.yml"
    assert names.index("Telemetry — bootstrap OpenSearch objects") < imported[0]

    smoke = _tasks(INSTALL / "roles/tn_smoke/tasks/main.yml")
    inc = [i for i, t in enumerate(smoke)
           if (t.get("ansible.builtin.include_role") or {}).get("tasks_from") == "replicas"
           and t["ansible.builtin.include_role"]["name"] == "tn_telemetry"]
    ready = [i for i, t in enumerate(smoke) if t["name"] == "Smoke — readiness, including OpenSearch"]
    assert inc and ready and inc[0] < ready[0]


def test_readiness_stays_strict():
    smoke = _tasks(INSTALL / "roles/tn_smoke/tasks/main.yml")
    gate = next(t for t in smoke if t["name"] == "Smoke — assert the database, Redis and OpenSearch are healthy")
    assert gate["vars"]["_need"] == ["database", "redis", "opensearch"]
    assert "tn_ready.status == 200" in gate["ansible.builtin.assert"]["that"]
    assert "failed_when" not in gate and "ignore_errors" not in gate
