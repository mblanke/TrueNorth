"""pyproject.toml's dependency list must match control-plane/api/requirements.txt.

The API image installs requirements.txt; `pip install -e .` uses pyproject.toml. They
drifted once already (the whole OpenTelemetry set was missing), and drift now also
means a security pin fixed in one file but not the other.
"""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_pyproject_dependencies_match_api_requirements():
    requirements = [
        line.split("#", 1)[0].strip()
        for line in (ROOT / "control-plane/api/requirements.txt").read_text().splitlines()
        if line.split("#", 1)[0].strip()
    ]
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    assert declared == requirements, (
        "pyproject.toml [project].dependencies has drifted from control-plane/api/requirements.txt: "
        f"only in pyproject {sorted(set(declared) - set(requirements))}, "
        f"only in requirements {sorted(set(requirements) - set(declared))}"
    )
