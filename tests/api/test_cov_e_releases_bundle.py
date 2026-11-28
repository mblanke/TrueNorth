# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""Round-2 coverage for app/course_releases/bundle.py: every refusal a malformed upload gets.

``parse`` trusts nothing in the tarball. These tests start from a real release built by
the release kit (the happy path) and then mutate it — a tampered byte, a dropped file, a
forged release.json field — asserting the exact refusal each mutation earns.
"""

import gzip
import hashlib
import io
import json
import tarfile

import pytest
import yaml
from _release_kit import build as build_release
from app.course_releases import bundle as bundle_mod
from app.course_releases.bundle import PARTS, BundleError, parse, release_digest

# -- tarball plumbing --------------------------------------------------------


def untar(data: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data)), mode="r:") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar if m.isfile()}


def tar_of(members: dict[str, bytes], *, specials: tuple[str, ...] = ()) -> bytes:
    """Gzip a tar of the given regular files; ``specials`` are added as symlinks."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:") as tar:
        for name in sorted(members):
            blob = members[name]
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(blob))
        for name in specials:
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = "elsewhere"
            tar.addfile(info)
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return buf.getvalue()


def reforge(data, *, edit_files=None, drop=(), meta_edit=None, part_digest_override=None) -> bytes:
    """Rebuild the tarball after editing members and/or release.json, recomputing every
    digest the way the release tool would — so a test fails for the reason it targets."""
    members = untar(data)
    for path in drop:
        members.pop(path)
    for path, blob in (edit_files or {}).items():
        members[path] = blob if isinstance(blob, bytes) else blob.encode()
    meta = json.loads(members["release.json"])
    for name in PARTS:
        part_files = {p[len(name) + 1 :]: b for p, b in members.items() if p.startswith(f"{name}/")}
        meta["parts"][name] = {
            "digest": bundle_mod.digest_files(part_files),
            "files": [
                {"path": rel, "sha256": hashlib.sha256(blob).hexdigest()} for rel, blob in sorted(part_files.items())
            ],
        }
    meta.update(meta_edit or {})
    for name, digest in (part_digest_override or {}).items():
        meta["parts"][name]["digest"] = digest
    meta["release_digest"] = release_digest({n: meta["parts"][n]["digest"] for n in PARTS}, meta)
    members["release.json"] = json.dumps(meta).encode()
    return tar_of(members)


def with_members(data, *, edit_files=None, drop=()) -> bytes:
    """Edit members without touching release.json — for forgeries that are not even JSON."""
    members = untar(data)
    for path in drop:
        members.pop(path)
    for path, blob in (edit_files or {}).items():
        members[path] = blob if isinstance(blob, bytes) else blob.encode()
    return tar_of(members)


def edit_manifest(data, fn) -> bytes:
    members = untar(data)
    manifest = json.loads(members["platform/manifest.json"])
    fn(manifest)
    return reforge(data, edit_files={"platform/manifest.json": json.dumps(manifest)})


def edit_course(data, fn) -> bytes:
    members = untar(data)
    doc = yaml.safe_load(members["platform/02-content/arc2-iot.yaml"])
    fn(doc)
    return reforge(
        data, edit_files={"platform/02-content/arc2-iot.yaml": yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)}
    )


@pytest.fixture(scope="module")
def release(tmp_path_factory) -> bytes:
    return build_release(tmp_path_factory.mktemp("cov-e-plain"))


@pytest.fixture(scope="module")
def ranged_release(tmp_path_factory) -> bytes:
    return build_release(tmp_path_factory.mktemp("cov-e-ranged"), range_ordinals=frozenset({1}))


def refuse(data, *fragments):
    with pytest.raises(BundleError) as exc:
        parse(data)
    for fragment in fragments:
        assert fragment in str(exc.value), str(exc.value)


# -- gzip and tar shape ------------------------------------------------------


def test_the_kit_release_parses(release):
    bundle = parse(release)
    assert bundle.meta["slug"] == "arc2-iot"
    assert bundle.sha256 == hashlib.sha256(release).hexdigest()
    assert bundle.course["course_code"] == "ARC2-IOT"


def test_truncated_gzip_is_refused(release):
    refuse(release[:-40], "not a gzipped tar")


def test_data_that_is_not_gzip_at_all_is_refused():
    refuse(b"this is not a tarball", "not a gzipped tar")


def test_gzip_that_is_not_a_tar_is_refused():
    refuse(gzip.compress(b"not a tar at all"), "not a gzipped tar")


def test_an_upload_over_the_byte_limit_is_refused(release, monkeypatch):
    monkeypatch.setattr(bundle_mod, "MAX_BYTES", 10)
    refuse(release, "bytes; the limit is 10")


