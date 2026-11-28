# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""scripts/load_content.py: the pure parsing and loading steps that run against the API —
the top-scalar/top-list/nested-scalar YAML readers (comments, quoting, block scalars), the
Report marks (ok/skip/FAIL with detail and the failure count), names_of's non-200 and
non-list degradation, load_yaml_items' skip-existing/create-and-map behaviour, the Sigma
detection field mapping (level/status validation, description folding, mitre ids from
techniques or tags), and load_demo's idempotency and missing-template skip. main() is not
exercised here: it needs a live API on the other end of Api's urllib calls, and the
curriculum/golden-image steps need the real content pack files, which stays a no-network
boundary this clone does not cross."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("cov_f_load_content", ROOT / "scripts" / "load_content.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class FakeApi:
    """get/post_json/post_files recording fake; answers come from canned dicts."""

    def __init__(self, gets=None, posts=None):
        self.gets = gets or {}
        self.posts = posts or {}
        self.post_calls: list[tuple[str, object]] = []
        self.file_calls: list[tuple[str, dict]] = []

    def get(self, path):
        return self.gets.get(path, (404, {"detail": "nope"}))

    def post_json(self, path, payload=None):
        self.post_calls.append((path, payload))
        return self.posts.get(path, (200, {"id": "new-1"}))

    def post_files(self, path, files):
        self.file_calls.append((path, files))
        return 200, {"ok": True}


# ── the tiny YAML readers ────────────────────────────────────────────────────────────


def test_top_scalar_reads_quoted_commented_and_block_values():
    text = """
name: 'SOC Drill'
version: "2.1"
bare: value with spaces
commented: keep   # trailing comment
block: |
  folded body
  second body line
empty_last:
"""
    assert mod.top_scalar(text, "name") == "SOC Drill"
    assert mod.top_scalar(text, "version") == "2.1"
    assert mod.top_scalar(text, "bare") == "value with spaces"
    assert mod.top_scalar(text, "commented") == "keep"
    # The block-scalar sentinel yields None (not a one-line value), even with a body.
    assert mod.top_scalar(text, "block") is None
    # An empty value with nothing after it degrades to None; so does a missing key.
    assert mod.top_scalar(text, "empty_last") is None
    assert mod.top_scalar(text, "missing") is None


def test_top_list_reads_indented_dash_items_and_strips_noise():
    text = """
tags:
  - attack.t1555
  - 'card.access'
  - keep   # trailing comment
# a full-line comment after the block
other: x
"""
    assert mod.top_list(text, "tags") == ["attack.t1555", "card.access", "keep"]
    assert mod.top_list(text, "other") == []  # scalars are not lists
    assert mod.top_list(text, "missing") == []


def test_nested_scalar_reads_one_level_under_its_parent():
    text = """
logsource:
  category: process_creation
  product: windows
  service: 'sysmon'
elsewhere:
  category: not-this-one
"""
    assert mod.nested_scalar(text, "logsource", "category") == "process_creation"
    assert mod.nested_scalar(text, "logsource", "service") == "sysmon"
    assert mod.nested_scalar(text, "logsource", "missing") is None
    assert mod.nested_scalar(text, "absent", "category") is None


# ── Report ───────────────────────────────────────────────────────────────────────────


def test_report_marks_ok_skip_and_fail_and_counts_failures(capsys):
    rep = mod.Report()
    rep.line(200, "good step")
    rep.line(201, "created step")
    rep.line("skip", "already there")
    rep.line(500, "boom", {"detail": "why it broke"})
    rep.line(403, "refused", "plain text detail")
    rep.line(422, "no detail given")
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "  ok   good step"
    assert out[1] == "  ok   created step"
    assert out[2] == "  skip already there"
    assert out[3] == "  FAIL boom  {'detail': 'why it broke'}"
    assert out[4] == "  FAIL refused  plain text detail"
    # A FAIL with no detail prints no dangling suffix.
    assert out[5] == "  FAIL no detail given"
    assert rep.failures == 3
    # A non-2xx int is a failure; "skip" and 2xx are not.
    rep.line("skip", "more skip")
    assert rep.failures == 3


