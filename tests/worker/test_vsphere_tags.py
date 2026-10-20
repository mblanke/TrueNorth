"""vsphere_tags: range labels, the tags of a range VM, and the CIS tagging client in both
REST dialects, against an in-memory tagging service that enforces single cardinality."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from vsphere_cis_fake import FakeCis
from worker.provisioners import vsphere_tags as tags

ID = "2fc8d04d-1111-2222-3333-444455556666"


def _run(coro):
    return asyncio.run(coro)


KEY = "2fc8d04d1111"


def test_range_key_is_12_hex_digits():
    assert tags.range_key(ID) == KEY and tags.range_key(ID.upper()) == KEY


@pytest.mark.parametrize(("name", "want"), [
    ("Blue Team 3", f"blue-team-3-{KEY}"),
    ("  ARC²  Week/1 ", f"arc-week-1-{KEY}"),
    ("", KEY),
    (None, KEY),
    ("!!!", KEY),
    (f"lab-{KEY}", f"lab-{KEY}"),  # already carries the key: not twice
    (KEY, KEY),
    ("x" * 90, "x" * 40 + f"-{KEY}"),
])
def test_range_label(name, want):
    assert tags.range_label(name, ID) == want


@pytest.mark.parametrize(("folder", "hit", "legacy"), [
    (f"blue-team-{KEY}", True, False), (KEY, True, False), (f"BLUE-{KEY.upper()}", True, False),
    ("2fc8d04d", True, True),  # an earlier build's <id8> folder: found, flagged legacy
    ("blue-2fc8d04d", False, False),  # 8 digits after a name is not a key
    ("x-2fc8d04d9999", False, False),  # same id8, another range's key
    (f"{KEY}-x", False, False), (f"x{KEY}", False, False), ("0badc0de", False, True), ("", False, False),
])
def test_folders_match_by_the_12_hex_key_or_an_exact_id8(folder, hit, legacy):
    assert tags.belongs_to(folder, ID) is hit
    assert tags.is_legacy_folder(folder) is legacy


@pytest.mark.parametrize(("source", "want"), [
    ("ubuntu-lts", "ubuntu-lts"), ("ubuntu-lts@esx02-20261020", "ubuntu-lts"),
    ("library:srv2022", "srv2022"), ("", ""), (None, ""),
])
def test_template_id(source, want):
    assert tags.template_id(source) == want


def test_range_vm_tags():
    vm = {"os": "windows-server-2022", "template_name": "srv2022", "_source": "srv2022@esx03-20261019"}
    assert tags.range_vm_tags(vm, "r-2fc8d04d") == {
        "tn-type": "range-vm", "tn-range": "r-2fc8d04d", "tn-os": "windows-server-2022", "tn-template": "srv2022"}
    assert tags.range_vm_tags({}, "r") == {"tn-type": "range-vm", "tn-range": "r"}  # unknown OS/template: left out


def test_categories_and_types_are_the_published_set():
    assert set(tags.CATEGORIES) == {"tn-type", "tn-os", "tn-template", "tn-range"}
    assert tags.TYPES == ("golden-template", "template-copy", "range-vm", "test-clone", "depot", "mgmt", "retired")


# -- TagClient ----------------------------------------------------------------------------


def _client(cis: FakeCis, dialect: str) -> tags.TagClient:
    async def call(method, path, body):
        async with httpx.AsyncClient(transport=httpx.MockTransport(cis), base_url="https://vc") as c:
            r = await c.request(method, path, **({"json": body} if body is not None else {}))
            r.raise_for_status()
            return r.json() if r.content else None

    return tags.TagClient(call, dialect)


@pytest.fixture(params=["api", "rest"])
def dialect(request):
    return request.param


def test_apply_creates_categories_and_tags_once(dialect):
    cis = FakeCis()
    client = _client(cis, dialect)
    _run(client.apply("vm-1", {"tn-type": "range-vm", "tn-range": "r-1"}))
    _run(client.apply("vm-2", {"tn-type": "range-vm", "tn-range": "r-1"}))
    assert cis.tags_of("vm-1") == cis.tags_of("vm-2") == {"tn-type": "range-vm", "tn-range": "r-1"}
    assert sorted(c["name"] for c in cis.categories.values()) == ["tn-range", "tn-type"]
    assert len(cis.tags) == 2
    prefix = "/rest/com/vmware/cis/tagging/" if dialect == "rest" else "/api/cis/tagging/"
    assert all(p.startswith(prefix) for _, p in cis.calls)


def test_a_value_already_set_in_a_single_category_is_replaced(dialect):
    cis = FakeCis()
    old = cis.add_tag("tn-type", "test-clone")
    other = cis.add_tag("owner", "someone")  # not ours: left alone
    cis.attached["vm-1"] = {old, other}
    _run(_client(cis, dialect).apply("vm-1", {"tn-type": "golden-template"}))
    assert cis.tags_of("vm-1") == {"tn-type": "golden-template", "owner": "someone"}


def test_applying_the_same_tags_again_changes_nothing(dialect):
    cis = FakeCis()
    _run(_client(cis, dialect).apply("vm-1", {"tn-type": "mgmt"}))
    cis.calls.clear()
    _run(_client(cis, dialect).apply("vm-1", {"tn-type": "mgmt"}))
    assert not [p for m, p in cis.calls if "attach" in p and "list-attached" not in p]


def test_a_category_made_meanwhile_by_another_worker_is_used(dialect):
    cis = FakeCis()
    client = _client(cis, dialect)
    _run(client._category_ids())  # listed before the other worker created it
    theirs = cis.add_category("tn-os")
    _run(client.apply("vm-1", {"tn-os": "pfsense"}))
    assert cis.tags[next(iter(cis.attached["vm-1"]))]["category_id"] == theirs


def test_a_tag_made_meanwhile_by_another_worker_is_used(dialect):
    cis = FakeCis()
    client = _client(cis, dialect)
    _run(client.ensure({"tn-os": "kali"}))
    theirs = cis.add_tag("tn-os", "parrot-security")
    _run(client.apply("vm-1", {"tn-os": "parrot-security"}))
    assert cis.attached["vm-1"] == {theirs}


def test_a_failure_stops_tagging_for_the_build_with_one_warning(dialect, caplog):
    cis = FakeCis()
    cis.fail = "attach"
    client = _client(cis, dialect)
    caplog.set_level("WARNING", logger=tags.__name__)
    assert _run(client.apply("vm-1", {"tn-type": "range-vm"})) is False  # never raises
    cis.fail = None
    before = len(cis.calls)
    assert _run(client.apply("vm-2", {"tn-type": "range-vm"})) is False
    assert len(cis.calls) == before  # nothing more is tried this build
    assert caplog.text.count("vCenter tagging stopped") == 1 and client.stopped


def test_a_slow_call_times_out_and_stops_tagging(dialect):
    async def slow(method, path, body):
        await asyncio.sleep(1)

    client = tags.TagClient(slow, dialect, timeout=0.01)
    assert _run(client.apply("vm-1", {"tn-type": "range-vm"})) is False
    assert "longer than 0.01s" in client.stopped


def test_nothing_is_tagged_past_the_deadline(dialect):
    cis = FakeCis()
    now = [100.0]
    client = _client(cis, dialect)
    client._deadline, client._clock = 150.0, lambda: now[0]
    assert _run(client.apply("vm-1", {"tn-type": "range-vm"})) is True
    now[0] = 151.0
    calls = len(cis.calls)
    assert _run(client.apply("vm-2", {"tn-type": "range-vm"})) is False
    assert len(cis.calls) == calls and "budget" in client.stopped


def test_delete_range_tags_only_that_ranges(dialect):
    cis = FakeCis()
    mine = cis.add_tag("tn-range", f"blue-{KEY}")
    bare = cis.add_tag("tn-range", KEY)
    twin = cis.add_tag("tn-range", "red-2fc8d04d9999")  # same id8, another range: kept
    old8 = cis.add_tag("tn-range", "2fc8d04d")  # an 8-digit name: could be any range of that id8
    same_name_other_cat = cis.add_tag("tn-template", f"x-{KEY}")
    gone = _run(_client(cis, dialect).delete_range_tags(ID))
    assert sorted(gone) == sorted([f"blue-{KEY}", KEY])
    assert mine not in cis.tags and bare not in cis.tags
    assert twin in cis.tags and old8 in cis.tags and same_name_other_cat in cis.tags


def test_delete_range_tags_without_the_category_does_nothing(dialect):
    cis = FakeCis()
    assert _run(_client(cis, dialect).delete_range_tags(ID)) == []
    assert cis.categories == {}
