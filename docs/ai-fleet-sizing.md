# AI Engine: Architecture, Sizing & Integration

How TrueNorth's AI features are powered by the local, air-gapped GPU engine.

## What the AI is for (and what it is NOT)

The GPU engine is an **author-time content engine** used by a small number of
content creators / instructors (~5–10) to generate and refine material via the
`ai-orchestrator`: quizzes (`/ai/quiz-generate`), courses (`/ai/course-generate`),
virtual cyber exercises (`/ai/exercise-forge`), AAR analysis, learning
recommendations, detection rules, and embeddings.

It is **not** in the exercise-runtime path. The 500–1,200 concurrent exercise
participants are served by the API, DB, OpenSearch, and provisioned ranges — they
never touch the GPU. So the engine is sized for a handful of concurrent
generation jobs, not the platform's peak user count.

## The machine — Dell R7725 AI node (`eqt6r2d-u14`, 133.1.14.240)

| | |
|---|---|
| CPU | 2× AMD EPYC 9535 — 256 threads |
| RAM | 2.0 TiB |
| Disk | 14 TB NVMe `/data` (13.6 TB free) |
| GPU | **2× NVIDIA H200 NVL — 144 GB each (~288 GB total)**, driver 580 / CUDA 13 |

## Engine: vLLM behind LiteLLM (not Ollama)

The local stack lives at `/data/ai-stack/` (vLLM 0.19.1 venv):

- **vLLM** serves one model per GPU; **LiteLLM** (`:4000`, OpenAI-compatible,
  master key `sk-r7725-local`) is the single front door with named aliases.
- **pgvector** (in the `litellm-postgres` container) backs the stack's own RAG.
- Launch scripts: `/data/ai-stack/scripts/start-vllm-gpu*.sh`, `start-litellm.sh`.

### Current GPU layout (today)
| GPU | Model | Port | LiteLLM alias |
|-----|-------|------|---------------|
| 0 | Mistral 7B (bf16) | 8003 | `fast` |
| 1 | Mistral Small 3 24B (bf16, multimodal) | 8004 | `agent` |
| — | bge embedding server (idle) | 8005 | `embed` |

### Target GPU layout (after Qwen 112B)
| GPU | Model | Port | LiteLLM alias |
|-----|-------|------|---------------|
| 0 | **Qwen 112B-A10B FP8** (replaces Mistral 7B) | 8003 | `forge`, `fast` |
| 1 | Mistral Small 3 24B (multimodal, fallback) | 8004 | `agent` |
| — | bge-m3 embedding server | 8005 | `embed` |

**VRAM math.** Qwen is a 112B-total / 10B-active **MoE** in FP8: ~112 GB of weights
must be resident (all experts), but only ~10B params compute per token, so it's
fast. ~112 GB fits on a single 144 GB H200 with ~30 GB left for KV cache — no
tensor-parallel needed. Mistral 24B stays on GPU1. Staged artifacts:
`/data/ai-stack/scripts/start-vllm-qwen.sh` and `/data/ai-stack/litellm/config.qwen.yaml`.

## How TrueNorth integrates

TrueNorth's `ai-orchestrator` uses its **OpenAI-compatible backend** pointed at
LiteLLM — one integration point, no Ollama:

```
AI_MODEL_BACKEND=openai
OPENAI_BASE_URL=http://133.1.14.240:4000/v1   # or host.docker.internal:4000 in compose
OPENAI_API_KEY=sk-r7725-local
```

Per-task model selection maps to LiteLLM aliases via env (no code change to swap
models):

| Env var | Default (today) | After Qwen | Drives |
|---------|-----------------|-----------|--------|
| `AI_HEAVY_MODEL` | `agent` | `forge` | quiz, course, exercise, AAR, scenario, learning |
| `AI_GENERAL_MODEL` | `agent` | `forge`/`agent` | general generation |
| `AI_CODE_MODEL` | `agent` | `forge` | detection rules |
| `AI_EMBED_MODEL` | `embed` | `embed` | `/ai/embedding` |

`OPENAI_TIMEOUT_S=600` covers long (6000-token) course drafts.

### Embeddings / RAG
`/ai/embedding` routes through LiteLLM `embed` when `AI_EMBED_BACKEND=openai`
(default). TrueNorth's curriculum index dimension is `EMBED_DIM` (default 1024 for
bge-m3); **set it to match the served embed model** (bge-small-en-v1.5 = 384).
Mismatched vectors are dropped and the chunk is indexed text-only (BM25 still
works), so embeddings are best-effort but recommended for semantic retrieval.

## Connectivity: air-gapped

"Air-gapped" here means **no external LLM provider** — all inference is local
(LiteLLM/vLLM on this box). `OPENAI_BASE_URL` points at `133.1.14.240:4000`, the
LAN LiteLLM, not the internet; `ANTHROPIC_API_KEY` is blank and unused.

## Cutover to Qwen (gated)
1. Confirm the exact Qwen model and download to `/data/models/` (~112 GB).
2. Stop Mistral 7B on GPU0; run `start-vllm-qwen.sh`.
3. Back up `litellm/config.yaml`, apply `config.qwen.yaml`, restart LiteLLM.
4. Set `AI_HEAVY_MODEL=forge` (etc.) on the orchestrator and restart it.
