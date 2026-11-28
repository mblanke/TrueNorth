# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""tn_noise_agent.__main__ (kept under tests/scripts/ as this card's owned test location):
argparse wiring. A plain-http controller is refused before the token file is even read;
with --allow-http the CLI builds the ControllerClient from the token file, the AgentConfig
from the controller host and management nets, and runs one cycle or forever. The client
and runner are replaced, so no network is touched."""

from __future__ import annotations

import logging
import stat

import pytest
import tn_noise_agent.__main__ as cli


class FakeClient:
    last = None

    def __init__(self, base_url, token, *, timeout=15.0, context=None):
        self.args = (base_url, token, context)
        FakeClient.last = self


class FakeRunner:
    last = None

    def __init__(self, client, cfg, dry=False):
        self.client, self.cfg, self.dry, self.mode = client, cfg, dry, None
        FakeRunner.last = self

    def cycle(self):
        self.mode = "cycle"

    def run_forever(self):
        self.mode = "forever"


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(cli, "ControllerClient", FakeClient)
    monkeypatch.setattr(cli, "Runner", FakeRunner)
    return cli


def _token_file(tmp_path, mode):
    path = tmp_path / "agent.token"
    path.write_text("sekret\n", encoding="utf-8")
    path.chmod(mode)
    return path


def test_plain_http_controller_is_refused(tmp_path):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--controller", "http://127.0.0.1:4200/api", "--token-file", str(tmp_path / "absent.token")])
    assert exc.value.code == 2  # refused before the token file is stat'ed


def test_cli_builds_client_config_and_runner_from_the_flags(tmp_path, wired):
    tok = _token_file(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
    wired.main(
        [
            "--controller",
            "http://127.0.0.1:4200/api",
            "--token-file",
            str(tok),
            "--allow-http",
            "--domain",
            "lab.local",
            "--mgmt-net",
            "10.0.0.0/8",
            "--dry-run",
            "--once",
        ]
    )
    runner = FakeRunner.last
    assert runner is not None
    assert runner.client.args == ("http://127.0.0.1:4200/api", "sekret", None)  # token stripped
    assert runner.cfg.domain == "lab.local"
    assert runner.cfg.forbidden_hosts == frozenset({"127.0.0.1"})  # the controller host
    assert runner.cfg.forbidden_nets == ("10.0.0.0/8",)
    assert runner.dry is True
    assert runner.mode == "cycle"


def test_default_is_forever_and_https_needs_no_override(tmp_path, wired):
    tok = _token_file(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
    wired.main(["--controller", "https://controller.lab/api", "--token-file", str(tok)])
    runner = FakeRunner.last
    assert runner.mode == "forever"
    assert runner.dry is False
    assert runner.cfg.forbidden_hosts == frozenset({"controller.lab"})
    assert runner.cfg.forbidden_nets == ()


def test_a_world_readable_token_file_is_warned_about(tmp_path, wired, caplog):
    tok = _token_file(tmp_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    with caplog.at_level(logging.WARNING):
        wired.main(
            [
                "--controller",
                "http://127.0.0.1:4200/api",
                "--token-file",
                str(tok),
                "--allow-http",
                "--once",
            ]
        )
    assert "0600" in caplog.text and str(tok) in caplog.text
