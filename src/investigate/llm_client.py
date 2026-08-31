"""llm_client.py — Free-tier OpenAI-compatible chat client for the
investigator's narrative writer.

LLM LAW (context §1): free tiers only (Groq/Cerebras/local Ollama) selected
via PROMETHEUS_LLM_* env vars; base URLs pass the guardrail allowlist; every
call path has a deterministic offline fallback and NEVER fabricates model
output on failure (failures raise LLMUnavailable).

`transport` accepts an httpx transport (e.g. httpx.MockTransport) so unit
tests exercise the exact wire contract without network.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from .guardrails import GuardrailViolation, validate_llm_base_url

logger = logging.getLogger(__name__)

__all__ = ["LLMClient", "LLMUnavailable"]


class LLMUnavailable(RuntimeError):
    """Raised when no LLM is configured or a call fails after retry."""


class LLMClient:
    def __init__(self, transport: Optional[httpx.BaseTransport] = None,
                 timeout_s: float = 20.0):
        base = os.environ.get("PROMETHEUS_LLM_BASE_URL", "").strip()
        self.model = os.environ.get("PROMETHEUS_LLM_MODEL", "").strip()
        key = os.environ.get("PROMETHEUS_LLM_API_KEY", "").strip()
        if key.startswith("gsk-"):
            key = "gsk_" + key[4:]
        self.api_key = key
        self.available = bool(base and self.model)
        if self.available:
            try:
                self.base_url = validate_llm_base_url(base)
            except GuardrailViolation as gv:
                logger.warning("LLM disabled by guardrail: %s", gv.reason)
                self.available = False
                raise
        else:
            self.base_url = ""
        self.timeout_s = timeout_s
        self._transport = transport

    # ------------------------------------------------------------------ #
    def chat(self, messages: List[Dict[str, str]],
             max_tokens: int = 400, temperature: float = 0.2,
             ) -> Dict[str, Any]:
        if not self.available:
            raise LLMUnavailable("no LLM configured "
                                 "(set PROMETHEUS_LLM_BASE_URL/_MODEL)")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        attempts = 2
        last_exc: Optional[str] = None
        for attempt in range(attempts):
            try:
                with httpx.Client(transport=self._transport,
                                  timeout=self.timeout_s) as client:
                    resp = client.post(
                        f"{self.base_url}/chat/completions",
                        headers=headers,
                        content=json.dumps(payload),
                    )
                if resp.status_code in (429, 500, 502, 503):
                    raise httpx.HTTPStatusError(
                        f"transient {resp.status_code}",
                        request=resp.request, response=resp)
                resp.raise_for_status()
                body = resp.json()
                text = body["choices"][0]["message"]["content"]
                return {"text": text, "mode": "llm",
                        "model": self.model,
                        "finish": body["choices"][0].get("finish_reason")}
            except Exception as exc:                     # noqa: BLE001
                last_exc = f"{type(exc).__name__}: {exc}"[:200]
                continue                                  # single warm retry

        raise LLMUnavailable(f"chat failed after {attempts} attempts: "
                             f"{last_exc}")
