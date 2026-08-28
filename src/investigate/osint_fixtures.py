"""osint_fixtures.py — TWIN-DERIVED OSINT (synthetic namespace ONLY).

Law 6: external enrichment queries may reference synthetic names only.
The fixture provider therefore GENERATES the entire OSINT universe from the
twin's own seeded state — every entity that exists here was born here.

Fixture record per account (deterministic given seed):
    pseudonym          human-style fake identity ("Jane R. Ogbeni"-like but
                       generated from a fixed first/last-name pool)
    business           optional storefront metadata for merchant-like actors
    device_history     count of distinct devices ever linked
    wallet_exposure    which chains show linked wallets (synthetic addr)
    watch_flags        analyst notes (e.g. aged-domain churn seen)

`registered_names()` exposes the complete synthetic namespace so the
sanctions agent can PROVE no outbound string escapes it.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any, Dict, List, Set

__all__ = ["build_osint_fixtures", "registered_names",
           "SYNTHETIC_NAMESPACE_TAG"]

SYNTHETIC_NAMESPACE_TAG = "prometheus-sandbox"

_FIRST = ["Adaeze", "Kabiru", "Meera", "Tomas", "Lin", "Ravi", "Sofia",
          "Chidi", "Aiko", "Farah", "Diego", "Nadia", "Otto", "Priya"]
_LAST = ["Okafor", "Bello", "Nair", "Sinha", "Okonkwo", "Duarte", "Keita",
         "Novak", "Tanaka", "Rao"]

_BUSINESS_WORDS = ["Ventures", "Global", "Traders", "Hub", "Digital",
                   "Logistics", "Mart", "Studio"]


def _pseudonym(seed_source: str, rng: random.Random) -> str:
    fn = rng.choice(_FIRST)
    ln = rng.choice(_LAST)
    tail = hashlib.sha256(seed_source.encode()).hexdigest()[:4]
    return f"{fn} {ln[0]}. {tail}"


def _wallet_addr(seed_source: str, chain: str) -> str:
    h = hashlib.sha256(f"{SYNTHETIC_NAMESPACE_TAG}:{chain}:"
                       f"{seed_source}".encode()).hexdigest()
    prefix = "0x" if chain in ("eth", "polygon") else \
        {"btc": "bc1q", "tron": "T"}.get(chain, "x")
    return f"{prefix}{h[:36]}"


def build_osint_fixtures(world, seed: int = 42) -> Dict[str, Dict[str, Any]]:
    """Deterministic per-account OSINT dossiers from twin state."""
    fixtures: Dict[str, Dict[str, Any]] = {}
    accounts = list(world.accounts.items())
    merchants = list(world.merchants.items())

    def dossier(ent_id: str, kind: str) -> Dict[str, Any]:
        per_ent_rng = random.Random(
            int(hashlib.sha256(f"{seed}:{ent_id}".encode()).hexdigest(), 16)
            % (2 ** 32))
        rec: Dict[str, Any] = {
            "entity_id": ent_id,
            "kind": kind,
            "namespace": SYNTHETIC_NAMESPACE_TAG,
            "pseudonym": _pseudonym(ent_id, per_ent_rng),
        }
        if kind == "merchant":
            word = per_ent_rng.choice(_BUSINESS_WORDS)
            age_days = per_ent_rng.choice([900, 400, 180, 45, 8])
            rec["business"] = {
                "name": f"{word}-{per_ent_rng.randint(1000, 9999)}",
                "domain_age_days_hint": age_days,
                "registrar_churn_seen": age_days < 200 and per_ent_rng.random() < 0.4,
            }
            if per_ent_rng.random() < 0.35:
                rec["watch_flags"] = ["new_domain", "hosting_change_observed"]
        else:
            dev_count = len(world.accounts[ent_id].linked_devices) \
                if ent_id in world.accounts else 0
            rec["device_history"] = {"distinct_devices": dev_count}
            chains = per_ent_rng.sample(["eth", "btc", "tron"],
                                        k=per_ent_rng.randint(1, 2))
            rec["wallet_exposure"] = {
                ch: _wallet_addr(ent_id, ch) for ch in chains}
            kyc_tier = world.accounts.get(ent_id)
            rec["kyc_tier"] = getattr(kyc_tier, "customer_id", None) is not None
            risk = None
            cust = world.accounts[ent_id].customer_id if ent_id in world.accounts else None
            if cust and cust in world.customers:
                risk = world.customers[cust].risk_state
            rec["risk_state_at_enrichment"] = risk or "unknown"
        return {**rec}

    for acc_id, _acct in accounts:
        fixtures[acc_id] = dossier(acc_id, "account")
    for m_id, _m in merchants:
        fixtures[m_id] = dossier(m_id, "merchant")
    return fixtures


def registered_names(fixtures: Dict[str, Dict[str, Any]]) -> Set[str]:
    """Every outbound-safe string the sandbox owns (ids + pseudonyms +
    wallet addresses + business names)."""
    names: Set[str] = set(fixtures.keys())
    for rec in fixtures.values():
        names.add(rec["pseudonym"])
        if "business" in rec:
            names.add(rec["business"]["name"])
        for addr in rec.get("wallet_exposure", {}).values():
            names.add(addr)
    return names
