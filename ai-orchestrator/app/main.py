"""TrueNorth Range - AI Orchestrator API.

Multi-backend AI service with dynamic Ollama fleet support,
cloud fallback (OpenAI/Anthropic), tag-based task-to-model routing,
load balancing, health-aware failover, and caching.
Designed for 1,200+ concurrent users at 70k-VM scale.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# ── Logging ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("ai-orchestrator")


# ── Enums & Constants ──────────────────────────────────────────────────
class TaskType(str, Enum):
    detection_rule = "detection-rule"
    scenario_suggest = "scenario-suggest"
    aar_analysis = "aar-analysis"
    exercise_forge = "exercise-forge"
    learning_recommendation = "learning-recommendation"
    general = "general"
    embedding = "embedding"


class BackendType(str, Enum):
    ollama = "ollama"
    openai = "openai"
    anthropic = "anthropic"
    mock = "mock"


# ── Model Configuration ───────────────────────────────────────────────
@dataclass
class OllamaNode:
    """Represents a single Ollama server."""

    name: str
    base_url: str
    models: list[str] = field(default_factory=list)
    tagged_models: dict[str, set[str]] = field(default_factory=dict)
    healthy: bool = True
    last_check: float = 0.0
    avg_latency_ms: float = 0.0
    _inflight: int = 0

    def mark_healthy(self, latency_ms: float = 0.0) -> None:
        self.healthy = True
        self.last_check = time.time()
        if latency_ms > 0:
            self.avg_latency_ms = self.avg_latency_ms * 0.7 + latency_ms * 0.3

    def mark_unhealthy(self) -> None:
        self.healthy = False
        self.last_check = time.time()


@dataclass
class ModelRoute:
    """Maps a task to capability tags for model selection."""

    task: TaskType
    tags: list[str]
    fallback_backend: BackendType = BackendType.openai
    fallback_model: str = ""
    max_tokens_default: int = 2000


# ── Fleet config from env ──────────────────────────────────────────────
OLLAMA_PROXY_URL = os.getenv("OLLAMA_PROXY_URL", "https://ai.guapo613.beer")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
AUTH_DISABLED = os.getenv("AUTH_DISABLED", "false").lower() in ("1", "true", "yes")

# Primary backend preference: "ollama", "openai", "anthropic", "mock"
PRIMARY_BACKEND = BackendType(os.getenv("AI_MODEL_BACKEND", "ollama"))

# Concurrency
MAX_LLM_CONCURRENCY = int(os.getenv("MAX_LLM_CONCURRENCY", "20"))
MAX_OLLAMA_PER_NODE = int(os.getenv("MAX_OLLAMA_PER_NODE", "4"))
HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "30"))

# Cache
CACHE_TTL = int(os.getenv("AI_CACHE_TTL", "3600"))
CACHE_MAX = int(os.getenv("AI_CACHE_MAX", "2000"))

# ── Runtime state (set in lifespan) ────────────────────────────────────
_llm_semaphore: asyncio.Semaphore | None = None
_node_semaphores: dict[str, asyncio.Semaphore] = {}
_ollama_clients: dict[str, httpx.AsyncClient] = {}
_cache: dict[str, tuple[float, Any]] = {}
_health_task: asyncio.Task | None = None


# ── Dynamic fleet from OLLAMA_NODES env ────────────────────────────────
def _parse_fleet() -> dict[str, OllamaNode]:
    """Parse OLLAMA_NODES env var: ``name1=url1,name2=url2,...``

    Each node starts with an empty model list; the health-check loop
    will auto-discover models and assign capability tags.
    """
    raw = os.getenv("OLLAMA_NODES", "")
    fleet: dict[str, OllamaNode] = {}
    if not raw.strip():
        return fleet
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" not in pair:
            logger.warning("Skipping malformed OLLAMA_NODES entry: %s", pair)
            continue
        name, url = pair.split("=", 1)
        name, url = name.strip(), url.strip()
        if name and url:
            fleet[name] = OllamaNode(name=name, base_url=url)
            logger.info("Fleet node registered: %s -> %s", name, url)
    return fleet


FLEET: dict[str, OllamaNode] = _parse_fleet()


# ── Model tagging ──────────────────────────────────────────────────────
_SIZE_PATTERN = re.compile(r"(\d+)x?(\d+)?b", re.IGNORECASE)


def _model_size_hint(name: str) -> int:
    """Extract approximate parameter count (billions) from a model name.

    Examples: ``70b`` -> 70, ``8x22b`` -> 176, ``latest`` -> 0.
    """
    m = _SIZE_PATTERN.search(name)
    if not m:
        return 0
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else 0
    return a * b if b else a


def _tag_model(name: str) -> set[str]:
    """Assign capability tags to a model based on name patterns."""
    tags: set[str] = {"general"}
    lower = name.lower()

    # Size classification
    size = _model_size_hint(name)
    is_large = size >= 65 or "8x22b" in lower
    if is_large:
        tags.add("large")

    # Instruct capability
    if "instruct" in lower:
        tags.add("instruct")
        if is_large:
            tags.add("instruct-large")

    # Code capability
    if any(p in lower for p in ("coder", "codestral", "deepseek-coder", "codellama")):
        tags.add("code")

    # Embedding models
    if any(p in lower for p in ("embed", "bge")):
        tags.add("embedding")
        tags.discard("general")  # embedding models aren't general-purpose

    # Vision models
    if any(p in lower for p in ("vision", "llava", "minicpm")):
        tags.add("vision")

    return tags


def _find_best_for_tags(tags: list[str]) -> tuple[str, OllamaNode] | None:
    """Find the single best (model_name, node) for the given capability tags.

    Iterates *tags* in priority order.  For the first tag that has any
    matches on healthy nodes, pick the largest model (by parameter-count
    hint), breaking ties by lowest in-flight count.
    """
    for tag in tags:
        candidates: list[tuple[str, OllamaNode, int]] = []
        for node in FLEET.values():
            if not node.healthy:
                continue
            for model_name, model_tags in node.tagged_models.items():
                if tag in model_tags:
                    candidates.append((model_name, node, _model_size_hint(model_name)))
        if not candidates:
            continue
        candidates.sort(key=lambda c: (-c[2], c[1]._inflight))
        return (candidates[0][0], candidates[0][1])
    return None


def _find_all_for_tags(tags: list[str]) -> list[tuple[str, OllamaNode]]:
    """Return all (model_name, node) candidates for *tags*, ordered by
    tag priority -> model size (desc) -> node in-flight (asc).

    Used by ``_generate`` to try multiple candidate models before falling
    back to cloud.
    """
    seen: set[tuple[str, str]] = set()
    results: list[tuple[str, OllamaNode]] = []
    for tag in tags:
        candidates: list[tuple[str, OllamaNode, int]] = []
        for node in FLEET.values():
            if not node.healthy:
                continue
            for model_name, model_tags in node.tagged_models.items():
                if tag in model_tags and (model_name, node.name) not in seen:
                    candidates.append((model_name, node, _model_size_hint(model_name)))
                    seen.add((model_name, node.name))
        candidates.sort(key=lambda c: (-c[2], c[1]._inflight))
        results.extend((c[0], c[1]) for c in candidates)
    return results


# ── Task -> tag routing table ──────────────────────────────────────────
TASK_ROUTES: dict[TaskType, ModelRoute] = {
    TaskType.scenario_suggest: ModelRoute(
        task=TaskType.scenario_suggest,
        tags=["instruct-large", "large", "instruct"],
        fallback_backend=BackendType.openai,
        fallback_model="gpt-4o",
        max_tokens_default=3000,
    ),
    TaskType.aar_analysis: ModelRoute(
        task=TaskType.aar_analysis,
        tags=["instruct-large", "large", "instruct"],
        fallback_backend=BackendType.anthropic,
        fallback_model="claude-sonnet-4-20250514",
        max_tokens_default=4000,
    ),
    TaskType.detection_rule: ModelRoute(
        task=TaskType.detection_rule,
        tags=["code"],
        fallback_backend=BackendType.openai,
        fallback_model="gpt-4o",
        max_tokens_default=2000,
    ),
    TaskType.general: ModelRoute(
        task=TaskType.general,
        tags=["general"],
        fallback_backend=BackendType.openai,
        fallback_model="gpt-4o-mini",
        max_tokens_default=2000,
    ),
    TaskType.embedding: ModelRoute(
        task=TaskType.embedding,
        tags=["embedding"],
        fallback_backend=BackendType.mock,
        max_tokens_default=512,
    ),
    TaskType.exercise_forge: ModelRoute(
        task=TaskType.exercise_forge,
        tags=["instruct-large", "large", "instruct"],
        fallback_backend=BackendType.openai,
        fallback_model="gpt-4o",
        max_tokens_default=5000,
    ),
    TaskType.learning_recommendation: ModelRoute(
        task=TaskType.learning_recommendation,
        tags=["instruct-large", "large", "instruct"],
        fallback_backend=BackendType.openai,
        fallback_model="gpt-4o",
        max_tokens_default=3000,
    ),
}


# ── Lifespan ───────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _llm_semaphore, _health_task

    _llm_semaphore = asyncio.Semaphore(MAX_LLM_CONCURRENCY)

    # Create Ollama clients (long-lived, connection-pooled)
    for name, node in FLEET.items():
        _ollama_clients[name] = httpx.AsyncClient(
            base_url=node.base_url,
            timeout=httpx.Timeout(120, connect=5),
            limits=httpx.Limits(max_connections=MAX_OLLAMA_PER_NODE + 2, max_keepalive_connections=MAX_OLLAMA_PER_NODE),
        )
        _node_semaphores[name] = asyncio.Semaphore(MAX_OLLAMA_PER_NODE)

    # Background health monitor
    _health_task = asyncio.create_task(_health_check_loop())

    logger.info(
        "AI Orchestrator v0.4.0 started: primary=%s, fleet=%s, concurrency=%d, per_node=%d",
        PRIMARY_BACKEND.value,
        list(FLEET.keys()),
        MAX_LLM_CONCURRENCY,
        MAX_OLLAMA_PER_NODE,
    )
    yield

    # Shutdown
    _health_task.cancel()
    for client in _ollama_clients.values():
        await client.aclose()
    logger.info("AI Orchestrator shutdown")


# ── Health check loop ──────────────────────────────────────────────────
async def _health_check_loop():
    """Periodically check Ollama node health and refresh model tags."""
    while True:
        try:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            for name, node in list(FLEET.items()):
                try:
                    client = _ollama_clients.get(name)
                    if client is None:
                        continue
                    start = time.perf_counter()
                    resp = await client.get("/api/tags", timeout=5)
                    latency = (time.perf_counter() - start) * 1000
                    if resp.status_code == 200:
                        node.mark_healthy(latency)
                        # Update model list from live data and rebuild tags
                        data = resp.json()
                        live_models = [m["name"] for m in data.get("models", [])]
                        if live_models:
                            node.models = live_models
                            node.tagged_models = {m: _tag_model(m) for m in live_models}
                        logger.debug(
                            "Health OK: %s (%.0fms, %d models)",
                            name,
                            latency,
                            len(live_models),
                        )
                    else:
                        node.mark_unhealthy()
                        logger.warning("Health FAIL: %s (status %d)", name, resp.status_code)
                except Exception as e:
                    node.mark_unhealthy()
                    logger.warning("Health FAIL: %s (%s)", name, e)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Health loop error: %s", e)


# ── App ────────────────────────────────────────────────────────────────
app = FastAPI(title="TrueNorth Range AI Orchestrator", version="0.4.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Schemas ────────────────────────────────────────────────────────────
class GenerateRequest(BaseModel):
    task: str = Field(..., description="Task type: detection-rule, scenario-suggest, aar-analysis, general")
    prompt: str = Field(..., max_length=10000, description="Detailed prompt/instructions")
    context: dict = Field(default_factory=dict, description="Additional context data")
    model: str = Field(default="", description="Override model name (e.g. llama3.1:70b-instruct-q5_K_M)")
    node: str = Field(default="", description="Override node (wile/roadrunner)")
    max_tokens: int = Field(default=2000, ge=100, le=16000)
    skip_cache: bool = Field(default=False, description="Bypass cache for this request")


class GenerateResponse(BaseModel):
    task: str
    model_used: str
    node_used: str = ""
    backend: str = ""
    output: str
    usage: dict = {}
    cached: bool = False
    latency_ms: float = 0


class DetectionRuleRequest(BaseModel):
    technique: str = Field(..., pattern=r"^T\d{4}(\.\d{3})?$", description="MITRE ATT&CK technique ID")
    data_source: str = Field(default="sysmon", description="Data source (sysmon, zeek, suricata)")
    format: str = Field(default="sigma", description="Output format (sigma, opensearch, suricata)")
    model: str = Field(default="", description="Override model")


class ScenarioSuggestRequest(BaseModel):
    objectives: list[str] = Field(..., min_length=1, max_length=10, description="Training objectives")
    difficulty: str = Field(default="intermediate")
    duration_minutes: int = Field(default=60, ge=15, le=480)
    model: str = Field(default="", description="Override model")


class AARAnalysisRequest(BaseModel):
    report_data: str = Field(..., max_length=50000, description="AAR JSON or text data")
    context: dict = Field(default_factory=dict, description="Extra context")
    model: str = Field(default="", description="Override model")


class EmbeddingRequest(BaseModel):
    text: str = Field(..., max_length=8000)
    model: str = Field(default="bge-m3:latest")


class ExerciseForgeRequest(BaseModel):
    threat_indicators: list[dict] = Field(
        ...,
        min_length=1,
        max_length=20,
        description="List of threat indicators with type, value, severity, mitre_attack_ids",
    )
    difficulty: str = Field(default="intermediate", pattern=r"^(beginner|intermediate|advanced|expert)$")
    duration_minutes: int = Field(default=60, ge=15, le=480)
    objective_count: int = Field(default=4, ge=2, le=10)
    range_template: str = Field(default="small-enterprise")
    focus_areas: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="Focus: detection, containment, eradication, recovery, analysis",
    )
    model: str = Field(default="", description="Override model")


class LearningRecommendationRequest(BaseModel):
    user_profile: dict = Field(..., description="User competency profile with assertions and gaps")
    exercise_history: list[dict] = Field(default_factory=list, description="Recent exercise results")
    available_courses: list[dict] = Field(default_factory=list, description="Available course catalog")
    target_role: str = Field(default="", description="Target NICE work role code")
    model: str = Field(default="", description="Override model")


class EmbeddingResponse(BaseModel):
    embedding: list[float]
    model_used: str
    node_used: str = ""
    latency_ms: float = 0


class AddNodeRequest(BaseModel):
    """Request body to register a new Ollama node at runtime."""

    name: str = Field(..., description="Unique node name (e.g. gpu3)")
    url: str = Field(..., description="Ollama base URL (e.g. http://10.0.0.12:11434)")


class FleetStatus(BaseModel):
    nodes: dict[str, dict] = {}
    primary_backend: str = ""
    cache_size: int = 0
    max_concurrency: int = 0


# ── Cache helpers ──────────────────────────────────────────────────────
def _cache_key(prompt: str, model: str) -> str:
    return hashlib.sha256(f"{model}:{prompt}".encode()).hexdigest()


def _cache_get(key: str) -> tuple[str, str, str, dict] | None:
    if key in _cache:
        ts, val = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return val
        del _cache[key]
    return None


def _cache_set(key: str, val: tuple[str, str, str, dict]) -> None:
    if len(_cache) >= CACHE_MAX:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        del _cache[oldest]
    _cache[key] = (time.time(), val)


# ── Retry helper ───────────────────────────────────────────────────────
async def _call_with_retry(coro_factory, retries: int = 3, base_delay: float = 1.0):
    """Call an async function with exponential backoff on 429/5xx."""
    for attempt in range(retries + 1):
        try:
            return await coro_factory()
        except httpx.HTTPStatusError as e:
            if (e.response.status_code in (429,) or e.response.status_code >= 500) and attempt < retries:
                delay = base_delay * (2**attempt) + random.uniform(0, 0.5)
                logger.warning(
                    "LLM call failed (%s), retrying in %.1fs (attempt %d/%d)",
                    e.response.status_code,
                    delay,
                    attempt + 1,
                    retries,
                )
                await asyncio.sleep(delay)
                continue
            raise
        except (httpx.ConnectError, httpx.ReadTimeout) as e:
            if attempt < retries:
                delay = base_delay * (2**attempt) + random.uniform(0, 0.5)
                logger.warning("Connection error (%s), retrying in %.1fs", type(e).__name__, delay)
                await asyncio.sleep(delay)
                continue
            raise


# ── Ollama backend ─────────────────────────────────────────────────────
def _find_node_for_model(model: str, preferred_nodes: list[str] | None = None) -> tuple[str, OllamaNode] | None:
    """Find the best healthy node that hosts the given model.

    Selection priority:
    1. Preferred nodes (in order) that are healthy and have the model
    2. Any healthy node with the model
    3. None if no healthy node has it
    """
    candidates = []
    order = preferred_nodes or list(FLEET.keys())

    for name in order:
        node = FLEET.get(name)
        if node and node.healthy and model in node.models:
            candidates.append((name, node))

    # Also check non-preferred nodes
    for name, node in FLEET.items():
        if name not in order and node.healthy and model in node.models:
            candidates.append((name, node))

    if not candidates:
        return None

    # Pick the node with lowest inflight (simple load balancing)
    candidates.sort(key=lambda x: x[1]._inflight)
    return candidates[0]


async def _call_ollama(
    prompt: str,
    model: str,
    node_name: str = "",
    preferred_nodes: list[str] | None = None,
    max_tokens: int = 2000,
    system_prompt: str = "",
) -> tuple[str, str, str, dict]:
    """Call Ollama API. Returns (text, model, node_name, usage)."""

    # Resolve node
    if node_name and node_name in FLEET:
        target = (node_name, FLEET[node_name])
        if not target[1].healthy:
            logger.warning("Requested node %s is unhealthy, trying alternatives", node_name)
            target = _find_node_for_model(model, preferred_nodes)
    else:
        target = _find_node_for_model(model, preferred_nodes)

    if not target:
        raise HTTPException(503, f"No healthy node available for model {model}")

    name, node = target
    client = _ollama_clients[name]

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    async def _do():
        node._inflight += 1
        try:
            start = time.perf_counter()
            # Use OpenAI-compatible endpoint
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": 0.7,
                    "stream": False,
                },
            )
            resp.raise_for_status()
            latency = (time.perf_counter() - start) * 1000
            node.mark_healthy(latency)
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            usage["latency_ms"] = round(latency, 1)
            usage["node"] = name
            return text, model, name, usage
        finally:
            node._inflight -= 1

    # Per-node concurrency control
    async with _node_semaphores[name]:
        return await _call_with_retry(_do, retries=2, base_delay=2.0)


async def _call_ollama_embedding(text: str, model: str = "bge-m3:latest") -> tuple[list[float], str, str]:
    """Get embeddings from Ollama."""
    target = _find_node_for_model(model)
    if not target:
        raise HTTPException(503, f"No healthy node for embedding model {model}")

    name, node = target
    client = _ollama_clients[name]

    async def _do():
        resp = await client.post("/api/embeddings", json={"model": model, "prompt": text})
        resp.raise_for_status()
        data = resp.json()
        return data["embedding"], model, name

    async with _node_semaphores[name]:
        return await _call_with_retry(_do, retries=2)


# ── Cloud backends ─────────────────────────────────────────────────────
async def _call_openai(prompt: str, model: str = "", max_tokens: int = 2000) -> tuple[str, str, str, dict]:
    from .backends import get_cloud_backend  # lazy: avoids top-level package collision in tests
    text, model_used, usage = await get_cloud_backend("openai").generate(prompt, model=model, max_tokens=max_tokens)
    return text, model_used, "openai", usage


async def _call_anthropic(prompt: str, model: str = "", max_tokens: int = 2000) -> tuple[str, str, str, dict]:
    from .backends import get_cloud_backend  # lazy: avoids top-level package collision in tests
    text, model_used, usage = await get_cloud_backend("anthropic").generate(prompt, model=model, max_tokens=max_tokens)
    return text, model_used, "anthropic", usage


# ── Unified router ─────────────────────────────────────────────────────
async def _generate(
    prompt: str,
    task: TaskType = TaskType.general,
    model_override: str = "",
    node_override: str = "",
    max_tokens: int = 0,
    use_cache: bool = True,
    system_prompt: str = "",
) -> tuple[str, str, str, dict, bool]:
    """Route to the best backend based on task, fleet health, and config.

    Returns: (text, model_used, node_or_backend, usage, cached)
    """
    route = TASK_ROUTES.get(task, TASK_ROUTES[TaskType.general])
    effective_max = max_tokens or route.max_tokens_default
    effective_model = model_override

    # Cache check
    if model_override:
        cache_model = model_override
    else:
        best = _find_best_for_tags(route.tags)
        cache_model = best[0] if best else PRIMARY_BACKEND.value
    key = _cache_key(prompt, cache_model)
    if use_cache:
        cached = _cache_get(key)
        if cached:
            logger.debug("Cache hit for key=%s", key[:12])
            return (*cached, True)

    async with _llm_semaphore:
        # Strategy: try Ollama fleet first if primary is ollama
        if BackendType.ollama == PRIMARY_BACKEND:
            # Build candidate list: explicit override or tag-based discovery
            if model_override:
                models_to_try: list[tuple[str, str]] = [(model_override, node_override)]
            else:
                candidates = _find_all_for_tags(route.tags)
                models_to_try = [(m, n.name) for m, n in candidates]

            last_err = None
            for model, node_hint in models_to_try:
                try:
                    result = await _call_ollama(
                        prompt,
                        model,
                        node_name=node_hint,
                        max_tokens=effective_max,
                        system_prompt=system_prompt,
                    )
                    if use_cache:
                        _cache_set(key, result)
                    return (*result, False)
                except Exception as e:
                    last_err = e
                    logger.warning("Ollama call failed for %s: %s, trying next", model, e)
                    continue

            # All Ollama models failed -- fall back to cloud
            logger.warning(
                "All Ollama models failed for task=%s, falling back to %s",
                task.value,
                route.fallback_backend.value,
            )
            try:
                result = await _cloud_fallback(prompt, route, effective_max)
                if use_cache:
                    _cache_set(key, result)
                return (*result, False)
            except Exception as cloud_err:
                logger.error("Cloud fallback also failed: %s (original: %s)", cloud_err, last_err)
                raise HTTPException(503, f"All backends failed. Ollama: {last_err}, Cloud: {cloud_err}") from cloud_err

        elif BackendType.openai == PRIMARY_BACKEND and OPENAI_API_KEY:
            result = await _call_openai(prompt, effective_model or route.fallback_model, effective_max)
        elif BackendType.anthropic == PRIMARY_BACKEND and ANTHROPIC_API_KEY:
            result = await _call_anthropic(prompt, effective_model or route.fallback_model, effective_max)
        else:
            from .backends import get_cloud_backend  # lazy
            text, model_used, usage = await get_cloud_backend("mock").generate(prompt, max_tokens=effective_max)
            result = (text, model_used, "mock", usage)

    if use_cache:
        _cache_set(key, result)
    return (*result, False)


async def _cloud_fallback(prompt: str, route: ModelRoute, max_tokens: int) -> tuple[str, str, str, dict]:
    """Fall back to cloud provider."""
    if route.fallback_backend == BackendType.openai and OPENAI_API_KEY:
        return await _call_openai(prompt, route.fallback_model, max_tokens)
    elif route.fallback_backend == BackendType.anthropic and ANTHROPIC_API_KEY:
        return await _call_anthropic(prompt, route.fallback_model, max_tokens)
    else:
        from .backends import get_cloud_backend  # lazy
        text, model_used, usage = await get_cloud_backend("mock").generate(prompt, max_tokens=max_tokens)
        return text, model_used, "mock", usage


# ── Middleware ──────────────────────────────────────────────────────────
@app.middleware("http")
async def timing_middleware(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = (time.perf_counter() - start) * 1000
    response.headers["X-Response-Time-Ms"] = f"{elapsed:.1f}"
    return response


# ── Routes ─────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "0.4.0",
        "primary_backend": PRIMARY_BACKEND.value,
        "cache_size": len(_cache),
        "max_concurrency": MAX_LLM_CONCURRENCY,
        "fleet": {
            name: {
                "healthy": node.healthy,
                "url": node.base_url,
                "models_count": len(node.models),
                "avg_latency_ms": round(node.avg_latency_ms, 1),
                "inflight": node._inflight,
                "last_check": round(time.time() - node.last_check, 0) if node.last_check else None,
            }
            for name, node in FLEET.items()
        },
    }


@app.get("/fleet", response_model=FleetStatus)
async def fleet_status():
    """Detailed fleet status with model lists and capability tags."""
    return FleetStatus(
        nodes={
            name: {
                "healthy": node.healthy,
                "url": node.base_url,
                "models": node.models,
                "tagged_models": {m: sorted(t) for m, t in node.tagged_models.items()},
                "avg_latency_ms": round(node.avg_latency_ms, 1),
                "inflight": node._inflight,
            }
            for name, node in FLEET.items()
        },
        primary_backend=PRIMARY_BACKEND.value,
        cache_size=len(_cache),
        max_concurrency=MAX_LLM_CONCURRENCY,
    )


@app.get("/fleet/{node_name}/models")
async def node_models(node_name: str):
    """List models available on a specific node with their tags."""
    if node_name not in FLEET:
        raise HTTPException(404, f"Unknown node: {node_name}")
    node = FLEET[node_name]
    return {
        "node": node_name,
        "healthy": node.healthy,
        "models": node.models,
        "tagged_models": {m: sorted(t) for m, t in node.tagged_models.items()},
    }


@app.post("/fleet/nodes")
async def add_fleet_node(req: AddNodeRequest):
    """Register a new Ollama node at runtime (useful for dynamic scaling)."""
    if req.name in FLEET:
        raise HTTPException(409, f"Node '{req.name}' already exists")
    node = OllamaNode(name=req.name, base_url=req.url)
    FLEET[req.name] = node
    _ollama_clients[req.name] = httpx.AsyncClient(
        base_url=req.url,
        timeout=httpx.Timeout(120, connect=5),
        limits=httpx.Limits(
            max_connections=MAX_OLLAMA_PER_NODE + 2,
            max_keepalive_connections=MAX_OLLAMA_PER_NODE,
        ),
    )
    _node_semaphores[req.name] = asyncio.Semaphore(MAX_OLLAMA_PER_NODE)
    logger.info("Runtime node added: %s -> %s", req.name, req.url)
    return {"status": "added", "name": req.name, "url": req.url}


@app.post("/fleet/{node_name}/pull")
@app.post("/fleet/{node_name}/pull")
async def pull_model(node_name: str, model: str):
    """Trigger a model pull on a specific node."""
    if node_name not in FLEET:
        raise HTTPException(404, f"Unknown node: {node_name}")
    client = _ollama_clients[node_name]
    try:
        resp = await client.post("/api/pull", json={"name": model, "stream": False}, timeout=600)
        resp.raise_for_status()
        return {"status": "pulled", "node": node_name, "model": model}
    except Exception as e:
        raise HTTPException(502, f"Pull failed on {node_name}: {e}") from e


@app.post("/ai/generate", response_model=GenerateResponse)
async def generate(req: GenerateRequest):
    start = time.perf_counter()
    task = TaskType(req.task) if req.task in TaskType.__members__.values() else TaskType.general
    full_prompt = f"Task: {req.task}\n\n{req.prompt}"
    if req.context:
        full_prompt += f"\n\nContext:\n{json.dumps(req.context, indent=2)}"

    output, model_used, node_used, usage, cached = await _generate(
        full_prompt,
        task,
        req.model,
        req.node,
        req.max_tokens,
        use_cache=not req.skip_cache,
    )
    latency = (time.perf_counter() - start) * 1000
    logger.info(
        "generate task=%s model=%s node=%s cached=%s latency=%.0fms",
        req.task,
        model_used,
        node_used,
        cached,
        latency,
    )
    return GenerateResponse(
        task=req.task,
        model_used=model_used,
        node_used=node_used,
        backend=PRIMARY_BACKEND.value,
        output=output,
        usage=usage,
        cached=cached,
        latency_ms=round(latency, 1),
    )


@app.post("/ai/detection-rule", response_model=GenerateResponse)
async def generate_detection_rule(req: DetectionRuleRequest):
    start = time.perf_counter()
    prompt = f"""Generate a {req.format} detection rule for MITRE ATT&CK technique {req.technique}.