# ── names_of ─────────────────────────────────────────────────────────────────────────


def test_names_of_indexes_the_field_and_degrades_on_error():
    api = FakeApi(gets={"/things": (200, [{"id": 1, "name": "A"}, {"id": "2", "name": "B"}])})
    assert mod.names_of(api, "/things", "name") == {"A": {"id": 1, "name": "A"}, "B": {"id": "2", "name": "B"}}
    api = FakeApi(gets={"/things": (200, "gateway text")})
    assert mod.names_of(api, "/things", "name") == {}  # a non-list body maps nothing
    api = FakeApi(gets={"/things": (503, "down")})
    assert mod.names_of(api, "/things", "name") == {}


# ── load_yaml_items ──────────────────────────────────────────────────────────────────


def test_load_yaml_items_skips_existing_and_maps_new_ids(tmp_path, capsys):
    existing = (200, [{"id": "tpl-1", "name": "Existing"}])
    created = (201, {"id": "tpl-2", "name": "New One"})
    api = FakeApi(gets={"/templates": existing}, posts={"/templates": created})
    old = tmp_path / "oldtpl" / "template.yaml"
    old.parent.mkdir()
    old.write_text("id: oldtpl-slug\nname: Existing\nversion: '3.0'\n", encoding="utf-8")
    new = tmp_path / "newtpl" / "template.yaml"
    new.parent.mkdir()
    new.write_text("name: New One\n", encoding="utf-8")  # no id: the parent dir name stands in

    ids = mod.load_yaml_items(api, mod.Report(), "Range templates", "/templates", [old, new])
    assert api.post_calls == [
        (
            "/templates",
            {
                "name": "New One",
                "version": "1.0",
                "yaml": "name: New One\n",
                "is_public": True,
            },
        )
    ]
    out = capsys.readouterr().out
    assert "Range templates" in out
    assert "  skip Existing (exists)" in out
    assert "  ok   New One" in out
    # The created id is indexed under the yaml name, the slug and the directory name.
    assert ids["New One"] == "tpl-2"
    assert ids["newtpl"] == "tpl-2"
    assert ids["Existing"] == "tpl-1"


def test_load_yaml_items_falls_back_to_the_parent_directory_name(tmp_path, capsys):
    # A file with neither name nor id: everything derives from the directory.
    fp = tmp_path / "dirname" / "template.yaml"
    fp.parent.mkdir()
    fp.write_text("steps: []\n", encoding="utf-8")
    api = FakeApi(gets={"/templates": (200, [])}, posts={"/templates": (201, {"id": "tpl-9"})})
    ids = mod.load_yaml_items(api, mod.Report(), "T", "/templates", [fp])
    assert api.post_calls[0][1]["name"] == "dirname"
    assert ids["dirname"] == "tpl-9"


# ── load_detections ──────────────────────────────────────────────────────────────────


def sigma(title="Suspicious Process", **extra):
    lines = [
        f"title: {title}",
        "id: 9a1b-2c3d",
        "description: |",
        "  Looks for a process",
        "  that should not run.",
        "level: HIGH",
        "status: testing",
        "logsource:",
        "  category: process_creation",
        "  product: windows",
        "tags:",
        "  - attack.t1059",
    ]
    lines += [f"{k}: {v}" for k, v in extra.items()]
    return "\n".join(lines) + "\n"


def test_load_detections_posts_the_sigma_fields(tmp_path, monkeypatch, capsys):
    det = tmp_path / "detections"  # load_detections globs CONTENT / "detections"
    det.mkdir()
    (det / "suspicious.yaml").write_text(sigma(), encoding="utf-8")
    monkeypatch.setattr(mod, "CONTENT", tmp_path)  # point the loader at the synthetic rules
    api = FakeApi(gets={"/detection-rules": (200, [])}, posts={"/detection-rules": (201, {"id": "dr-1"})})
    mod.load_detections(api, mod.Report())
    ((path, payload),) = api.post_calls
    assert path == "/detection-rules"
    assert payload["title"] == "Suspicious Process"
    assert payload["sigma_id"] == "9a1b-2c3d"
    assert payload["level"] == "high"  # lower-cased
    assert payload["status"] == "testing"
    assert payload["description"] == "Looks for a process that should not run."  # folded
    assert payload["logsource_category"] == "process_creation"
    assert payload["logsource_product"] == "windows"
    assert payload["logsource_service"] is None
    assert payload["detection_yaml"].startswith("title: Suspicious")
    assert payload["mitre_attack_ids"] == ["attack.t1059"]
    assert payload["false_positives"] is None
    assert payload["tags"] is None
    assert "  ok   Suspicious Process" in capsys.readouterr().out


