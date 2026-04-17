#!/usr/bin/env python3
"""TrueNorth Range — Scenario runner.

Parses scenario YAML, executes timeline via injector registry,
then validates objectives.

Usage:
    python -m scenario-engine.runner.run <scenario.yaml> [--range-id <id>] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

import yaml

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def parse_timecode(tc: str) -> int:
    """Parse MM:SS timecode to seconds."""
    m = re.match(r"^(\d+):(\d{2})$", tc)
    if not m:
        raise ValueError(f"Invalid timecode: {tc}")
    return int(m.group(1)) * 60 + int(m.group(2))


def validate_against_schema(data: dict, schema_path: str) -> bool:
    """Validate scenario data against JSON schema."""
    try:
        from jsonschema import validate as jvalidate

        schema = json.loads(Path(schema_path).read_text())
        jvalidate(data, schema)
        return True
    except Exception as e:
        logger.error(f"Schema validation failed: {e}")
        return False


def load_scenario(path: str) -> dict:
    """Load and validate a scenario YAML file."""
    data = yaml.safe_load(Path(path).read_text())
    schema_path = Path(__file__).parent.parent / "schemas" / "scenario.schema.json"
    if schema_path.exists():
        validate_against_schema(data, str(schema_path))
    return data


def build_range_context(range_id: str) -> dict:
    """Build a RangeContext (mock for standalone mode)."""
    return {
        "range_id": range_id,
        "tenant_id": "standalone",
        "vms": [
            {"name": "jump-box", "ip": "10.0.1.10", "role": "jump"},
            {"name": "dc-01", "ip": "10.0.2.10", "role": "dc"},
            {"name": "ws-01", "ip": "10.0.3.10", "role": "workstation"},
        ],
        "networks": [
            {"name": "mgmt", "cidr": "10.0.1.0/24"},
            {"name": "srv", "cidr": "10.0.2.0/24"},
            {"name": "user", "cidr": "10.0.3.0/24"},
        ],
    }


def execute_timeline(scenario: dict, range_ctx: dict, dry_run: bool = False) -> list[dict]:
    """Execute scenario timeline events sequentially."""
    # Try to import injector registry
    try:
        from importlib import import_module

        injectors_mod = import_module("scenario-engine.injectors")
        get_injector = injectors_mod.get_injector
        range_context_cls = injectors_mod.RangeContext
        ctx = range_context_cls(**range_ctx)
    except ImportError:
        get_injector = None
        ctx = None
        logger.warning("Injector registry not available — running in log-only mode")

    timeline = scenario.get("timeline", [])
    results = []
    start_time = time.time()

    for event in timeline:
        t = event.get("t", "0:00")
        action = event.get("action", "unknown")
        params = event.get("params", {})
        offset = parse_timecode(t)

        # Wait for timeline offset (compressed in dev)
        elapsed = time.time() - start_time
        wait = max(0, offset * 0.1 - elapsed)  # 10x compression
        if wait > 0 and not dry_run:
            logger.info(f"Waiting {wait:.1f}s until t={t} ...")
            time.sleep(wait)

        logger.info(f"[{t}] -> {action} (params: {params})")

        if dry_run:
            results.append({"t": t, "action": action, "status": "dry_run"})
            continue

        # Dispatch to injector
        if get_injector and ctx:
            injector = get_injector(action)
            if injector:
                result = injector.execute(params, ctx)
                results.append(
                    {"t": t, "action": action, "status": "ok" if result.success else "failed", "detail": result.detail}
                )
            else:
                logger.warning(f"No injector found for action: {action}")
                results.append({"t": t, "action": action, "status": "no_injector"})
        else:
            results.append({"t": t, "action": action, "status": "log_only"})

    return results


def evaluate_objectives(scenario: dict, range_ctx: dict) -> list[dict]:
    """Evaluate scenario objectives via validators."""
    try:
        from importlib import import_module

        validators_mod = import_module("scenario-engine.validators")
        get_validator = validators_mod.get_validator
    except ImportError:
        logger.warning("Validator registry not available")
        return []

    objectives = scenario.get("objectives", [])
    results = []

    for obj in objectives:
        obj_id = obj.get("id", "unknown")
        validator_name = obj.get("validator", "")
        params = obj.get("params", {})
        points = obj.get("points", 0)

        validator = get_validator(validator_name)
        if validator:
            vr = validator.check(params, range_ctx["range_id"], range_ctx["tenant_id"])
            results.append(
                {
                    "id": obj_id,
                    "validator": validator_name,
                    "passed": vr.passed,
                    "evidence": vr.evidence,
                    "points_earned": points if vr.passed else 0,
                }
            )
        else:
            logger.warning(f"No validator found for: {validator_name}")
            results.append(
                {"id": obj_id, "validator": validator_name, "passed": False, "evidence": "Validator not found"}
            )

    return results


def main():
    parser = argparse.ArgumentParser(description="TrueNorth Range Scenario Runner")
    parser.add_argument("scenario", help="Path to scenario YAML file")
    parser.add_argument("--range-id", default="standalone-001", help="Range ID")
    parser.add_argument("--dry-run", action="store_true", help="Log actions without executing")
    parser.add_argument("--output", help="Write results to JSON file")
    args = parser.parse_args()

    logger.info(f"Loading scenario: {args.scenario}")
    scenario = load_scenario(args.scenario)
    logger.info(f"Scenario: {scenario['name']} v{scenario['version']}")

    range_ctx = build_range_context(args.range_id)

    logger.info("=== Executing Timeline ===")
    timeline_results = execute_timeline(scenario, range_ctx, dry_run=args.dry_run)

    logger.info("=== Evaluating Objectives ===")
    objective_results = evaluate_objectives(scenario, range_ctx)

    # Summary
    total = sum(r.get("points_earned", 0) for r in objective_results)
    max_pts = sum(o.get("points", 0) for o in scenario.get("objectives", []))
    logger.info(f"=== Score: {total}/{max_pts} ===")

    results = {
        "scenario": scenario["name"],
        "range_id": args.range_id,
        "timeline": timeline_results,
        "objectives": objective_results,
        "score": {"earned": total, "max": max_pts},
    }

    if args.output:
        Path(args.output).write_text(json.dumps(results, indent=2))
        logger.info(f"Results written to {args.output}")
    else:
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