Data source: {req.data_source}.
Requirements:
- Include a descriptive title and description
- Reference the MITRE technique ID
- Use realistic field names for the data source
- Include common false positive notes
Output only the detection rule in {req.format} format."""

    output, model_used, node_used, usage, cached = await _generate(
        prompt,
        TaskType.detection_rule,
        req.model,
    )
    latency = (time.perf_counter() - start) * 1000
    return GenerateResponse(
        task="detection-rule",
        model_used=model_used,
        node_used=node_used,
        backend=PRIMARY_BACKEND.value,
        output=output,
        usage=usage,
        cached=cached,
        latency_ms=round(latency, 1),
    )


@app.post("/ai/scenario-suggest", response_model=GenerateResponse)
async def suggest_scenario(req: ScenarioSuggestRequest):
    start = time.perf_counter()
    prompt = f"""Design a cyber training scenario as YAML for TrueNorth Range platform.
Training objectives: {", ".join(req.objectives)}
Difficulty: {req.difficulty}
Duration: {req.duration_minutes} minutes

The YAML should include:
- name, version, range_template
- timeline with timed actions (t: "H:MM") using these injector types:
  email_phish, dns_spike, http_burst, simulated_execution, identity_new_admin_user
- objectives with validators: opensearch_query, manual_ack, deliverable_check
- Each objective has points adding up to 100

