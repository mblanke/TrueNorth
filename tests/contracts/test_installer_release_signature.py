# Author: Marc-Andre Blanke
# Created: 2026-11-27T02:54:39Z
"""The installer's release signature check (install/roles/tn_release), issue #541.

Air-gapped, the role gave cosign `--trusted-root <file> --offline=true`. cosign v3.1.3
reports `--offline` as deprecated; a later cosign that drops it would stop every air-gapped
install at 20-fetch-app. These tests render the role's real argv and pin what it passes.
"""

from __future__ import annotations

from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "install/roles/tn_release"
VERIFY_TASK = "Release — cosign verify-blob against the release workflow's identity"


def _find(tasks, name):
    for task in tasks:
        if task.get("name") == name:
            return task
        found = _find(task.get("block", []), name)
        if found:
            return found
    return None


def _argv(**overrides) -> list[str]:
    tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text(encoding="utf-8"))
    task = _find(tasks, VERIFY_TASK)
    assert task, f"task {VERIFY_TASK!r} is gone from the role"
    expr = task["ansible.builtin.command"]["argv"].strip()
    assert expr.startswith("{{") and expr.endswith("}}")
    values = {
        "tn_cosign_path": "/kit/cosign",
        "tn_release_cache_dir": "/cache",
        "tn_cosign_version": "v3.1.3",
        "tn_cosign_platform": "linux-amd64",
        "tn_release_files": "/release",
        "tn_release_cert_identity": "https://example.test/release.yml@refs/tags/v1.2.3",
        "tn_release_cert_oidc_issuer": "https://issuer.test",
        "tn_release_trusted_root": "",
    } | overrides
    return jinja2.Environment().compile_expression(expr[2:-2])(**values)


def test_air_gapped_check_gives_cosign_the_trusted_root_and_no_deprecated_flag():
    argv = _argv(tn_release_trusted_root="/kit/trusted_root.json")
    assert argv[argv.index("--trusted-root") + 1] == "/kit/trusted_root.json"
    assert not [a for a in argv if a.startswith("--offline")], argv
    # Still the same check: the bundle, the signer's identity, and the manifest last.
    assert argv[:2] == ["/kit/cosign", "verify-blob"]
    assert argv[argv.index("--bundle") + 1] == "/release/release-manifest.json.sigstore.json"
    assert argv[argv.index("--certificate-identity") + 1].endswith("@refs/tags/v1.2.3")
    assert argv[argv.index("--certificate-oidc-issuer") + 1] == "https://issuer.test"
    assert argv[-1] == "/release/release-manifest.json"


def test_connected_check_passes_no_trusted_root():
    argv = _argv()
    assert "--trusted-root" not in argv
    assert not [a for a in argv if a.startswith("--offline")], argv
    assert argv[-1] == "/release/release-manifest.json"


def test_without_tn_cosign_path_the_pinned_download_is_used():
    assert _argv(tn_cosign_path="")[0] == "/cache/cosign-v3.1.3-linux-amd64"


def test_docs_do_not_tell_an_operator_to_pass_the_deprecated_flag():
    for rel in ("docs/guides/02-install-guide.md", "install/README.md"):
        assert "--offline=true" not in (ROOT / rel).read_text(encoding="utf-8"), rel
    release = (ROOT / "docs/release.md").read_text(encoding="utf-8")
    assert "verifies with `--offline`" not in release
    assert "--trusted-root" in release
