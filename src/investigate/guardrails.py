"""investigate.guardrails — Security rails for the deep-path investigator.

Implements the security posture locked for Phase 8:

* Prompt-injection defense: LLM-bound text is sanitized through one choke
  point; messages carrying instruction-override payloads are BLOCKED
  (GuardrailViolation), never silently rewritten past the model.
* Secret hygiene: anything that looks like a credential is redacted before
  it can reach a prompt or a persisted artifact.
* Identifier law: case ids are strictly validated (blocks traversal).
* Network law: the LLM base URL must be HTTPS unless it is an explicitly
  local development endpoint.
"""

from __future__ import annotations

import json
import re
from typing import List, Optional, Tuple

__all__ = [
    "GuardrailViolation", "sanitize_text", "redact_secrets",
    "validate_case_id", "validate_llm_base_url",
    "compose_case_prompt", "injection_report",
]

_CASE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

# High-confidence instruction-override payloads — hard block
_INJECTION_HARD = [
    re.compile(p, re.IGNORECASE) for p in (
        r"ignore\s+(all\s+)?(previous|prior|above)",
        r"disregard\s+(the\s+)?(previous|above|system)",
        r"(new|revised)\s+(system\s+)?instructions?\s*:",
        r"you\s+are\s+now\s+(a|an|the)",
        r"act\s+as\s+(if|though)\s+you\s+(have\s+)?no\s+rules",
        r"developer\s+mode",
        r"jailbreak",
        r"<\s*/?\s*(system|assistant)\s*>",
    )
]

# Lower-severity markers — allowed but surfaced in the report
_INJECTION_SOFT = [
    re.compile(p, re.IGNORECASE) for p in (
        r"repeat\s+(your|the)\s+(system\s+)?prompt",
        r"print\s+(your|the)\s+(instructions|prompt)",
        r"what\s+(are|is)\s+your\s+(rules|configuration)",
        r"roleplay\s+as",
        r"pretend\s+(to\s+be|that)",
    )
]

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),                       # groq/openai style
    re.compile(r"PROMETHEUS_LLM_[A-Z_]+\s*[=:]\s*\S+"),
    re.compile(r"[A-Za-z0-9]{32,}"),                          # opaque blobs
]

_MAX_TEXT_LEN = 6000

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0"}
_ALLOWED_REMOTE_SUFFIXES = ("api.groq.com", "api.cerebras.ai")


class GuardrailViolation(Exception):
    """Raised when input trips a hard security rail."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = redact_secrets(detail)[:300]
        super().__init__(f"GUARDRAIL[{reason}]: {self.detail}")


def injection_report(text: str) -> Tuple[List[str], List[str]]:
    """Return (hard_hits_patterns_matched, soft_flags)."""
    hard = [p.pattern for p in _INJECTION_HARD if p.search(text)]
    soft = [p.pattern for p in _INJECTION_SOFT if p.search(text)]
    return hard, soft


def sanitize_text(text: str, max_len: int = _MAX_TEXT_LEN,
                  allow_soft_flags: bool = True) -> str:
    """One choke point for any externally-derived text heading into an LLM.

    - truncates overlong blobs
    - redacts secret-looking substrings
    - HARD-BLOCKS instruction-override payloads (fails closed)
    Soft flags pass through (they belong in the narrative's own risk view),
    but callers can tighten with allow_soft_flags=False.
    """
    if text is None:
        return ""
    cleaned = redact_secrets(str(text))[:max_len]
    hard, soft = injection_report(cleaned)
    if hard:
        raise GuardrailViolation(
            "prompt_injection", f"patterns={hard[:3]} snippet={cleaned[:120]}")
    if soft and not allow_soft_flags:
        raise GuardrailViolation("soft_injection_markers", str(soft[:3]))
    return cleaned


def redact_secrets(text: str) -> str:
    out = str(text)
    for pat in _SECRET_PATTERNS:
        def _mask(m):
            s = m.group(0)
            return s[:6] + "…REDACTED…" + s[-2:] if len(s) > 12 \
                else "…REDACTED…"
        out = pat.sub(_mask, out)
    return out


def validate_case_id(case_id: str) -> str:
    if not isinstance(case_id, str) or not _CASE_ID_RE.match(case_id):
        raise GuardrailViolation(
            "bad_case_id",
            "ids must match ^[A-Za-z0-9_-]{1,64}$ "
            "(traversal/format attacks blocked)")
    return case_id


def validate_llm_base_url(base_url: str) -> str:
    url = (base_url or "").strip()
    ok_local = url.startswith("http://") and any(
        h in url.split("//", 1)[-1] for h in _LOCAL_HOSTS)
    ok_remote = url.startswith("https://") and any(
        url.removeprefix("https://").startswith(sfx)
        for sfx in _ALLOWED_REMOTE_SUFFIXES)
    if not (ok_local or ok_remote):
        raise GuardrailViolation(
            "llm_base_url_not_allowlisted",
            f"{url[:80]} (free-tier/local endpoints only)")
    return url


def compose_case_prompt(case_context: dict,
                        evidence_summaries: List[str],
                        max_chars: int = 8000) -> Tuple[str, str]:
    """Deterministic system+user pair for narrative generation.

    Every dynamic string has already passed sanitize_text() upstream of this
    function being called by the CaseManager — composition itself adds only
    fixed scaffolding.
    """
    system = (
        "You are the narrative writer of a fraud investigation console.\n"
        "Rules you cannot change:\n"
        "* Summarize ONLY facts present in the supplied evidence list.\n"
        "* Every factual claim must cite its [EVIDENCE-ID].\n"
        "* If evidence is missing, say so instead of inventing detail.\n"
        "* Output <=200 words."
    )
    user = json.dumps({
        "case": case_context,
        "evidence": evidence_summaries[:40],
    }, sort_keys=True, default=str)[:max_chars]
    return system, user
