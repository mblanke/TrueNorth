"""The CI test shards together run exactly the suite `pytest tests/` would, once each."""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location("ci_shard", ROOT / "scripts" / "ci_shard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _workflow_shards() -> int:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    m = re.search(r"shard:\s*\[([0-9,\s]+)\]", text)
    assert m, "ci.yml test-python-shard has no `shard: [1, 2, ...]` matrix"
    total = len([s for s in m.group(1).split(",") if s.strip()])
    # The count is written in three more places; they must agree with the matrix.
    assert f"(${{{{ matrix.shard }}}}/{total})" in text
    assert f"ci_shard.py ${{{{ matrix.shard }}}} {total} " in text
    assert f"len(reports) != {total}" in text
    return total


def test_every_test_file_is_in_exactly_one_shard():
    ci_shard = _load()
    files = ci_shard.test_files()
    total = _workflow_shards()
    dealt = [f for i in range(1, total + 1) for f in ci_shard.shard(files, i, total)]
    assert sorted(dealt) == files
    assert len(dealt) == len(set(dealt))


def test_every_shard_has_work_and_integration_is_left_out():
    ci_shard = _load()
    files = ci_shard.test_files()
    total = _workflow_shards()
    assert len(files) > 100, "found suspiciously few test files"
    assert not [f for f in files if f.startswith("tests/integration/")]
    for i in range(1, total + 1):
        assert ci_shard.shard(files, i, total), f"shard {i}/{total} is empty"


def test_the_sharder_finds_every_file_pytest_would():
    """pytest.ini has no python_files, so the default patterns apply."""
    ci_shard = _load()
    on_disk = {
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "tests").rglob("*.py")
        if (p.name.startswith("test_") or p.name.endswith("_test.py"))
        and "integration" not in p.relative_to(ROOT / "tests").parts
    }
    assert set(ci_shard.test_files()) == on_disk


def test_no_test_imports_a_conftest_by_its_bare_name():
    """`from conftest import X` gets whichever conftest.py pytest loaded last, which depends
    on the files the run was given. A CI shard is a subset, so it can get another directory's."""
    offenders = [
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "tests").rglob("*.py")
        if re.search(r"^\s*(from conftest import|import conftest\b)", p.read_text(), re.M)
        and p.name != "test_ci_shard.py"
    ]
    assert not offenders, f"import the directory's helper module instead: {offenders}"
