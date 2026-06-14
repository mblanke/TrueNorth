# AI Fleet Sizing & Deployment

How the local, air-gapped LLM fleet is sized and stood up for TrueNorth Range.

## What the AI is for (and what it is NOT)

The GPU-backed LLM fleet is an **author-time content engine**. It is used by a
small number of **content creators / instructors (≈5–10)** to generate and
refine material through the `ai-orchestrator`:

- Moodle-style quizzes — `POST /ai/quiz-generate`
- Courses and lessons — `POST /ai/course-generate`
- Virtual cyber exercises — `POST /ai/exercise-forge`
- After-action analysis, learning recommendations, detection rules, embeddings

It is **not** in the exercise-runtime path. The platform's 500–1,200 concurrent
**exercise participants do not touch the GPU fleet** — that load is served by
the API, database, OpenSearch, and the provisioned ranges. Consequently the AI
fleet is sized for a handful of concurrent generation jobs, not for the
platform's peak user count.

## Hardware

| Item | Spec |
|------|------|
| GPU nodes | `wile` (192.168.1.50), `roadrunner` (192.168.1.51) |
| GPU per node | 2× NVIDIA H200 (141 GB HBM3e each → **~282 GB VRAM/node**) |
| Inference server | Ollama (one container per node, all GPUs) |

> Note: earlier hardware notes listed "141 GB total VRAM" per node — that is the
> per-GPU figure. With 2× H200 the node has ~282 GB.

## Model strategy

| Role | Model | Notes |
|------|-------|-------|
| Flagship (all generation) | `qwen3.5:122b-a1mb-fp8` | ⚠️ confirm exact Ollama registry tag before pulling |
| Embeddings (curriculum RAG) | `bge-m3` | 1024-dim, matches `EMBED_DIM` in `curriculum_ingest.py` |

**VRAM math.** A ~122B model at FP8 (~1 byte/param) needs ~122 GB of weights
resident. On a ~282 GB node that leaves ~160 GB for KV cache/context and a
co-resident embedding model — comfortable headroom, even for large context
windows. The model is kept loaded (`OLLAMA_KEEP_ALIVE=-1`) so authors get
interactive turnaround instead of paying a multi-minute reload per request.

**Routing.** The orchestrator tags discovered models by name (`ai-orchestrator/
app/main.py`). `qwen3.5:122b…` is tagged `large` + `general`, so quiz/course/
exercise/AAR/scenario tasks route to it directly. Tasks whose tags it does *not*
carry (e.g. `detection-rule` → `code`) hit the **largest-healthy-model safety
net** in `_generate()`, which routes them to the flagship anyway rather than
degrading to the mock backend. Embeddings are excluded from that net — without
`bge-m3` they fall back to BM25 keyword retrieval (`embed_text` is best-effort).

## Concurrency settings

Sized for author-time use; tune in `.env` / compose:

| Variable | Value | Meaning |
|----------|-------|---------|
| `MAX_LLM_CONCURRENCY` | `4` | Global in-flight generation cap (≈2 nodes × 2) |
| `MAX_OLLAMA_PER_NODE` | `2` | Parallel requests per node (mirror `OLLAMA_NUM_PARALLEL`) |
| `OLLAMA_TIMEOUT_S` | `600` | Per-request timeout — a 122B generation can take minutes |
| `OLLAMA_MAX_MODEL_B` | `0` | No size cap; we *want* the big model routed |

## Connectivity: air-gapped

There is no cloud provider. Every `TASK_ROUTES` entry uses `fallback_backend =
mock`, and `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` stay blank. The mock backend
is only ever reached if the entire fleet is unreachable, and it returns a
deterministic placeholder — it never makes an outbound call.

## Standing it up

On each GPU node:

```bash
# 1. Verify GPU passthrough
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi

# 2. Start Ollama (binds all local GPUs)
docker compose -f infra/platform/docker/compose.ollama.yml up -d

# 3. Pull the models (confirm the flagship tag first!)
bash scripts/ollama-bootstrap.sh
# overrides: FLAGSHIP_MODEL=… EMBED_MODEL=… PULL_EMBED=0
```

On the control-plane host, set the fleet and bring up the stack:

```bash
# name=url pairs, comma-separated
OLLAMA_NODES=wile=http://192.168.1.50:11434,roadrunner=http://192.168.1.51:11434
AI_MODEL_BACKEND=ollama
```

Verify discovery: `curl http://<control-plane>:6000/fleet` should show both
nodes healthy with the flagship model tagged `large`/`general`.
