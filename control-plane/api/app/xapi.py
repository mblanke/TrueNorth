"""TrueNorth Range — xAPI statement builder (cmi5 profile compatible)."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import httpx

LRS_URL = os.getenv("LRS_URL", "http://lrs:8000")
LRS_AUTH = os.getenv("LRS_AUTH", "")  # Basic auth token for LRS


# cmi5 Verb IRIs
VERBS = {
    "launched": "http://adlnet.gov/expapi/verbs/launched",
    "initialized": "http://adlnet.gov/expapi/verbs/initialized",
    "completed": "http://adlnet.gov/expapi/verbs/completed",
    "passed": "http://adlnet.gov/expapi/verbs/passed",
    "failed": "http://adlnet.gov/expapi/verbs/failed",
    "terminated": "http://adlnet.gov/expapi/verbs/terminated",
    "scored": "http://adlnet.gov/expapi/verbs/scored",
    "experienced": "http://adlnet.gov/expapi/verbs/experienced",
    "attempted": "http://adlnet.gov/expapi/verbs/attempted",
}


def _actor(user_email: str, user_name: str) -> dict:
    """Build xAPI Actor object."""
    return {
        "objectType": "Agent",
        "name": user_name,
        "mbox": f"mailto:{user_email}",
    }


def _verb(verb_key: str) -> dict:
    """Build xAPI Verb object."""
    return {
        "id": VERBS.get(verb_key, f"http://truenorthrange.local/verbs/{verb_key}"),
        "display": {"en-US": verb_key},
    }


def _activity(activity_type: str, activity_id: str, name: str, description: str = "") -> dict:
    """Build xAPI Activity (Object)."""
    return {
        "objectType": "Activity",
        "id": f"http://truenorthrange.local/{activity_type}/{activity_id}",
        "definition": {
            "type": f"http://truenorthrange.local/activity-types/{activity_type}",
            "name": {"en-US": name},
            "description": {"en-US": description or name},
        },
    }


def build_statement(
    verb_key: str,
    user_email: str,
    user_name: str,
    activity_type: str,
    activity_id: str,
    activity_name: str,
    result: dict | None = None,
    context_extensions: dict | None = None,
) -> dict:
    """Build a complete xAPI statement."""
    stmt = {
        "id": str(uuid.uuid4()),
        "actor": _actor(user_email, user_name),
        "verb": _verb(verb_key),
        "object": _activity(activity_type, activity_id, activity_name),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if result:
        stmt["result"] = result

    context = {
        "platform": "TrueNorth Range",
        "language": "en-US",
    }
    if context_extensions:
        context["extensions"] = {
            f"http://truenorthrange.local/extensions/{k}": v
            for k, v in context_extensions.items()
        }
    stmt["context"] = context

    return stmt


async def send_statement(statement: dict) -> bool:
    """Send an xAPI statement to the LRS."""
    try:
        headers = {"Content-Type": "application/json", "X-Experience-API-Version": "1.0.3"}
        if LRS_AUTH:
            headers["Authorization"] = f"Basic {LRS_AUTH}"
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{LRS_URL}/xapi/statements", json=statement, headers=headers)
            return resp.status_code in (200, 204)
    except Exception:
        return False


async def send_statements(statements: list[dict]) -> int:
    """Send multiple xAPI statements. Returns count of successful sends."""
    sent = 0
    for stmt in statements:
        if await send_statement(stmt):
            sent += 1
    return sent


# ── Convenience builders ───────────────────────────────────────────────

def exercise_launched(user_email: str, user_name: str, exercise_id: str, exercise_name: str, **kwargs) -> dict:
    return build_statement("launched", user_email, user_name, "exercise", exercise_id, exercise_name, context_extensions=kwargs)


def exercise_completed(user_email: str, user_name: str, exercise_id: str, exercise_name: str, score: int, max_score: int) -> dict:
    return build_statement(
        "completed", user_email, user_name, "exercise", exercise_id, exercise_name,
        result={"score": {"raw": score, "max": max_score, "scaled": score / max(max_score, 1)}, "completion": True, "success": score >= max_score * 0.7},
    )


def objective_achieved(user_email: str, user_name: str, objective_id: str, objective_name: str, points: int) -> dict:
    return build_statement(
        "passed", user_email, user_name, "objective", objective_id, objective_name,
        result={"score": {"raw": points}, "success": True},
    )


def scenario_started(user_email: str, user_name: str, scenario_id: str, scenario_name: str, range_id: str) -> dict:
    return build_statement(
        "initialized", user_email, user_name, "scenario", scenario_id, scenario_name,
        context_extensions={"range_id": range_id},
    )
