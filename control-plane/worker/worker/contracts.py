"""TrueNorth Range — API -> worker task contract (docs/adr/0002-interface-versioning.md).

SOURCE OF TRUTH for every Celery task the worker accepts: its name, the queue it runs
on, and its positional arguments. The API ships a byte-identical copy at
``control-plane/api/app/task_contracts.py`` (the two services build separate images,
so neither can import the other); ``scripts/export_task_contracts.py`` writes that
copy plus ``docs/interfaces/worker-tasks.schema.json``, and ``scripts/dod.sh`` fails
if either has drifted. Edit this file, then run the script.

Stdlib only: this module must import cleanly in both the API and worker images.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TASK_PREFIX = "worker.tasks."

_PY_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "array": (list, tuple),
    "object": dict,
}


class TaskContractError(ValueError):
    """A dispatch does not match the published task contract."""


@dataclass(frozen=True)
class Arg:
    name: str
    type: str = "string"  # JSON Schema type: string | array | object
    required: bool = True
    description: str = ""


@dataclass(frozen=True)
class TaskContract:
    name: str
    queue: str
    args: tuple[Arg, ...] = ()
    description: str = ""

    @property
    def qualified_name(self) -> str:
        return TASK_PREFIX + self.name


QUEUES = ("default", "provision", "destroy", "scenario", "telemetry")

_RANGE = Arg("range_id", description="ranges.id (UUID)")
_EXERCISE = Arg("exercise_id", description="exercises.id (UUID)")
_SNAPSHOT = Arg("snapshot_id", description="range_snapshots.id (UUID)")

TASKS: dict[str, TaskContract] = {
    c.name: c
    for c in (
        # -- Range lifecycle ------------------------------------------------
        TaskContract("provision_range", "provision", (_RANGE,), "Build a range's VMs on its hypervisor."),
        TaskContract("batch_provision", "provision", (Arg("range_ids", "array", description="ranges.id list"),)),
        TaskContract("destroy_range", "destroy", (_RANGE,), "Tear down a range's VMs."),
        TaskContract("stop_range", "provision", (_RANGE,), "Power a range's VMs off."),
        TaskContract("start_range", "provision", (_RANGE,), "Power a range's VMs on."),
        TaskContract("snapshot_range", "provision", (_RANGE, _SNAPSHOT)),
        TaskContract("restore_snapshot", "provision", (_RANGE, _SNAPSHOT)),
        TaskContract("delete_snapshot", "provision", (_RANGE, _SNAPSHOT)),
        # -- Exercises -------------------------------------------------------
        TaskContract("run_scenario", "scenario", (_EXERCISE,)),
        TaskContract(
            "run_scenario_v2",
            "scenario",
            (_EXERCISE, Arg("scenario_definition", "object", description="validated scenario YAML as JSON")),
        ),
        TaskContract("generate_aar", "default", (_EXERCISE,), "Write the after-action review."),
        TaskContract(
            "forge_exercise",
            "default",
            (
                Arg("request_id"),
                Arg("tenant_id"),
                Arg("indicators", "array"),
                Arg("config", "object"),
            ),
        ),
        # -- Learning --------------------------------------------------------
        TaskContract("auto_assess_competency", "default", (_EXERCISE, Arg("user_id"))),
        TaskContract(
            "generate_learning_recommendation",
            "default",
            (Arg("user_id"), Arg("target_role", required=False, description="NICE work role code")),
        ),
        # -- Telemetry -------------------------------------------------------
        TaskContract("ingest_telemetry_batch", "telemetry", (_RANGE, Arg("events", "array"))),
        # -- Periodic (beat) -------------------------------------------------
        TaskContract("cleanup_expired_ranges", "default"),
        TaskContract("health_check_ranges", "default"),
        TaskContract("collect_range_metrics", "telemetry"),
    )
}


def route_table() -> dict[str, dict[str, str]]:
    """Celery ``task_routes`` for every contracted task. Sender and worker share it."""
    return {c.qualified_name: {"queue": c.queue} for c in TASKS.values()}


def validate_args(name: str, args: tuple | list) -> TaskContract:
    """Check a dispatch against the contract; raise TaskContractError if it does not fit."""
    contract = TASKS.get(name)
    if contract is None:
        raise TaskContractError(f"no contract for task {name!r}; add it to worker/worker/contracts.py")
    required = sum(1 for a in contract.args if a.required)
    if not required <= len(args) <= len(contract.args):
        raise TaskContractError(
            f"{name} takes {required}..{len(contract.args)} args "
            f"({', '.join(a.name for a in contract.args)}), got {len(args)}"
        )
    for arg, value in zip(contract.args, args, strict=False):
        if not isinstance(value, _PY_TYPES[arg.type]):
            raise TaskContractError(f"{name}.{arg.name} must be {arg.type}, got {type(value).__name__}")
    return contract


def json_schema() -> dict[str, Any]:
    """The published contract (docs/interfaces/worker-tasks.schema.json)."""
    tasks = {}
    for c in sorted(TASKS.values(), key=lambda t: t.name):
        tasks[c.qualified_name] = {
            "description": c.description,
            "queue": c.queue,
            "args": {
                "type": "array",
                "prefixItems": [{"title": a.name, "type": a.type, "description": a.description} for a in c.args],
                "minItems": sum(1 for a in c.args if a.required),
                "maxItems": len(c.args),
            },
        }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "TrueNorth worker task contract",
        "description": "Celery tasks (JSON serializer, positional args) the worker accepts.",
        "queues": list(QUEUES),
        "tasks": tasks,
    }
