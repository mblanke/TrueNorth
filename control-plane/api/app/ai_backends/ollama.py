"""Ollama engine adapter: ``/api/generate`` for completions, LAN node probing."""

from __future__ import annotations

import logging

import httpx

from .base import BaseAIEngine, empty_probe

logger = logging.getLogger("truenorth.api.ai_backends.ollama")

DEFAULT_MODEL = "llama3.1:latest"


class OllamaEngine(BaseAIEngine):
    uses_fleet_nodes = True

    def generate(
        self,
        prompt: str,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 30.0,
        model: str = "",
    ) -> dict:
        r = httpx.post(
            f"{base_url.rstrip('/')}/api/generate",
            json={"model": model or DEFAULT_MODEL, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        return {"response": data.get("response", ""), "model": data.get("model", "unknown")}

    def probe(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        host: str = "",
        port: int = 0,
        timeout: float = 5.0,
    ) -> dict:
        """Probe one Ollama node (``host:port``): version, pulled and loaded models."""
        base = f"http://{host}:{port}"
        result = empty_probe(base)
        try:
            vr = httpx.get(f"{base}/api/version", timeout=timeout)
            vr.raise_for_status()
            result["version"] = vr.json().get("version")

            tr = httpx.get(f"{base}/api/tags", timeout=timeout)
            tr.raise_for_status()
            result["models"] = [
                {
                    "name": m["name"],
                    "size_bytes": m.get("size", 0),
                    "family": m.get("details", {}).get("family", ""),
                    "parameter_size": m.get("details", {}).get("parameter_size", ""),
                    "quantization": m.get("details", {}).get("quantization_level", ""),
                }
                for m in tr.json().get("models", [])
            ]

            pr = httpx.get(f"{base}/api/ps", timeout=timeout)
            if pr.status_code == 200:
                result["running"] = [rm.get("name", "") for rm in pr.json().get("models", [])]

            result["online"] = True
        except Exception as exc:
            logger.warning("Probe failed for %s:%s — %s", host, port, exc)
        return result
