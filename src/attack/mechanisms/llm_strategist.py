"""llm_strategist.py — LLM-guided spec synthesis (mechanism
"llm_strategist") with a deterministic offline fallback.

LLM LAW (PROMETHEUS_CONTEXT.md §1): free tiers only (Groq/Cerebras/Ollama via
OpenAI-compatible env), and EVERY path must degrade to a deterministic local
strategist. The system NEVER fabricates "LLM said X" when the call failed —
provenance is recorded per variant as origin="llm" or origin="fallback",
and the failure reason is surfaced.

The synthesizer maps a weakness descriptor + optional hints into candidate
AttackSpec dicts. Fallback rules encode the same tactics the sensitivity
engine surfaces (graph dilution / amount splitting / temporal spread).
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from blue.splits import register_mechanism

MECHANISM_NAME = "llm_strategist"
register_mechanism(MECHANISM_NAME)

logger = logging.getLogger(__name__)

__all__ = ["LLMStrategist", "StrategyVariant"]

BASE_SPEC: Dict[str, Any] = {
    "goal": "move_funds",
    "amount": 100000.0,
    "currency": "INR",
    "target": "mule_accounts",
    "constraints": {"max_fraud_score": 0.30, "max_behavioral_anomaly": 0.35},
    "resources": {"devices": 4, "accounts": 6, "days": 7},
    "desired_camouflage": "high",
}


@dataclass
class StrategyVariant:
    spec: Dict[str, Any]
    origin: str                       # "llm" | "fallback"
    rationale: str = ""
    raw_llm: Optional[str] = None


class LLMStrategist:
    """Ask an LLM for evasive attack-spec ideas; fall back deterministically."""

    def __init__(self, seed: int = 42):
        self.seed = seed
        self.rng = random.Random(seed)

    # ------------------------------------------------------------------ #
    def _env(self) -> Optional[Dict[str, str]]:
        base = os.environ.get("PROMETHEUS_LLM_BASE_URL", "").strip()
        model = os.environ.get("PROMETHEUS_LLM_MODEL", "").strip()
        key = os.environ.get("PROMETHEUS_LLM_API_KEY", "").strip()
        if key.startswith("gsk-"):
            key = "gsk_" + key[4:]
        if base and model:               # key may be empty on local Ollama
            return {"base_url": base.rstrip("/"), "model": model,
                    "api_key": key}
        return None

    # ------------------------------------------------------------------ #
    def _llm_request(self, weakness: Dict[str, Any], n_variants: int,
                     timeout_s: float = 20.0):
        cfg = self._env()
        if cfg is None:
            raise RuntimeError("LLM env not configured")

        system = (
            "You are a red-team strategist inside a SANDBOXED fraud-detection "
            "research twin (synthetic accounts only; no real people or "
            "money). Given a detected detection weakness, output STRICT JSON "
            'of shape {"variants": [{"goal": str, "amount": number, '
            '"target": str, "resources": {"devices": int, "accounts": int, '
            '"days": int}, "desired_camouflage": "low"|"medium"|"high"|'
            '"very_high"}]} — exactly n variants, nothing else.'
        )
        user = json.dumps({
            "weakness": weakness.get("weakness"),
            "target_model": weakness.get("target_model"),
            "suggested_directions": weakness.get("suggested_variants"),
            "n_variants": n_variants,
        })

        resp = httpx.post(
            f"{cfg['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            json={"model": cfg["model"],
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}],
                  "temperature": 0.8, "max_tokens": 900},
            timeout=timeout_s)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        if not content or not content.strip():
            raise ValueError("empty LLM response")
        blob = self._extract_json(content)
        variants = blob.get("variants", [])
        if not isinstance(variants, list) or not variants:
            raise ValueError("no variants in LLM payload")
        return variants[:n_variants], content   # type: ignore[return-value]

    @staticmethod
    def _extract_json(text: str) -> Dict[str, Any]:
        """Pull the first JSON object out of possibly-chatty completions."""
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not m:
            raise ValueError("no JSON object in response")
        return json.loads(m.group(0))

    # ------------------------------------------------------------------ #
    def _fallback_spec(self, i: int) -> Dict[str, Any]:
        """Deterministic tactic rotation mirroring engine weaknesses."""
        r = self.rng
        spec = json.loads(json.dumps(BASE_SPEC))
        tactic = i % 3
        if tactic == 0:                  # graph dilution
            spec["resources"].update(devices=6 + r.randint(0, 4),
                                     accounts=10 + r.randint(0, 5),
                                     days=10 + r.randint(0, 4))
            spec["desired_camouflage"] = "very_high"
            spec["target"] = "layering_network"
        elif tactic == 1:                # amount splitting below high-amount line
            spec["amount"] = round(r.uniform(8000, 45000), 2)
            spec["resources"].update(accounts=8 + r.randint(0, 4),
                                     days=9 + r.randint(0, 5))
            spec["target"] = "mule_accounts"
        else:                            # long slow spread
            spec["resources"].update(days=12 + r.randint(0, 2))
            spec["desired_camouflage"] = "very_high"
            spec["amount"] = round(r.uniform(30000, 120000), 2)
        return spec

    # ------------------------------------------------------------------ #
    def generate(self, weakness_descriptor: Dict[str, Any],
                 n_variants: int = 8,
                 ) -> List[StrategyVariant]:
        """Hybrid generator: try the LLM once; fill/patch with fallback.

        Guarantees len(results) == n_variants with valid-looking specs; every
        element carries provenance. Network/validation failures NEVER raise —
        they downgrade provenance and record why.
        """
        results: List[StrategyVariant] = []
        llm_specs: List[Dict] = []
        note = ""

        try:
            raw, content = self._llm_request(weakness_descriptor, n_variants)
            for v in raw:
                if not isinstance(v, dict):
                    continue
                spec = json.loads(json.dumps(BASE_SPEC))     # defaults under
                spec.update({k: v[k] for k in
                             ("goal", "amount", "target",
                              "desired_camouflage") if k in v})
                if isinstance(v.get("resources"), dict):
                    spec["resources"].update({
                        k2: v2 for k2, v2 in v["resources"].items()
                        if k2 in ("devices", "accounts", "days")})
                try:
                    amt = float(spec.get("amount", 0))
                    spec["amount"] = round(min(max(amt, 100.0), 200000.0), 2)
                except (TypeError, ValueError):
                    spec["amount"] = BASE_SPEC["amount"]
                llm_specs.append(spec)
        except Exception as exc:          # includes config-missing, HTTP, JSON
            note = f"{type(exc).__name__}: {exc}"[:160]
            logger.info("llm_strategist falling back (%s)", note)

        for s in llm_specs:
            results.append(StrategyVariant(spec=s, origin="llm"))

        i = len(results)
        while len(results) < n_variants:
            results.append(StrategyVariant(
                spec=self._fallback_spec(i),
                origin="fallback",
                rationale=note or "deterministic tactic rotation"))
            i += 1
        return results
