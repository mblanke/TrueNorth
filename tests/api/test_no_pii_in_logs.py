"""No log call in the API or worker passes an email address as an argument.

Registration, approval, LTI just-in-time provisioning, the bootstrap admin and a
lease force-release all logged the person's email at INFO, which put it in every log
shipper, backup and support bundle. They log user ids now. This walks every logger
call by construction, so a new one that passes ``email`` or ``x.email`` fails here.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "control-plane"
LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}


def _mentions_email(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in ("email", "user_email"):
            return True
        if isinstance(sub, ast.Attribute) and sub.attr in ("email", "user_email"):
            return True
    return False


def _offenders(path: Path, root: Path = ROOT) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in LOG_METHODS:
            continue
        owner = node.func.value
        name = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "")
        if "log" not in name.lower():
            continue
        args = [*node.args[1:], *(k.value for k in node.keywords)]
        if any(_mentions_email(a) for a in args):
            found.append(f"{path.relative_to(root)}:{node.lineno}")
    return found


def test_no_logger_call_is_passed_an_email():
    files = [*ROOT.glob("api/app/**/*.py"), *ROOT.glob("worker/worker/**/*.py")]
    assert len(files) > 100
    offenders = [o for f in files for o in _offenders(f)]
    assert not offenders, "log a user id, not an email:\n  " + "\n  ".join(offenders)


def test_the_guard_detects_an_email_argument(tmp_path):
    sample = tmp_path / "x.py"
    sample.write_text('logger.info("user %s", user.email)\nlog.warning("%s", email)\nlogger.info("ok %s", user.id)\n')
    assert len(_offenders(sample, tmp_path)) == 2