Output valid YAML only."""

    output, model_used, node_used, usage, cached = await _generate(
        prompt,
        TaskType.scenario_suggest,
        req.model,
        max_tokens=3000,
    )
    latency = (time.perf_counter() - start) * 1000
    return GenerateResponse(
        task="scenario-suggest",
        model_used=model_used,
        node_used=node_used,
        backend=PRIMARY_BACKEND.value,
        output=output,
        usage=usage,
        cached=cached,
        latency_ms=round(latency, 1),
    )


@app.post("/ai/aar-analysis", response_model=GenerateResponse)
async def analyze_aar(req: AARAnalysisRequest):
    start = time.perf_counter()
    prompt = f"""Analyze this cyber exercise After-Action Report data and provide:
1. Executive summary of performance
2. Key findings (what went well, what needs improvement)
3. Specific recommendations for each missed objective
4. Suggested follow-up training topics
5. Overall readiness assessment (1-10 scale)

Data:
{req.report_data}

{json.dumps(req.context, indent=2) if req.context else ""}"""

    output, model_used, node_used, usage, cached = await _generate(
        prompt,
        TaskType.aar_analysis,
        req.model,
        use_cache=False,
        max_tokens=4000,
    )
    latency = (time.perf_counter() - start) * 1000
    return GenerateResponse(
        task="aar-analysis",
        model_used=model_used,
        node_used=node_used,
        backend=PRIMARY_BACKEND.value,
        output=output,
        usage=usage,
        cached=cached,
        latency_ms=round(latency, 1),
    )


@app.post("/ai/embedding", response_model=EmbeddingResponse)
async def get_embedding(req: EmbeddingRequest):
    """Generate text embeddings using local models (bge-m3 / nomic-embed-text)."""
    start = time.perf_counter()
    try:
        embedding, model_used, node_used = await _call_ollama_embedding(req.text, req.model)
    except Exception as e:
        raise HTTPException(503, f"Embedding failed: {e}") from e
    latency = (time.perf_counter() - start) * 1000
    return EmbeddingResponse(
        embedding=embedding,
        model_used=model_used,
        node_used=node_used,
        latency_ms=round(latency, 1),
    )


@app.post("/ai/exercise-forge", response_model=GenerateResponse)
async def forge_exercise(req: ExerciseForgeRequest):
    """Generate a complete exercise scenario from threat intelligence indicators."""
    start = time.perf_counter()

    # Build indicator summary for the prompt
    indicator_lines = []
    mitre_ids = set()
    for ind in req.threat_indicators:
        line = f"- {ind.get('indicator_type', 'unknown')}: {ind.get('value', 'N/A')} (severity: {ind.get('severity', 'medium')})"
        if ind.get("description"):
            line += f" — {ind['description']}"
        indicator_lines.append(line)
        for mid in ind.get("mitre_attack_ids") or []:
            mitre_ids.add(mid)

    focus = ", ".join(req.focus_areas) if req.focus_areas else "detection, containment, analysis"

    prompt = f"""Generate a complete cyber training exercise scenario as YAML for the TrueNorth Range platform.

