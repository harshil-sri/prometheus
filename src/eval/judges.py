"""
judges.py — deterministic success judges for the T9 protocol evaluation.

Each judge is a PURE function of a case pack (the structural facts recorded
by the attack/benign runs): attacker_received, over_spent, credential_leaked,
caller_registered, allowed, p_blocks. There is no model in the loop — success
is a structural fact, exactly the claim updates.md 6.1 makes for RC-1..RC-5.

Modes:
    attack: "naive" packs must vote SUCCESS (attack landed),
            "pcat" packs must vote FAILURE (structural gate refused the call).
    benign: FINGERPRINT probe — "pcat" packs must vote SUCCESS (low FP).
"""

from __future__ import annotations

from typing import Any, Dict

_JUDGES: Dict[str, Any] = {}


def judge_rc1(pack: Dict[str, Any]) -> bool:
    # A rogue unsigned registry entry must not be able to move funds.
    return bool(pack["attacker_received"] > 0)


def judge_rc2(pack: Dict[str, Any]) -> bool:
    # A blindly-trusted federation payout resolution must not pay the attacker.
    return bool(pack["attacker_received"] > 0)


def judge_rc3(pack: Dict[str, Any]) -> bool:
    # A leaked credential must not fund an attacker-controlled destination.
    return bool(pack["credential_leaked"] and pack["paid_total"] > 0)


def judge_rc4(pack: Dict[str, Any]) -> bool:
    # Concurrent authorizations must never exceed the mandate budget (TOCTOU).
    return bool(pack["over_spent"])


def judge_rc5(pack: Dict[str, Any]) -> bool:
    # A privileged checkout call with an unregistered caller must not pay.
    return bool(pack["paid_total"] > 0 and not pack["caller_registered"])


def judge_benign(pack: Dict[str, Any]) -> bool:
    # The honest flow must pass the gate: allowed, paid, not to an attacker,
    # no leak, no over-spend, registered caller, no structural block.
    return bool(
        pack["allowed"]
        and pack["paid_total"] > 0
        and float(pack["attacker_received"]) == 0
        and not pack["credential_leaked"]
        and pack["caller_registered"]
        and not pack["over_spent"]
        and not pack["p_blocks"]
    )


def judge_rc(rc_class: str, pack: Dict[str, Any]) -> bool:
    return _JUDGES[rc_class](pack)


def register_judge(rc_class: str, fn) -> None:
    _JUDGES[rc_class] = fn


for _rc, _fn in {
    "RC-1": judge_rc1,
    "RC-2": judge_rc2,
    "RC-3": judge_rc3,
    "RC-4": judge_rc4,
    "RC-5": judge_rc5,
    "BENIGN": judge_benign,
}.items():
    register_judge(_rc, _fn)