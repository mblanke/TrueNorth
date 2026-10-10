#!/usr/bin/env python3
"""Fail when a pytest JUnit report shows no test actually passed.

    python scripts/junit_require_pass.py report.xml

pytest's own exit code is 0 when every test was skipped. For a required integration lane
that is the failure mode that matters: the old `integration` job was green for months with
every test skipped because nothing was listening. This reads the report pytest wrote and
exits 1 unless passed = tests - failures - errors - skipped is at least 1, and when
any test failed or errored.

Standard library only, so it runs on a bare CI runner.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET


def counts(path: str) -> dict[str, int]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    total = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in suites:
        for key in total:
            total[key] += int(suite.get(key, 0) or 0)
    total["passed"] = total["tests"] - total["failures"] - total["errors"] - total["skipped"]
    return total


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("report")
    args = ap.parse_args(argv)
    try:
        c = counts(args.report)
    except (OSError, ET.ParseError) as exc:
        print(f"junit_require_pass: cannot read {args.report}: {exc}", file=sys.stderr)
        return 1
    print(
        "junit_require_pass: {passed} passed, {failures} failed, {errors} errors, "
        "{skipped} skipped (of {tests})".format(**c)
    )
    if c["failures"] or c["errors"]:
        # The job also keeps pytest's exit code, but this script must not be the step that
        # reports a lane with failing tests as fine on its own (cmi5 review, 2026-10-10).
        print("junit_require_pass: failing or erroring tests in the report", file=sys.stderr)
        return 1
    if c["passed"] >= 1:
        return 0
    print("junit_require_pass: zero tests passed; a required lane that runs nothing is a failure", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
