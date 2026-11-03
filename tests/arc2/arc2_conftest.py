"""This directory's conftest.py, importable by a name that is its own.

`from conftest import X` finds whichever conftest.py pytest registered last, which depends
on which files the run was given: a whole-suite run happens to leave tests/arc2/conftest.py
there when the arc2 tests import, a run of a subset (the CI shards, scripts/ci_shard.py) can
leave tests/worker/conftest.py instead. This loads the file by path, so the answer does not
depend on the run. Fixtures still come from pytest's own load of conftest.py; only the
module-level data and helpers are read from here.
"""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("arc2_tests_conftest", Path(__file__).with_name("conftest.py"))
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

CROSSWALK = _module.CROSSWALK
QUESTIONS = _module.QUESTIONS
LAB_PROFILE = _module.LAB_PROFILE
course_yaml = _module.course_yaml
module_config = _module.module_config
write_run_content = _module.write_run_content