def test_load_detections_validates_level_status_and_falls_back_for_mitre_ids(tmp_path, monkeypatch, capsys):
    det = tmp_path / "detections"  # load_detections globs CONTENT / "detections"
    det.mkdir()
    (det / "weird.yaml").write_text(
        "title: Weird\nlevel: absurd\nstatus: half-baked\nseverity: critical\ntags:\n  - attack.t1003\n  - wl.script\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "CONTENT", tmp_path)
    api = FakeApi(gets={"/detection-rules": (200, [])})
    mod.load_detections(api, mod.Report())
    payload = api.post_calls[0][1]
    assert payload["level"] == "medium"  # an unknown level degrades to medium
    assert payload["status"] == "draft"  # an unknown status degrades to draft
    # mitre ids fall back to the tag list when no technique list is given.
    assert payload["mitre_attack_ids"] == ["attack.t1003", "wl.script"]
    assert "  ok   Weird" in capsys.readouterr().out


def test_load_detections_skips_existing_titles_and_defaults(tmp_path, monkeypatch, capsys):
    det = tmp_path / "detections"  # load_detections globs CONTENT / "detections"
    det.mkdir()
    (det / "known.yaml").write_text("title: Known Rule\n", encoding="utf-8")
    (det / "bare.yaml").write_text("description: a bare one-liner\n", encoding="utf-8")
    monkeypatch.setattr(mod, "CONTENT", tmp_path)
    api = FakeApi(gets={"/detection-rules": (200, [{"id": "dr-0", "title": "Known Rule"}])})
    mod.load_detections(api, mod.Report())
    assert len(api.post_calls) == 1  # only the unknown rule is posted
    payload = api.post_calls[0][1]
    assert payload["title"] == "bare"  # the file stem stands in for a missing title
    assert payload["level"] == "medium" and payload["status"] == "draft"
    assert payload["description"] == "a bare one-liner"
    assert payload["sigma_id"] is None and payload["mitre_attack_ids"] is None
    out = capsys.readouterr().out
    assert "  skip Known Rule (exists)" in out
    assert "  ok   bare" in out


# ── load_demo ────────────────────────────────────────────────────────────────────────


def demo_apis(existing_users=(), existing_ranges=(), existing_exercises=()):
    gets = {
        "/users": (200, [{"id": "u-1", "email": e} for e in existing_users]),
        "/ranges": (200, [{"id": "r-1", "name": n} for n in existing_ranges]),
        "/exercises": (200, [{"id": "e-1", "name": n} for n in existing_exercises]),
    }
    return FakeApi(gets=gets)


def test_load_demo_creates_people_and_pairs_ranges_with_scenarios(capsys):
    api = demo_apis()
    templates = {"soc-training": "tpl-1", "small-enterprise": "tpl-2"}
    scenarios = {"incident-response-drill": "scn-1", "ransomware-lite": "scn-2"}
    mod.load_demo(api, mod.Report(), templates, scenarios)
    posts = api.post_calls
    # Five demo people first, then range, provision and exercise per pair.
    assert [p[1]["email"] for p in posts[:5]] == [
        "instructor.demo@truenorth.test",
        "trainee1.demo@truenorth.test",
        "trainee2.demo@truenorth.test",
        "trainee3.demo@truenorth.test",
        "observer.demo@truenorth.test",
    ]
    assert posts[5] == ("/ranges", {"name": "Demo — SOC analyst lab", "template_id": "tpl-1"})
    assert posts[6] == ("/ranges/new-1/provision", None)
    assert posts[7] == (
        "/exercises",
        {"name": "Demo — SOC analyst lab — exercise", "range_id": "new-1", "scenario_id": "scn-1"},
    )
    out = capsys.readouterr().out
    assert "Demo people, ranges and exercises" in out
    assert "  ok   Demo Instructor (instructor)" in out
    assert "  ok   range Demo — SOC analyst lab" in out
    assert "  ok     provision (mock) Demo — SOC analyst lab" in out
    assert "  ok   exercise Demo — SOC analyst lab — exercise" in out


def test_load_demo_skips_what_exists_and_what_is_missing(capsys):
    api = demo_apis(existing_users=["instructor.demo@truenorth.test"], existing_ranges=["Demo — SOC analyst lab"])
    mod.load_demo(api, mod.Report(), {"soc-training": "tpl-1"}, {})  # no scenario ids at all
    assert len(api.post_calls) == 4  # only the four missing people; ranges and exercises skip
    out = capsys.readouterr().out
    assert "  skip Demo Instructor (exists)" in out
    assert "  skip Demo — SOC analyst lab (template or scenario missing)" in out
    assert "  skip Demo — Small enterprise (template or scenario missing)" in out


def test_load_demo_reuses_an_existing_range_without_provisioning_again(capsys):
    api = demo_apis(existing_ranges=["Demo — SOC analyst lab"])
    templates = {"soc-training": "tpl-1", "small-enterprise": "tpl-2"}
    scenarios = {"incident-response-drill": "scn-1", "ransomware-lite": "scn-2"}
    mod.load_demo(api, mod.Report(), templates, scenarios)
    paths = [p for p, _ in api.post_calls]
    # The SOC range already exists: it is not re-created and never provisioned again;
    # the Small enterprise range is new and gets both.
    assert ("/ranges", {"name": "Demo — SOC analyst lab", "template_id": "tpl-1"}) not in api.post_calls
    assert "/ranges/r-1/provision" not in paths
    assert paths.count("/ranges") == 1
    assert paths.count("/ranges/new-1/provision") == 1
    assert paths.count("/exercises") == 2
    out = capsys.readouterr().out
    assert "  skip range Demo — SOC analyst lab (exists)" in out


def test_load_demo_skips_the_exercise_when_the_range_creation_has_no_id(capsys):
    # A range POST that returns no id: the exercise step is skipped silently, not crashed on.
    api = demo_apis()
    templates = {"soc-training": "tpl-1"}
    scenarios = {"incident-response-drill": "scn-1"}
    api.post_json = lambda path, payload=None: (200, {})  # no id in any body
    mod.load_demo(api, mod.Report(), templates, scenarios)
    out = capsys.readouterr().out
    assert "  ok   range Demo — SOC analyst lab" in out
    assert "exercise Demo — SOC analyst lab — exercise" not in out


def test_api_post_files_builds_a_multipart_body(tmp_path):
    fp = tmp_path / "upload.csv"
    fp.write_bytes(b"a,b\n1,2\n")
    api = mod.Api("http://api.test/", "tok-1")  # trailing slash is stripped, token set
    api._send = lambda method, path, body, ctype: (method, path, body, ctype)
    method, path, body, ctype = api.post_files("/import", {"file": fp})
    assert method == "POST" and path == "/import"
    boundary = ctype.partition("boundary=")[2]
    assert ctype.startswith("multipart/form-data; boundary=")
    assert f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="upload.csv"'.encode() in body
    assert b"a,b\n1,2\n" in body
    assert body.endswith(f"--{boundary}--\r\n".encode())
    # No token: no Authorization header is prepared.
    bare = mod.Api("http://api.test", None)
    assert bare.headers == {}


def test_load_yaml_items_still_creates_when_the_listing_fails(tmp_path, capsys):
    # The listing itself 404s: nothing exists, the file is posted, its id is mapped.
    fp = tmp_path / "d" / "template.yaml"
    fp.parent.mkdir()
    fp.write_text("name: Lonely\n", encoding="utf-8")
    api = FakeApi()  # every get 404s
    ids = mod.load_yaml_items(api, mod.Report(), "T", "/templates", [fp])
    assert ids["Lonely"] == "new-1"  # the create still succeeded (default 200 {"id": new-1})
    assert "  ok   Lonely" in capsys.readouterr().out
