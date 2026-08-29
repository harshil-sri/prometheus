"""
test_protocol.py — Phase 3 gate: agentic-commerce structural attacks (T9).

Covers the updates.md 6.1 pillar end-to-end:
  * deterministic Mandate signing (sha256 over canonical JSON)
  * the FIVE RC structural classes land in the naive flow and are blocked
    behind the PCAT gate, one-to-one (RC-x -> P-x)
  * RC-4 is a true check-vs-deduct race (TOCTOU), killed by the atomic CAS
  * benign agent flow passes the gate (0 false positives)
  * judges are pure functions of structural facts (no model in the loop)
  * T9 rows flow through the real twin pipeline (mechanism / attack_id / rc)
  * T9 is isolated from the A1-A6 axes: fingerprint unchanged, T9 not in any
    trainable/held-out set, protocol_structural joins the mechanism namespace
  * the three API endpoints return real decisions/artifacts, never fabricated
"""

import pytest

from twin.core import WorldState
from twin.agentic import AgenticCommerce, _sign, _verify
from policy.pcat import PCATPolicy
from attack.protocol_attacks import (
    MECHANISM_NAME, RC_CLASSES, benign_checkout, run_t9_case,
)
from attack.benchmark_attacks import (
    ALL_ATTACKS, HELD_OUT_ATTACKS, TRAINABLE_ATTACKS,
)
from eval.judges import judge_benign, judge_rc, register_judge
from blue.splits import MECHANISM_REGISTRY, lock_holdout

HOLDOUT_FINGERPRINT = ("292cc7f67639cea556948086f8303fb248249da14f45b3d4825cca8f0473a162")


def _pcat():
    return lambda ac: PCATPolicy.for_agentic(ac)


# ---------------------------------------------------------------------------
# Signing determinism
# ---------------------------------------------------------------------------

def test_mandate_signature_deterministic():
    payload = {"merchant_id": "M1", "payout_account": "WALLET_X"}
    s1 = _sign(payload, "secret-A")
    s2 = _sign(payload, "secret-A")
    s3 = _sign(payload, "secret-B")
    assert s1 == s2                    # same secret -> same signature
    assert s3 != s1                    # different secret -> different signature
    assert _verify(payload, s1, "secret-A") is True
    assert _verify(payload, s1, "secret-B") is False
    assert _verify(payload, "", "secret-A") is False   # no sig -> rejected


# ---------------------------------------------------------------------------
# Structural attack classes: naive lands, PCAT blocks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rc", RC_CLASSES)
def test_attack_naive_lands_pcat_blocks(rc):
    w_naive = WorldState(seed=11)
    pack_naive = run_t9_case(w_naive, seed=3, rc_class=rc, defense_builder=None)
    assert judge_rc(rc, pack_naive) is True, f"{rc} naive must land"

    w_pcat = WorldState(seed=11)
    pack_pcat = run_t9_case(w_pcat, seed=3, rc_class=rc,
                            defense_builder=_pcat())
    assert judge_rc(rc, pack_pcat) is False, f"{rc} behind PCAT must not land"
    if rc == "RC-4":
        # P4 prevents the TOCTOU structurally: the single legit authorization
        # lands, the concurrent one is refused by the atomic CAS.
        assert pack_pcat["over_spent"] is False
        assert pack_pcat["paid_total"] <= pack_pcat["agent_budget"]
        assert pack_pcat["payments"], "one authorization legitimately lands"
    else:
        assert pack_pcat["allowed"] is False
        assert pack_pcat["payments"] == []
        assert pack_pcat["p_blocks"], f"{rc} pcat must record a structural block"


def test_rc1_is_blocked_by_p1_signature_gate():
    w = WorldState(seed=11)
    pack = run_t9_case(w, seed=3, rc_class="RC-1", defense_builder=_pcat())
    assert any("P1" in b for b in pack["p_blocks"])


def test_rc2_is_blocked_by_p2_identity_gate():
    w = WorldState(seed=11)
    pack = run_t9_case(w, seed=3, rc_class="RC-2", defense_builder=_pcat())
    assert any("P2" in b for b in pack["p_blocks"])


def test_rc3_is_blocked_by_p3_channel_gate():
    w = WorldState(seed=11)
    pack = run_t9_case(w, seed=3, rc_class="RC-3", defense_builder=_pcat())
    assert pack["credential_leaked"] is True
    assert any("P3" in b for b in pack["p_blocks"])


def test_rc4_toctou_naive_overspends():
    w = WorldState(seed=11)
    pack = run_t9_case(w, seed=3, rc_class="RC-4", defense_builder=None)
    assert pack["over_spent"] is True
    assert pack["paid_total"] > pack["agent_budget"]
    assert pack["n_authorizations"] == 2


def test_rc4_pcat_atomic_cas_prevents_overspend():
    w = WorldState(seed=11)
    pack = run_t9_case(w, seed=3, rc_class="RC-4", defense_builder=_pcat())
    assert pack["over_spent"] is False
    assert pack["paid_total"] <= pack["agent_budget"]
    assert pack["payments"], "one authorization legitimately lands"


