"""Tests for the Claude Code guard hook. Run: python3 -m unittest discover -s tests"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / ".claude" / "hooks" / "guard.py"


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self.tmp.name)
        (self.proj / ".claude" / "approvals").mkdir(parents=True)
        (self.proj / "config").mkdir()
        (self.proj / "config" / "blackout.json").write_text(json.dumps({"windows": []}))

    def tearDown(self):
        self.tmp.cleanup()

    def run_guard(self, tool: str, **tool_input) -> tuple[int, str]:
        env = dict(os.environ, CLAUDE_PROJECT_DIR=str(self.proj))
        p = subprocess.run(["python3", str(GUARD)], input=json.dumps({"tool_name": tool, "tool_input": tool_input}),
                           capture_output=True, text=True, env=env)
        return p.returncode, p.stderr

    def bash(self, cmd: str) -> tuple[int, str]:
        return self.run_guard("Bash", command=cmd)

    def approve(self, rid="CANARY", actions=("build",), hours=1.0):
        now = dt.datetime.now(dt.timezone.utc)
        (self.proj / ".claude" / "approvals" / f"{rid}.json").write_text(json.dumps({
            "range_id": rid, "actions": list(actions), "approved_by": "test",
            "created": now.isoformat(), "expires": (now + dt.timedelta(hours=hours)).isoformat()}))

    def assertBlocked(self, cmd, why=None):
        rc, err = self.bash(cmd)
        self.assertEqual(rc, 2, f"expected block: {cmd}")
        if why:
            self.assertIn(why, err)

    def assertAllowed(self, cmd):
        rc, err = self.bash(cmd)
        self.assertEqual(rc, 0, f"expected allow: {cmd}\n{err}")

    # --- allowed read-only and planning work
    def test_allows_planning(self):
        for c in ["tools/range validate CANARY", "tools/range compile CANARY --write-allocation",
                  "tools/range plan EXCOURSE", "tools/range check CANARY", "tools/range drift CANARY",
                  "tools/range list", "git status", "ansible-lint playbooks/",
                  "ansible-playbook playbooks/range_build.yml -e range_id=CANARY --check --diff",
                  "ansible-playbook --syntax-check playbooks/range_build.yml",
                  "ansible-playbook playbooks/discovery_network.yml",
                  "ansible-playbook playbooks/range_drift_collect.yml -e range_id=CANARY",
                  "govc ls /DC0/vm", "govc vm.info -json CANARY-red-op01", "govc tags.attached.ls -r /DC0/vm/x"]:
            self.assertAllowed(c)

    # --- R3/R4: real runs need approval
    def test_build_needs_approval(self):
        self.assertBlocked("tools/range build CANARY", "no human approval")
        self.assertBlocked("ansible-playbook playbooks/range_build.yml -e range_id=CANARY", "no human approval")
        self.approve()
        self.assertAllowed("tools/range build CANARY")
        self.assertAllowed("ansible-playbook playbooks/range_build.yml -e range_id=CANARY")

    def test_approval_scope_and_expiry(self):
        self.approve(actions=("build",))
        self.assertBlocked("tools/range teardown CANARY --confirm CANARY", "does not cover")
        self.approve(actions=("build",), hours=-1)
        self.assertBlocked("tools/range build CANARY", "expired")
        (self.proj / ".claude" / "approvals" / "CANARY.json").unlink()
        self.approve(rid="OTHER", actions=("build",))
        self.assertBlocked("tools/range build CANARY", "no human approval")

    def test_teardown_needs_confirm(self):
        self.approve(actions=("teardown",))
        self.assertBlocked("ansible-playbook playbooks/range_teardown.yml -e range_id=CANARY", "confirm")
        self.assertAllowed("ansible-playbook playbooks/range_teardown.yml -e range_id=CANARY -e confirm=CANARY")

    def test_refuses_wildcards_and_bypass_flags(self):
        self.approve()
        self.assertBlocked("ansible-playbook playbooks/range_build.yml -e range_id=all")
        self.assertBlocked("ansible-playbook playbooks/range_build.yml -e range_id=CANARY --skip-tags tn_vapi", "--skip-tags")
        self.assertBlocked("ansible-playbook playbooks/range_build.yml -e range_id=CANARY --start-at-task x")
        self.assertBlocked("ansible-playbook playbooks/range_build.yml -e range_id=CANARY -e tn_require_buildable=false",
                           "overriding")
        self.assertBlocked("ansible-playbook playbooks/other.yml -e range_id=CANARY", "not a known range playbook")
        self.assertBlocked("ansible-playbook other.yml -e range_id=CANARY", "playbooks must come from")
        self.assertBlocked('TNRANGE_ANSIBLE_ARGS="--skip-tags tn_vapi" tools/range build CANARY')

    def test_blackout(self):
        self.approve()
        now = dt.datetime.now(dt.timezone.utc)
        (self.proj / "config" / "blackout.json").write_text(json.dumps({"windows": [{
            "start": (now - dt.timedelta(hours=1)).isoformat(), "end": (now + dt.timedelta(hours=1)).isoformat(),
            "reason": "EX TEST"}]}))
        self.assertBlocked("tools/range build CANARY", "blackout")
        self.assertAllowed("tools/range check CANARY")

    def test_human_only_commands(self):
        for c in ["tools/range approve CANARY --action build", "tools/range revoke CANARY",
                  "tools/range release CANARY --confirm CANARY", "cd x && tools/range approve CANARY --action build"]:
            self.assertBlocked(c, "human-only")

    # --- R1 secrets
    def test_secrets(self):
        for c in ["ansible-vault view vault/vcenter.yml", "ansible-vault decrypt vault/x.yml", "env",
                  "printenv", "echo $VCENTER_PASSWORD", "cat vault/vcenter.yml", "grep pass vault/sim.yml",
                  "ansible-playbook playbooks/range_validate.yml -e range_id=CANARY -e tn_no_log=false",
                  "export GOVC_URL=https://admin:hunter2@vc/sdk"]:
            self.assertBlocked(c)
        self.assertEqual(self.run_guard("Read", file_path=str(self.proj / "vault" / "vcenter.yml"))[0], 2)

    # --- R2 govc
    def test_govc_writes_blocked(self):
        for c in ["govc vm.destroy CANARY-corp-dc01", "govc vm.power -off x", "govc snapshot.revert -vm x base",
                  "govc dvs.portgroup.add -dvs d -vlan 5 pg", "govc tags.detach t x", "govc object.destroy /DC0/vm/x"]:
            self.assertBlocked(c, "govc")

    # --- R5 protected files
    def test_protected_paths(self):
        for c in ["echo '{}' > .claude/approvals/CANARY.json", "rm .claude/hooks/guard.py",
                  "sed -i 's/x/y/' .claude/settings.json", "touch .claude/approvals/X.json",
                  "python3 -c 'open(\".claude/approvals/X.json\",\"w\")'"]:
            self.assertBlocked(c, "protected")
        for p in [".claude/approvals/CANARY.json", ".claude/hooks/guard.py", ".claude/settings.json",
                  "config/blackout.json", "vault/vcenter.yml"]:
            self.assertEqual(self.run_guard("Write", file_path=str(self.proj / p), content="x")[0], 2, p)
        self.assertEqual(self.run_guard("Write", file_path=str(self.proj / "ranges" / "R-0001.yml"), content="x")[0], 0)

    def test_reviewer_bypasses_are_blocked(self):
        # No approval exists in these cases; every one of them must be refused.
        for c in [
            "timeout 3600 ansible-playbook playbooks/range_build.yml -e range_id=CANARY",
            'bash -c "ansible-playbook playbooks/range_build.yml -e range_id=CANARY"',
            "nice ansible-playbook playbooks/range_build.yml -e range_id=CANARY",
            "sudo -E ansible-playbook playbooks/range_build.yml -e range_id=CANARY",
            "( ansible-playbook playbooks/range_build.yml -e range_id=CANARY )",
            "if true; then ansible-playbook playbooks/range_build.yml -e range_id=CANARY; fi",
            "sleep 1 & ansible-playbook playbooks/range_build.yml -e range_id=CANARY",
            "python3 /usr/bin/ansible-playbook playbooks/range_build.yml -e range_id=CANARY",
            "cd tools && ./range build CANARY",
            "cd tools && ./range approve CANARY --action build",
            "ansible localhost --module-name community.vmware.vmware_guest -a 'name=x state=absent'",
            "ansible localhost -mcommunity.vmware.vmware_guest -a 'name=x'",
            "python3 -c 'import sys; sys.path.insert(0,\"tools\"); from tnrange import gates; gates.approve()'",
            "govc env",
            "echo vm.destroy | xargs govc",
            "echo 'tools/range build CANARY' | bash",
            "eval tools/range build CANARY",
            "x=$(tools/range build CANARY)",
            "ansible-playbook playbooks/range_validate.yml playbooks/range_teardown.yml -e range_id=CANARY -e confirm=CANARY",
            "ansible-playbook playbooks/discovery_network.yml playbooks/range_build.yml -e range_id=CANARY",
            "ansible-playbook /tmp/evil/range_build.yml -e range_id=CANARY --check",
            "TNRANGE_ROOT=/tmp/x ansible-playbook playbooks/range_build.yml -e range_id=CANARY --check",
            "ANSIBLE_ROLES_PATH=/tmp/r tools/range check CANARY",
        ]:
            self.assertBlocked(c)

    def test_second_review_bypasses_are_blocked(self):
        self.approve(actions=("build", "teardown"))
        (self.proj / "vault").mkdir(exist_ok=True)
        for c in [
            "ansible-playbook playbooks/range_teardown.yml -e range_id=CANARY -e confirm=CANARY "
            "--extra-var=range_id=EXCOURSE --extra-var=confirm=EXCOURSE --extra-var=tn_cli=/bin/true",
            "ansible-playbook playbooks/range_build.yml -e range_id=CANARY --skip-tag=tn_vapi",
            "ansible-playbook playbooks/range_build.yml -e range_id=CANARY --start-at=x",
            "ansible-playbook playbooks/range_build.yml -e range_id=CANARY --extra-var=@/tmp/x",
            "ansible-playbook playbooks/range_build.yml -e range_id=CANARY --forks 50",
            "ansible-playbook playbooks/range_build.yml -e range_id=CANARY -e @vault/../../tmp/evil.yml",
            "R=tools/range; $R approve EXCOURSE --action build --action teardown",
            "tools/rang? approve EXCOURSE --action build",
            "$'ansible-playbook' playbooks/range_build.yml -e range_id=EXCOURSE",
            "/usr/local/bin/ansible-playboo? playbooks/range_build.yml -e range_id=EXCOURSE",
            "git -c alias.x='!tools/range approve EXCOURSE --action build' x",
            "ci/canary.sh", "bash ci/canary.sh", "./ci/canary.sh",
            'echo "tn_cli: /bin/true" > vault/x.yml',
            "ln -s /tmp/evil.yml vault/x.yml",
            "script -qc 'tools/range approve CANARY --action build' /dev/null",
        ]:
            self.assertBlocked(c)

    def test_quotes_with_braces_are_not_false_positives(self):
        for c in ["jq '.instances[] | {id}' build/CANARY/plan.json", "awk '{print $1}' ranges/CANARY.yml",
                  'git commit -m "fix teardown (scope)"']:
            self.assertAllowed(c)

    def test_cross_range_injection_blocked(self):
        self.approve(actions=("build", "teardown"))
        for c in [
            "ansible-playbook playbooks/range_build.yml -e range_id=CANARY -e @/tmp/ev.yml",
            "ansible-playbook playbooks/range_teardown.yml -e range_id=CANARY -e confirm=CANARY -e '{\"range_id\":\"EXCOURSE\"}'",
            "ansible-playbook playbooks/range_build.yml -e range_id=CANARY -e range_id=EXCOURSE",
            "ansible-playbook playbooks/range_build.yml -e range_id=CANARY -e plan=x",
            'TNRANGE_ANSIBLE_ARGS="-e range_id=EXCOURSE -e confirm=EXCOURSE" tools/range teardown CANARY --confirm CANARY',
        ]:
            self.assertBlocked(c)
        self.assertAllowed('TNRANGE_ANSIBLE_ARGS="-e @vault/vcenter.yml --vault-id tn@prompt" tools/range build CANARY')

    def test_approval_bound_to_plan(self):
        build = self.proj / "build" / "CANARY"
        build.mkdir(parents=True)
        (build / "plan.json").write_text(json.dumps({"plan_sha256": "aaa"}))
        now = dt.datetime.now(dt.timezone.utc)
        (self.proj / ".claude" / "approvals" / "CANARY.json").write_text(json.dumps({
            "range_id": "CANARY", "actions": ["build"], "plan_sha256": "aaa",
            "expires": (now + dt.timedelta(hours=1)).isoformat()}))
        self.assertAllowed("tools/range build CANARY")
        (build / "plan.json").write_text(json.dumps({"plan_sha256": "bbb"}))
        self.assertBlocked("tools/range build CANARY", "different plan")

    def test_bad_blackout_fails_closed(self):
        self.approve()
        (self.proj / "config" / "blackout.json").write_text(json.dumps({"windows": [{"start": "2026-10-01", "end": "2026-10-02"}]}))
        self.assertBlocked("tools/range build CANARY")

    def test_build_dir_protected(self):
        self.assertBlocked("echo '{}' > build/CANARY/plan.json", "protected")
        self.assertEqual(self.run_guard("Edit", file_path=str(self.proj / "build" / "CANARY" / "plan.json"))[0], 2)
        self.assertEqual(self.run_guard("Write", file_path=str(self.proj / "ranges" / ".allocations.json"))[0], 2)
        self.assertAllowed("cat build/CANARY/plan.txt")

    def test_readonly_viewers_allowed(self):
        for c in ["grep -rn ansible-playbook README.md", "grep -n vmware_guest playbooks/roles/range_vms/tasks/main.yml",
                  "git diff -- tools/range", "ls tools/", "cat ranges/CANARY.yml | head -20"]:
            self.assertAllowed(c)

    def test_malformed_input_fails_closed(self):
        p = subprocess.run(["python3", str(GUARD)], input="not json", capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)


if __name__ == "__main__":
    unittest.main()