## Threat Intelligence Context
The following indicators of compromise (IOCs) were observed from real threat feeds:
{chr(10).join(indicator_lines)}

MITRE ATT&CK techniques involved: {", ".join(sorted(mitre_ids)) if mitre_ids else "determine from indicators"}

## Exercise Requirements
- Difficulty: {req.difficulty}
- Duration: {req.duration_minutes} minutes
- Number of objectives: {req.objective_count}
- Range template: {req.range_template}
- Focus areas: {focus}

## Output Format (YAML)
Generate valid YAML with this structure:
```yaml
name: <descriptive-kebab-case-name>
version: "1.0"
description: <2-3 sentence description linking to threat intel>
range_template: {req.range_template}
difficulty: {req.difficulty}
duration_minutes: {req.duration_minutes}
mitre_attack:
  - <technique IDs>

timeline:
  - t: "HH:MM"
    action: "inject.<type>"  # email_phish, dns_spike, http_burst, simulated_execution, identity_new_admin_user
    params:
      technique: "<ATT&CK ID>"
      description: "<what this inject simulates>"
      <inject-specific params>

objectives:
  - id: <kebab-case-id>
    name: "<objective name>"
    type: <detection|containment|eradication|recovery|analysis>
    validator: "validate.<method>"  # opensearch_query, manual_ack, deliverable_check
    params:
      <validator-specific config>
    points: <integer>
    time_bonus: <true|false>
    time_limit_seconds: <seconds or 0>
```

