#!/usr/bin/env bash
# TrueNorth Range — Ollama model bootstrap
#
# Pulls the local flagship LLM (and, by default, an embedding model for
# curriculum RAG) onto a GPU node's Ollama server. Run once per GPU node after
# `docker compose -f infra/platform/docker/compose.ollama.yml up -d`.
#
# Usage:
#   bash scripts/ollama-bootstrap.sh                  # pull into local container
#   OLLAMA_CONTAINER=tn-ollama bash scripts/ollama-bootstrap.sh
#   FLAGSHIP_MODEL=qwen3.5:122b-a1mb-fp8 bash scripts/ollama-bootstrap.sh
#   PULL_EMBED=0 bash scripts/ollama-bootstrap.sh     # skip the embedding model
#
# NOTE: confirm the exact Ollama registry tag for the flagship model before
# running — the pull tag must match what the orchestrator routes to.
set -euo pipefail

# ── Flagship LLM ──────────────────────────────────────────────────────────
# Air-gapped cyber-assistant model. Override FLAGSHIP_MODEL if the registry tag
# differs. A ~122B FP8 model fits comfortably on a 2× H200 node (~282 GB VRAM).
FLAGSHIP_MODEL="${FLAGSHIP_MODEL:-qwen3.5:122b-a1mb-fp8}"

# ── Embedding model (curriculum RAG) ──────────────────────────────────────
# bge-m3 is 1024-dim, matching EMBED_DIM in curriculum_ingest.py. Without it,
# RAG silently degrades from semantic kNN to BM25 keyword search.
EMBED_MODEL="${EMBED_MODEL:-bge-m3}"
PULL_EMBED="${PULL_EMBED:-1}"

OLLAMA_CONTAINER="${OLLAMA_CONTAINER:-tn-ollama}"

# Run `ollama` either inside the container or natively, whichever is present.
ollama_cmd() {
  if command -v ollama >/dev/null 2>&1; then
    ollama "$@"
  elif command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' | grep -qx "$OLLAMA_CONTAINER"; then
    docker exec "$OLLAMA_CONTAINER" ollama "$@"
  else
    echo "ERROR: no 'ollama' binary and no running '$OLLAMA_CONTAINER' container found." >&2
    exit 1
  fi
}

echo "== TrueNorth Ollama bootstrap =="
echo "Flagship model : $FLAGSHIP_MODEL"
[[ "$PULL_EMBED" == "1" ]] && echo "Embedding model: $EMBED_MODEL"

echo "+ pulling $FLAGSHIP_MODEL (this can take a while for a ~122B model)"
ollama_cmd pull "$FLAGSHIP_MODEL"

if [[ "$PULL_EMBED" == "1" ]]; then
  echo "+ pulling $EMBED_MODEL"
  ollama_cmd pull "$EMBED_MODEL"
fi

echo
echo "== Installed models =="
ollama_cmd list

echo
echo "Done. Point the orchestrator at this node, e.g.:"
echo "  OLLAMA_NODES=$(hostname -s)=http://$(hostname -I 2>/dev/null | awk '{print $1}'):11434"