def test_an_upload_that_expands_past_the_size_limit_is_refused(monkeypatch):
    monkeypatch.setattr(bundle_mod, "MAX_BYTES", 1000)  # the compressed upload is smaller than this
    big = tar_of({"platform/a.txt": b"a" * 3000, "platform/b.txt": b"b" * 3000})
    assert len(big) < 1000
    refuse(big, "expands beyond the size limit")


def test_more_members_than_the_limit_are_refused(monkeypatch):
    monkeypatch.setattr(bundle_mod, "MAX_MEMBERS", 1)
    refuse(tar_of({"release.json": b"{}", "platform/x.txt": b"x"}), "more than 1 files")


def test_a_non_regular_member_is_refused(release):
    members = untar(release)
    refuse(tar_of(members, specials=("learner/02-content/mod_001/content/link.html",)), "only regular files")


def test_an_unsafe_path_is_refused():
    refuse(tar_of({"learner/../evil.txt": b"x"}), "unsafe path")


def test_a_duplicated_member_is_refused():
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:") as tar:
        for _ in range(2):
            info = tarfile.TarInfo("learner/dup.txt")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    refuse(buf.getvalue(), "duplicated")


# -- release.json ------------------------------------------------------------


def test_a_tarball_without_release_json_is_refused(release):
    members = untar(release)
    members.pop("release.json")
    refuse(tar_of(members), "release.json is missing")


def test_release_json_that_is_not_json_is_refused(release):
    refuse(with_members(release, edit_files={"release.json": "{oops"}), "not JSON")


def test_release_json_with_the_wrong_schema_is_refused(release):
    refuse(reforge(release, meta_edit={"schema": "arc2/release/9.9"}), "schema must be")


def test_release_json_missing_a_required_key_is_refused(release):
    refuse(reforge(release, meta_edit={"run_id": ""}), "has no run_id")


def test_a_member_outside_the_three_parts_is_refused(release):
    refuse(reforge(release, edit_files={"extra/notes.txt": "hi"}), "outside learner/, platform/ and instructor/")


def test_a_file_the_part_does_not_allow_is_refused(release):
    refuse(
        reforge(release, edit_files={"platform/02-content/mod_001/notes.txt": "x"}), "not allowed in the platform part"
    )


def test_instructor_material_in_the_learner_part_is_refused(release):
    refuse(
        reforge(release, edit_files={"learner/02-content/mod_001/content/answer_key.md": "Q1: B"}),
        "instructor material in the learner part",
    )


def test_tampered_files_are_named_in_the_refusal(release):
    refuse(
        _tampered(release),
        "learner files do not match release.json",
        "changed",
    )


def _tampered(data: bytes) -> bytes:
    """Reforge but leave the declared file digests stale, like a forger who forgot them."""
    members = untar(data)
    members["learner/02-content/mod_001/content/page-01.html"] = b"<h2>tampered</h2>"
    return tar_of(members)


def test_a_forged_part_digest_is_refused(release):
    refuse(reforge(release, part_digest_override={"learner": "f" * 64}), "learner digest does not match its files")


def test_an_empty_learner_part_is_refused(release):
    learner = [p for p in untar(release) if p.startswith("learner/")]
    assert learner
    refuse(reforge(release, drop=learner), "the learner part is empty")


# -- the run's manifest ------------------------------------------------------


def test_a_release_without_the_manifest_is_refused(release):
    refuse(reforge(release, drop={"platform/manifest.json"}), "platform/manifest.json is missing")


def test_a_manifest_that_is_not_json_is_refused(release):
    refuse(reforge(release, edit_files={"platform/manifest.json": "{oops"}), "manifest.json")


def test_a_manifest_that_is_not_an_object_is_refused(release):
    refuse(reforge(release, edit_files={"platform/manifest.json": "[]"}), "not an object")


def test_a_release_naming_another_course_is_refused(release):
    refuse(reforge(release, meta_edit={"arc2_code": "ARC2-OTHER"}), "different course than the run's manifest")


def test_a_release_whose_qa_did_not_pass_is_refused(release):
    def fail_qa(manifest):
        manifest["qa"]["result"] = "fail"

    refuse(edit_manifest(release, fail_qa), "QA did not pass")


def test_a_gate_not_accepted_on_the_named_digest_is_refused(release):
    def reopen(manifest):
        manifest["gates"]["outline"]["accepted_sha256"] = "9" * 64

    refuse(edit_manifest(release, reopen), "outline gate is not accepted on the digest")


def test_open_actions_that_differ_from_the_run_are_refused(release):
    refuse(reforge(release, meta_edit={"open_human_actions": [{"id": "ghost"}]}), "open actions differ")


def test_a_non_boolean_auto_accepted_is_refused(release):
    refuse(reforge(release, meta_edit={"auto_accepted": "yes"}), "auto_accepted must be true or false")