Rules:
- Timeline events must be chronologically ordered
- Total objective points must sum to 100
- Include at least one detection and one response objective
- Reference the actual IOC values from the threat intel in inject params
- Use realistic OpenSearch queries for detection validators
- Output ONLY valid YAML, no markdown fences or explanation"""

    output, model_used, node_used, usage, cached = await _generate(
        prompt,
        TaskType.exercise_forge,
        req.model,
        use_cache=False,
        max_tokens=5000,
    )
    latency = (time.perf_counter() - start) * 1000
    return GenerateResponse(
        task="exercise-forge",
        model_used=model_used,
        node_used=node_used,
        backend=PRIMARY_BACKEND.value,
        output=output,
        usage=usage,
        cached=cached,
        latency_ms=round(latency, 1),
    )


@app.post("/ai/learning-recommendation", response_model=GenerateResponse)
async def recommend_learning(req: LearningRecommendationRequest):
    """Generate personalized learning recommendations from competency profile."""
    start = time.perf_counter()

    prompt = f"""Analyze this cybersecurity trainee's competency profile and generate personalized learning recommendations.

## Current Competency Profile
{json.dumps(req.user_profile, indent=2)}

## Recent Exercise Performance
{json.dumps(req.exercise_history, indent=2) if req.exercise_history else "No recent exercises."}

