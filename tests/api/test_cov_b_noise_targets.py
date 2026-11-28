# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""What a noise agent may be pointed at (app/noise/targets.py).

Agents are dual-homed (training NIC + management NIC), so a target is a way to make them
connect somewhere. Anything that is not a plain host, address or small subnet is refused,
and so is anything in the address families that only ever mean "not the range": loopback,
link-local (cloud metadata lives there), multicast, unspecified. An inventory scan covers
at most a /24.
"""

from __future__ import annotations

import pytest
from app.noise.targets import MAX_TARGET_LEN, MAX_TARGETS_PER_POOL, MIN_SUBNET_PREFIX, problem, validate


@pytest.mark.parametrize(
    ("pool", "value"),
    [
        ("host", "10.0.5.14"),
        ("host", "srv-dc01"),
        ("host", "srv-dc01.corp.example"),
        ("host", "10.0.5.14:445"),
        ("web", "https://portal.corp.example/login"),
        ("web", "http://10.0.5.20:8080/status"),
        ("subnet", "10.0.7.0/24"),
        ("subnet", "10.0.7.4/24"),  # host bits set: accepted, strict=False
    ],
)
def test_a_plain_target_is_accepted(pool, value):
    assert problem(pool, value) is None


@pytest.mark.parametrize(
    ("pool", "value", "why"),
    [
        ("host", "127.0.0.1", "loopback"),
        ("host", "::1", "loopback"),
        ("host", "169.254.169.254", "link-local"),  # cloud metadata
        ("host", "fe80::1", "link-local"),
        ("host", "224.0.0.1", "multicast"),
        ("host", "0.0.0.0", "unspecified"),
        ("subnet", "127.0.0.0/24", "network"),
        ("subnet", "169.254.0.0/24", "network"),
        ("web", "ftp://portal.example/", "only http/https"),
        ("web", "file:///etc/passwd", "only http/https"),
        ("web", "https://user:pw@portal.example/", "no credentials"),
        ("web", "https://user@portal.example/", "no credentials"),
        ("host", "10.0.5.14:not-a-port", "bad port"),
        ("host", "-leading-hyphen", "not a hostname"),
        ("host", "under_score.example", "not a hostname"),
        ("host", "", "characters"),
    ],
)
def test_what_only_ever_means_not_the_range_is_refused(pool, value, why):
    assert problem(pool, value) is not None and why in problem(pool, value)


def test_a_subnet_must_be_ipv4_and_small():
    assert problem("subnet", "2001:db8::/48") is not None  # not IPv4
    assert problem("subnet", "10.0.0.0/16") is not None  # wider than the cap
    assert f"/{MIN_SUBNET_PREFIX} or smaller" in problem("subnet", "10.0.0.0/16")


def test_not_a_cidr():
    assert "not a CIDR" in problem("subnet", "10.0.0.0/six")


def test_a_port_does_not_confuse_a_hostname_with_one_colon():
    assert problem("host", "srv-dc01:5985") is None
    # An IPv6 literal is not a single-colon shape: it is judged as a hostname and refused.
    assert problem("host", "fe80::1") is not None


def test_a_target_too_long_is_refused():
    long = "a" * (MAX_TARGET_LEN + 1)
    assert "must be 1-" in problem("host", long)


def test_validate_reports_every_problem_with_its_pool():
    errors = validate({"host": ["10.0.5.14", "127.0.0.1"], "elsewhere": ["x"]}, pools={"host", "subnet"})
    assert errors == [
        "unknown target pool 'elsewhere'",
        "host: '127.0.0.1': loopback/link-local/multicast/unspecified address",
    ]


def test_validate_refuses_too_many_targets_in_one_pool():
    errors = validate({"host": [f"host{i}.example" for i in range(MAX_TARGETS_PER_POOL + 1)]}, pools={"host"})
    assert errors == [f"host: at most {MAX_TARGETS_PER_POOL} targets"]


def test_validate_of_valid_targets_is_empty():
    assert validate({"subnet": ["10.0.7.0/24"], "web": ["http://portal.example/"]}, pools={"subnet", "web"}) == []