def test_an_auto_accepted_gate_must_be_declared_in_release_json(release):
    def auto(manifest):
        manifest["gates"]["outline"]["accepted_by"] = bundle_mod.AUTO_ACCEPTED_BY

    refuse(edit_manifest(release, auto), "accepted by the test host's runner")


def test_a_declared_auto_acceptance_parses(release):
    bundle = parse(_auto_declared(release))
    assert bundle_mod.auto_accepted(bundle.meta) is True


def _auto_declared(data: bytes) -> bytes:
    def auto(manifest):
        manifest["gates"]["outline"]["accepted_by"] = bundle_mod.AUTO_ACCEPTED_BY

    return reforge(edit_manifest(data, auto), meta_edit={"auto_accepted": True})


# -- activities: three-way agreement ----------------------------------------


def test_an_unknown_activity_kind_is_refused(release):
    acts = dict(parse(release).meta["activities"])
    acts["mod_001"] = "fieldtrip"
    refuse(reforge(release, meta_edit={"activities": acts}), "theory, practical or range")


def test_a_manifest_module_without_a_mod_id_is_refused(release):
    def rename(manifest):
        manifest["content"]["modules"][0]["id"] = "m1"

    refuse(edit_manifest(release, rename), "module id 'm1' is not mod_NNN")


def test_a_manifest_module_with_no_activity_is_refused(release):
    def strip(manifest):
        manifest["content"]["modules"][0]["activity"] = {}

    refuse(edit_manifest(release, strip), "declares no activity")


def test_course_modules_that_differ_from_the_run_are_refused(release):
    def drop_module_two(doc):
        doc["modules"] = [m for m in doc["modules"] if m["ordinal"] != 2]

    refuse(edit_course(release, drop_module_two), "course file's modules differ from the run's modules")


def test_an_activity_the_course_file_disagrees_with_is_refused(release):
    acts = dict(parse(release).meta["activities"])
    acts["mod_002"] = "practical"

    def practical(manifest):
        for m in manifest["content"]["modules"]:
            if m["id"] == "mod_002":
                m["activity"] = {"kind": "practical", "no_range_reason": "none"}

    refuse(
        reforge(edit_manifest(release, practical), meta_edit={"activities": acts}),
        "mod_002 is practical in the run but",
    )


# -- the lab profile ---------------------------------------------------------


def test_a_release_without_range_work_may_not_carry_a_lab_profile(release):
    refuse(
        reforge(release, meta_edit={"lab_profile": "03-range/lab_profile.yaml"}),
        "no range activity carries a lab profile",
    )


def test_range_activities_need_a_lab_profile(ranged_release):
    profile = [p for p in untar(ranged_release) if p == "platform/03-range/lab_profile.yaml"]
    refuse(reforge(ranged_release, drop=profile), "need a lab profile")


def test_a_lab_profile_that_is_not_yaml_is_refused(ranged_release):
    refuse(
        reforge(ranged_release, edit_files={"platform/03-range/lab_profile.yaml": "not: [valid"}),
        "lab_profile.yaml",
    )


def test_a_lab_profile_that_fails_the_lab_rules_is_refused(ranged_release):
    doc = yaml.safe_load(untar(ranged_release)["platform/03-range/lab_profile.yaml"])
    doc["module_ids"] = ["mod_002"]  # mod_001 is the range module here
    refuse(
        reforge(ranged_release, edit_files={"platform/03-range/lab_profile.yaml": yaml.safe_dump(doc)}),
        "lab.modules",
        "mod_001",
    )


def test_a_valid_lab_profile_is_kept_on_the_bundle(ranged_release):
    bundle = parse(ranged_release)
    assert bundle.lab_profile["id"] == "iot-gateway"
    assert bundle.activities["mod_001"] == "range"
    assert set(bundle.activities.values()) == {"range", "theory"}


# -- bundle conveniences -----------------------------------------------------


def test_open_actions_reads_release_json(release):
    def add_action(manifest):
        manifest["human_actions"].append({"id": "a1", "status": "open", "blocks_promotion": True})

    with_action = reforge(edit_manifest(release, add_action), meta_edit={"open_human_actions": [{"id": "a1"}]})
    assert parse(release).open_actions == []
    assert parse(with_action).open_actions == [{"id": "a1"}]


def test_part_tarball_is_deterministic_and_holds_exactly_that_part(release):
    bundle = parse(release)
    learner = bundle_mod.part_tarball(bundle, "learner")
    assert learner == bundle_mod.part_tarball(bundle, "learner")
    first = sorted(bundle.files["learner"])[0]
    assert set(untar(learner)) == set(bundle.files["learner"])
    assert untar(learner)[first] == bundle.files["learner"][first]
