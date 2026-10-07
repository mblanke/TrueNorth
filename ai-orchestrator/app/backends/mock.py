"""TrueNorth Range AI Orchestrator — Mock AI backend.

Returns deterministic fake responses for:
  - Unit testing without GPU/API access
  - CI pipelines and the integration stack, where no model is available
  - Offline ranges without internet access

Set AI_MODEL_BACKEND=mock (and AI_EMBED_BACKEND=mock) to activate.

Most prompts get an echo (``[MOCK] Response for: ...``). Prompts the
control plane parses get a fixed, well-formed fixture instead, so the
authoring flows can be exercised end to end without a model:

  - exercise-forge  -> a scenario YAML the forge router can persist
  - course-generate -> a course JSON draft Course Studio can persist

Every fixture carries the ``[MOCK]`` marker so it is never mistaken for
model output.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os

from .base import BaseAIBackend

logger = logging.getLogger("truenorth.ai.mock")

MOCK_MARKER = "[MOCK]"

# Phrases from the orchestrator's own prompt templates (main.py).
_FORGE_PROMPT = "cyber training exercise scenario as YAML"
_COURSE_PROMPT = "Draft a complete course grounded ONLY"

EXERCISE_FORGE_FIXTURE = f"""# {MOCK_MARKER} deterministic exercise-forge fixture
name: mock-forged-phishing-intrusion
version: "1.0"
description: "{MOCK_MARKER} Phishing foothold followed by PowerShell execution and C2."
range_template: small-enterprise
difficulty: intermediate
duration_minutes: 60
mitre_attack:
  - T1566.001
  - T1059.001
timeline:
  - t: "00:05"
    action: inject.email_phish
    params:
      technique: T1566.001
      description: Spear-phishing attachment delivered to a finance user
  - t: "00:15"
    action: inject.simulated_execution
    params:
      technique: T1059.001
      description: Encoded PowerShell launched from the attachment
objectives:
  - id: detect-phish
    name: Detect the phishing delivery
    type: detection
    validator: validate.opensearch_query
    params:
      query: "event.category:email AND email.attachments.file.extension:docm"
    points: 50
    competency_code: ""
    time_bonus: false
    time_limit_seconds: 0
  - id: contain-host
    name: Contain the compromised host
    type: containment
    validator: validate.manual_ack
    params: {{}}
    points: 50
    competency_code: ""
    time_bonus: false
    time_limit_seconds: 0
"""

COURSE_FIXTURE = json.dumps(
    {
        "name": f"{MOCK_MARKER} Mock course draft",
        "description": f"{MOCK_MARKER} Deterministic course draft for tests.",
        "difficulty": "intermediate",
        "duration_hours": 2,
        "nice_work_roles": [],
        "tags": ["mock"],
        "modules": [
            {
                "ordinal": 0,
                "title": "Introduction",
                "description": "Mock reading module.",
                "content_type": "reading",
                "duration_minutes": 30,
                "lesson_markdown": f"# Introduction\n\n{MOCK_MARKER} lesson text.",
                "learning_objectives": ["Describe the topic"],
                "competency_codes": [],
            },
            {
                "ordinal": 1,
                "title": "Check on learning",
                "description": "Mock quiz module.",
                "content_type": "quiz",
                "duration_minutes": 15,
                "lesson_markdown": "",
                "learning_objectives": ["Recall the topic"],
                "competency_codes": [],
            },
        ],
    }
)


def _fixture_for(prompt: str) -> str | None:
    if _FORGE_PROMPT in prompt:
        return EXERCISE_FORGE_FIXTURE
    if _COURSE_PROMPT in prompt:
        return COURSE_FIXTURE
    return None


class MockAIBackend(BaseAIBackend):
    """Deterministic mock backend — no external calls."""

    async def generate(
        self,
        prompt: str,
        model: str = "",
        max_tokens: int = 2000,
        system_prompt: str = "",
    ) -> tuple[str, str, dict]:
        effective_model = model or "mock"
        response = _fixture_for(prompt) or (
            f"{MOCK_MARKER} Response for: {prompt[:200]}{'...' if len(prompt) > 200 else ''}"
        )
        usage = {
            "prompt_tokens": len(prompt.split()),
            "completion_tokens": 10,
            "total_tokens": len(prompt.split()) + 10,
            "system_prompt_chars": len(system_prompt),
        }
        logger.debug("MockAIBackend.generate: model=%s tokens=%d", effective_model, usage["total_tokens"])
        return response, effective_model, usage

    async def embed(self, text: str, model: str = "") -> tuple[list[float], str]:
        """A unit-range vector derived from sha256(text): same text, same vector."""
        dim = int(os.getenv("MOCK_EMBED_DIM", "384"))
        seed = hashlib.sha256(text.encode()).digest()
        vec = [(seed[i % len(seed)] / 255.0) * 2 - 1 for i in range(dim)]
        return vec, model or "mock-embed"

    async def health_check(self) -> bool:
        return True