## Available Courses
{json.dumps(req.available_courses, indent=2) if req.available_courses else "Use general recommendations."}

{f"## Target Work Role: {req.target_role}" if req.target_role else ""}

## Output Format (JSON)
Return a JSON object with:
```json
{{
  "summary": "Brief assessment of current skill level",
  "strengths": ["area1", "area2"],
  "gaps": ["gap1", "gap2"],
  "recommendations": [
    {{
      "priority": 1,
      "type": "course|exercise|certification|self-study",
      "title": "Recommended item title",
      "rationale": "Why this is recommended",
      "competencies_addressed": ["comp1", "comp2"],
      "estimated_hours": 4
    }}
  ],
  "target_role_readiness": 0.0-1.0,
  "next_milestone": "Description of next achievable milestone"
}}
```
Output ONLY valid JSON, no markdown fences."""

    output, model_used, node_used, usage, cached = await _generate(
        prompt,
        TaskType.learning_recommendation,
        req.model,
        use_cache=False,
        max_tokens=3000,
    )
    latency = (time.perf_counter() - start) * 1000
    return GenerateResponse(
        task="learning-recommendation",
        model_used=model_used,
        node_used=node_used,
        backend=PRIMARY_BACKEND.value,
        output=output,
        usage=usage,
        cached=cached,
        latency_ms=round(latency, 1),
    )
