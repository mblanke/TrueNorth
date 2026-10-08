"""Contract: no site secrets or private keys in tracked text files.

Scans every file ``git ls-files`` reports (binary files skipped) for:

* the internal AI node's LiteLLM master key and LAN address, which were committed
  before v1.0.0 and have been rotated; and
* PEM private-key headers (``-----BEGIN ... PRIVATE KEY-----``).

The patterns are assembled from pieces so this file does not match itself.

Allowlists are explicit and per file:

* ``PEM_FIXTURES`` — tests that build or parse throwaway keys (dummy material only).
* ``PENDING_RUNTIME_SLOT`` — files outside this change's ownership that still carry
  the old default and are being fixed in their own PR. Remove an entry when its file is
  clean; nothing else may be added without the owner's sign-off.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

_KEY = "sk-" + "r7725"
_ADDR = r"133\.1\." + r"14\.240"
SITE_SECRET = re.compile(re.escape(_KEY) + "|" + _ADDR)
PEM_PRIVATE = re.compile("-----BEGIN (?:[A-Z0-9]+ )*" + "PRIVATE KEY-----")

PEM_FIXTURES = frozenset(
    {
        "tests/api/test_sealed_signing_keys.py",
    }
)

PENDING_RUNTIME_SLOT = frozenset(
    {
        "infra/platform/docker/compose.dev.yml",
        "infra/platform/docker/compose.prod.yml",
        "install/inventory/group_vars/all/main.yml",
    }
)


def _tracked_text_files() -> list[tuple[str, str]]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    files = []
    for rel in out.decode().split("\0"):
        if not rel:
            continue
        path = REPO / rel
        try:
            data = path.read_bytes()
        except OSError:  # deleted in the working tree, a submodule, a dangling link
            continue
        if b"\0" in data[:8192]:
            continue
        files.append((rel, data.decode("utf-8", errors="replace")))
    return files


@pytest.fixture(scope="module")
def tracked() -> list[tuple[str, str]]:
    return _tracked_text_files()


def _hits(files, pattern, allow):
    found = []
    for rel, text in files:
        if rel in allow:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                found.append(f"{rel}:{lineno}")
    return found


def test_no_site_llm_key_or_address(tracked):
    hits = _hits(tracked, SITE_SECRET, PENDING_RUNTIME_SLOT)
    assert not hits, (
        "internal AI node key/address committed — use ${LLM_BASE_URL} / ${LLM_API_KEY} "
        "placeholders:\n" + "\n".join(hits)
    )


def test_no_private_keys(tracked):
    hits = _hits(tracked, PEM_PRIVATE, PEM_FIXTURES)
    assert not hits, "private key material committed:\n" + "\n".join(hits)


def test_pattern_self_check():
    assert SITE_SECRET.search("key=" + _KEY + "-local")
    assert SITE_SECRET.search("http://133.1." + "14.240:4000")
    assert PEM_PRIVATE.search("-----BEGIN RSA " + "PRIVATE KEY-----")
    assert PEM_PRIVATE.search("-----BEGIN " + "PRIVATE KEY-----")
    assert PEM_PRIVATE.search("-----BEGIN OPENSSH " + "PRIVATE KEY-----")