def test_rc5_is_blocked_by_p5_authz_gate():
    w = WorldState(seed=11)
    pack = run_t9_case(w, seed=3, rc_class="RC-5", defense_builder=_pcat())
    assert pack["caller_registered"] is False
    assert any("P5" in b for b in pack["p_blocks"])


# ---------------------------------------------------------------------------
# Benign control (false positives)
# ---------------------------------------------------------------------------

def test_benign_flow_passes_gate():
    for i in range(5):
        w = WorldState(seed=100 + i)
        pack = benign_checkout(w, seed=9 + i, defense_builder=_pcat())
        assert judge_benign(pack) is True, f"benign FP at i={i}"
        assert pack["allowed"] is True
        assert pack["p_blocks"] == []


def test_benign_naive_also_passes():
    w = WorldState(seed=101)
    pack = benign_checkout(w, seed=10, defense_builder=None)
    assert judge_benign(pack) is True


# ---------------------------------------------------------------------------
# Judges are pure / registry of judges is complete
# ---------------------------------------------------------------------------

def test_judges_registered_for_all_classes():
    dummy = {
        "rc_class": "", "allowed": False, "p_blocks": [], "payments": [],
        "paid_total": 0.0, "attacker_received": 0.0, "agent_budget": 100.0,
        "credential_leaked": False, "caller_registered": True,
        "over_spent": False, "n_authorizations": 1,
        "payout": "", "attacker_payout": "",
    }
    for rc in list(RC_CLASSES) + ["BENIGN"]:
        assert isinstance(judge_rc(rc, dummy), bool)
        # an unregistered class must explode loudly, never silently pass
        with pytest.raises(KeyError):
            judge_rc("RC-99", dummy)


def test_judge_is_pure_function():
    w = WorldState(seed=11)
    p1 = run_t9_case(w, seed=3, rc_class="RC-1", defense_builder=None)
    w2 = WorldState(seed=11)
    p2 = run_t9_case(w2, seed=3, rc_class="RC-1", defense_builder=None)
    assert p1 == p2
    assert judge_rc("RC-1", p1) == judge_rc("RC-1", p2)


def test_register_judge_overrides():
    register_judge("RC-1", lambda p: not bool(p["allowed"]))
    try:
        assert judge_rc("RC-1", {"allowed": True}) is False
    finally:
        from eval.judges import judge_rc1
        register_judge("RC-1", judge_rc1)


# ---------------------------------------------------------------------------
# Twin pipeline: T9 rows carry mechanism / attack_id / rc_class
# ---------------------------------------------------------------------------

def test_t9_transactions_flow_through_real_pipeline():
    w = WorldState(seed=11)
    run_t9_case(w, seed=3, rc_class="RC-1", defense_builder=None)
    t9 = [t for t in w.transactions if t.get("mechanism") == MECHANISM_NAME]
    assert t9, "expected protocol_structural payments in the twin log"
    for t in t9:
        assert t["attack_id"] == "T9"
        assert t["rc_class"] == "RC-1"
        assert t["category"] == "agentic"


def test_benign_rows_are_not_fraud():
    w = WorldState(seed=101)
    benign_checkout(w, seed=10, defense_builder=None)
    agentic = [t for t in w.transactions
               if t.get("mechanism") == MECHANISM_NAME]
    assert agentic
    assert all(not t.get("is_fraud") for t in agentic)
    assert all(t.get("category") == "agentic" for t in agentic)


# ---------------------------------------------------------------------------
# T9 is isolated: fingerprint + attack-set namespaces untouched
# ---------------------------------------------------------------------------

def test_t9_not_in_trainable_or_heldout_axes():
    assert "T9" not in TRAINABLE_ATTACKS
    assert "T9" not in HELD_OUT_ATTACKS
    assert "T9" not in ALL_ATTACKS


def test_t9_attributes_via_attack_type_of_tx():
    """Lock the plan's attribution claim: once benchmark_attacks loads
    (importing protocol_attacks registers T9), a T9 row's tx is typed T9."""
    from blue.splits import attack_type_of_tx
    assert attack_type_of_tx({"attack_id": "T9"}) == "T9"
    assert attack_type_of_tx({"attack_id": "T9-RC-1"}) == "T9"


def test_fingerprint_unchanged_by_t9_registration():
    h = lock_holdout()
    assert h.fingerprint == HOLDOUT_FINGERPRINT
    assert "T9" not in set(h.held_out_types)
    assert MECHANISM_NAME in MECHANISM_REGISTRY


# ---------------------------------------------------------------------------
# PCAT gate unit behaviour
# ---------------------------------------------------------------------------

