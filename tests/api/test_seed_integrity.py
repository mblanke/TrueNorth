"""Guards on app.seed — the module has no other test coverage.

On 2026-08-19 seed.py was reduced to two functions with zero import statements:
every original seed function was deleted, and because `from __future__ import
annotations` went with them, `def seed_nations_and_coalitions(db: Session)`
raised NameError at *import* time. main.py imports these names inside a
try/except that logs "Seed failed (may already exist)", so the whole thing
failed silently and no test went red.

These tests make that failure mode loud.
"""

import inspect

import pytest
from app import seed
from app.models import Course, Nation

REQUIRED_SEED_FUNCTIONS = [
    "seed_nations_and_coalitions",
    "seed_auth_zones",
    "seed_infrastructure",
    "seed_ai_backends",
]


@pytest.mark.parametrize("name", REQUIRED_SEED_FUNCTIONS)
def test_required_seed_function_exists_and_is_callable(name):
    """main.py imports each of these by name at startup."""
    fn = getattr(seed, name, None)
    assert fn is not None, f"app.seed.{name} is missing — main.py imports it at startup"
    assert callable(fn), f"app.seed.{name} is not callable"
    # one positional parameter: the Session
    assert len(inspect.signature(fn).parameters) == 1


def test_main_imports_only_seed_functions_that_exist():
    """Catch a main.py that imports a seed function nobody ever wrote."""
    import ast
    import pathlib

    main_py = pathlib.Path(seed.__file__).with_name("main.py")
    tree = ast.parse(main_py.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "seed":
            imported.update(a.name for a in node.names)
    missing = sorted(n for n in imported if not hasattr(seed, n))
    assert not missing, f"main.py imports non-existent seed functions: {missing}"


def test_seed_nations_actually_populates(db_session):
    """A smoke run — proves the module's imports resolve, not just that it parses."""
    seed.seed_nations_and_coalitions(db_session)
    assert db_session.query(Nation).count() == 42


def test_seed_is_idempotent(db_session):
    seed.seed_nations_and_coalitions(db_session)
    first = db_session.query(Nation).count()
    seed.seed_nations_and_coalitions(db_session)
    assert db_session.query(Nation).count() == first


def test_seed_creates_no_courses(db_session):
    """seed.py seeds reference data only.

    Course content belongs to the QSP spine or the programme catalogue importer,
    both of which record provenance. Placeholder courses must not appear here.
    """
    for name in REQUIRED_SEED_FUNCTIONS:
        getattr(seed, name)(db_session)
    assert db_session.query(Course).count() == 0