def test_pcat_gate_units():
    pol = PCATPolicy(certified_payouts={"WALLET_CERT": "M1"},
                     allowed_callers={"ID-1": ("payment",)})
    key = "K"
    signed = _sign({"merchant_id": "M1", "payout_account": "WALLET_CERT"}, key)

    ok, why = pol.enforce({"kind": "registry_update", "signed": False, "signature": ""})
    assert not ok and "P1" in why
    ok, why = pol.enforce({"kind": "registry_update", "signed": True,
                           "signature": signed, "registry_key": key,
                           "payload": {"merchant_id": "M1",
                                       "payout_account": "WALLET_CERT"}})
    assert ok, why
    ok, why = pol.enforce({"kind": "registry_update", "signed": True,
                           "signature": "forged", "registry_key": key,
                           "payload": {"merchant_id": "M1",
                                       "payout_account": "WALLET_CERT"}})
    assert not ok

    ok, why = pol.enforce({"kind": "payout_resolve", "payout_account": "WALLET_ATK"})
    assert not ok and "P2" in why
    ok, why = pol.enforce({"kind": "payout_resolve",
                           "payout_account": "WALLET_CERT"})
    assert ok

    ok, why = pol.enforce({"kind": "credential_channel", "observed": True})
    assert not ok and "P3" in why
    ok, why = pol.enforce({"kind": "credential_channel", "observed": False})
    assert ok

    ok, why = pol.enforce({"kind": "tool_call_authz", "scope": ["payment"],
                           "caller_identity": "ID-ATTACKER"})
    assert not ok and "P5" in why
    ok, why = pol.enforce({"kind": "tool_call_authz", "scope": ["payment"],
                           "caller_identity": "ID-1"})
    assert ok
    ok, why = pol.enforce({"kind": "tool_call_authz", "scope": ["utilities"],
                           "caller_identity": "ID-1"})
    assert not ok, "scope must be a subset of the registered scope"


def test_for_agentic_is_live_wired_ordering_independent():
    ac = AgenticCommerce(WorldState(seed=3), seed=3)
    pol = PCATPolicy.for_agentic(ac)            # built BEFORE any merchants
    agent = ac.new_agent(budget=1000)
    m = ac.register_merchant(payout_account="WALLET_LIVE",
                             owner_identity=agent["identity"])
    ok, why = pol.enforce({"kind": "payout_resolve",
                           "merchant_id": m["merchant_id"],
                           "payout_account": "WALLET_LIVE"})
    assert ok, why
    ok, why = pol.enforce({"kind": "tool_call_authz", "scope": ["payment"],
                           "caller_identity": agent["identity"]})
    assert ok, why


# ---------------------------------------------------------------------------
# API endpoints (real decisions; no fabrication)
# ---------------------------------------------------------------------------

@pytest.fixture
def api_client():
    from fastapi.testclient import TestClient
    from api.main import app
    return TestClient(app)


def test_api_protocol_endpoint_serves_real_artifact(api_client):
    r = api_client.get("/api/protocol")
    assert r.status_code == 200
    data = r.json()
    assert data["present"] is True
    assert data["schema"] == "prometheus.protocol_eval.v1"
    assert data["fingerprint_intact"] is True
    for rc in RC_CLASSES:
        assert data["per_rc"][rc]["naive"]["succeeded"] == 1
        assert data["per_rc"][rc]["pcat"]["succeeded"] == 0
    assert data["benign_fp_probe"]["all_passed"] is True
    assert data["citations"][0].startswith("Louck")


def test_api_agentic_status(api_client):
    r = api_client.get("/api/agentic/status")
    assert r.status_code == 200
    data = r.json()
    assert data["ready"] is True
    assert data["agents"] >= 2
    assert "WALLET_LEGIT_MAIN" in data["certified_payouts"]
    assert isinstance(data["events"], list)


def test_api_checkout_naive_lands_pcat_blocks(api_client):
    # RC-2 style: trusted merchant queried, federation returns attacker wallet.
    body = {
        "merchant_id": "MERCHANT_00001",
        "amount": 1000,
        "caller_identity": "DEMO-OWNER-1",
        "rc_class": "RC-2",
        "attacker_controlled_payout": "WALLET_ATK_DEMO",
    }
    r_naive = api_client.post("/api/agentic/checkout", json={**body, "defense": "naive"})
    assert r_naive.status_code == 200
    d_naive = r_naive.json()
    assert d_naive["status"] == "ok"
    assert d_naive["decision"]["allowed"] is True
    assert d_naive["judged_attack_success"] is True

    r_pcat = api_client.post("/api/agentic/checkout", json={**body, "defense": "pcat"})
    d_pcat = r_pcat.json()
    assert d_pcat["decision"]["allowed"] is False
    assert d_pcat["judged_attack_success"] is False
    assert any("P2" in b for b in d_pcat["decision"]["p_blocks"])


def test_api_checkout_benign_live_passes(api_client):
    r = api_client.post("/api/agentic/checkout", json={
        "merchant_id": "MERCHANT_00001",
        "amount": 500,
        "caller_identity": "DEMO-OWNER-1",
        "defense": "pcat",
    })
    assert r.status_code == 200
    d = r.json()
    assert d["decision"]["allowed"] is True
    assert d["decision"]["p_blocks"] == []
    assert d["judged_attack_success"] is None