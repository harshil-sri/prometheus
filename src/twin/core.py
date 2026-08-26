"""
core.py — Entity state schemas and WorldState container for the Financial Digital Twin.

Defines all dataclass schemas (CustomerState, AccountState, MerchantState, DeviceState,
IPState, WalletState, RelationshipState, BehavioralHistory) and the WorldState class
that holds all entities, provides registration methods, logs transactions, and tracks
attack trajectories.

IMPORTANT CONTRACT: log_transaction() returns a plain dict, NOT a dataclass.
The dict uses key names "from" and "to" (not from_id/to_id), matching the PRD spec.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Entity State Schemas
# ---------------------------------------------------------------------------

@dataclass
class CustomerState:
    customer_id: str
    risk_state: str = "normal"  # normal | elevated | flagged
    kyc_tier: str = "standard"  # low | standard | enhanced


@dataclass
class AccountState:
    account_id: str
    customer_id: str
    opened_at: int = 0
    balance: float = 0.0
    linked_devices: List[str] = field(default_factory=list)
    linked_payees: List[str] = field(default_factory=list)
    profile: Dict[str, Any] = field(default_factory=dict)  # normal-behaviour profile


@dataclass
class MerchantState:
    merchant_id: str
    domain: str = ""
    domain_history: List[Dict[str, Any]] = field(default_factory=list)
    hosting_asn: str = "ASN_1"
    template_fingerprint: str = "wp-plugin-hash-1"
    category: str = "retail"
    age_steps: int = 0


@dataclass
class DeviceState:
    device_id: str
    first_seen_step: int = 0
    linked_accounts: List[str] = field(default_factory=list)


@dataclass
class IPState:
    ip_block: str
    geo: str = "IN-DL"
    reputation: str = "neutral"  # neutral | suspicious | malicious


@dataclass
class WalletState:
    wallet_id: str
    chain: str = "eth"
    linked_accounts: List[str] = field(default_factory=list)


@dataclass
class RelationshipState:
    a: str
    b: str
    type: str = "transacted"
    count: int = 0


@dataclass
class BehavioralHistory:
    account_id: str
    recent_events: List[str] = field(default_factory=list)
    tx_count: int = 0
    total_amount: float = 0.0
    last_tx_step: int = 0
    velocity_window: List[float] = field(default_factory=list)


# ---------------------------------------------------------------------------
# WorldState container
# ---------------------------------------------------------------------------

class WorldState:
    """Holds all entities, provides registration methods, logs transactions,
    tracks attack trajectories, and supports deterministic serialisation."""

    EXTERNAL_ENTITIES = ["EXT_SALARY", "EXT_MERCHANT_PAYOUT", "EXT_BANK"]

    def __init__(self, seed: int = 42):
        self.rng = random.Random(seed)

        # Entity stores
        self.customers: Dict[str, CustomerState] = {}
        self.accounts: Dict[str, AccountState] = {}
        self.merchants: Dict[str, MerchantState] = {}
        self.devices: Dict[str, DeviceState] = {}
        self.ips: Dict[str, IPState] = {}
        self.wallets: Dict[str, WalletState] = {}
        self.relationships: Dict[str, RelationshipState] = {}
        self.behavioural_histories: Dict[str, BehavioralHistory] = {}

        # Logs — lists of plain dicts (not dataclasses)
        self.transactions: List[Dict[str, Any]] = []
        self.trajectories: List[Dict[str, Any]] = []

        # ID counters
        self._next_customer = 0
        self._next_account = 0
        self._next_merchant = 0
        self._next_device = 0
        self._next_ip = 0
        self._next_wallet = 0
        self._next_tx = 0
        self._next_trajectory = 0

        # Current step
        self.current_step: int = 0

    # ------------------------------------------------------------------
    # ID generation
    # ------------------------------------------------------------------

    def next_customer_id(self) -> str:
        self._next_customer += 1
        return f"CUST_{self._next_customer:05d}"

    def next_account_id(self) -> str:
        self._next_account += 1
        return f"ACC_{self._next_account:05d}"

    def next_merchant_id(self) -> str:
        self._next_merchant += 1
        return f"MERCHANT_{self._next_merchant:05d}"

    def next_device_id(self) -> str:
        self._next_device += 1
        return f"DEV_{self._next_device:05d}"

    def next_ip_id(self) -> str:
        self._next_ip += 1
        return f"IP_{self._next_ip:05d}"

    def next_wallet_id(self) -> str:
        self._next_wallet += 1
        return f"WALLET_{self._next_wallet:05d}"

    def next_tx_id(self) -> str:
        self._next_tx += 1
        return f"TX_{self._next_tx:06d}"

    def next_trajectory_id(self) -> str:
        self._next_trajectory += 1
        return f"TRAJ_{self._next_trajectory:05d}"

    # ------------------------------------------------------------------
    # Registration methods
    # ------------------------------------------------------------------

    def add_customer(self, customer_id: Optional[str] = None,
                     risk_state: str = "normal",
                     kyc_tier: str = "standard") -> CustomerState:
        cid = customer_id or self.next_customer_id()
        state = CustomerState(customer_id=cid, risk_state=risk_state, kyc_tier=kyc_tier)
        self.customers[cid] = state
        return state

    def add_account(self, customer_id: str,
                    account_id: Optional[str] = None,
                    opened_at: Optional[int] = None,
                    balance: float = 0.0) -> AccountState:
        aid = account_id or self.next_account_id()
        state = AccountState(
            account_id=aid,
            customer_id=customer_id,
            opened_at=opened_at if opened_at is not None else self.current_step,
            balance=balance,
        )
        self.accounts[aid] = state
        # Initialise behavioural history
        if aid not in self.behavioural_histories:
            self.behavioural_histories[aid] = BehavioralHistory(account_id=aid)
        return state

    def add_merchant(self, merchant_id: Optional[str] = None,
                     domain: str = "",
                     category: str = "retail",
                     hosting_asn: str = "ASN_1",
                     template_fingerprint: str = "wp-plugin-hash-1") -> MerchantState:
        mid = merchant_id or self.next_merchant_id()
        state = MerchantState(
            merchant_id=mid,
            domain=domain or f"merchant{self._next_merchant}.com",
            category=category,
            hosting_asn=hosting_asn,
            template_fingerprint=template_fingerprint,
        )
        self.merchants[mid] = state
        return state

    def add_device(self, device_id: Optional[str] = None,
                   first_seen_step: Optional[int] = None) -> DeviceState:
        did = device_id or self.next_device_id()
        state = DeviceState(
            device_id=did,
            first_seen_step=first_seen_step if first_seen_step is not None else self.current_step,
        )
        self.devices[did] = state
        return state

    def add_ip(self, ip_block: str,
               geo: str = "IN-DL",
               reputation: str = "neutral") -> IPState:
        if ip_block in self.ips:
            return self.ips[ip_block]
        state = IPState(ip_block=ip_block, geo=geo, reputation=reputation)
        self.ips[ip_block] = state
        return state

    def add_wallet(self, wallet_id: Optional[str] = None,
                   chain: str = "eth") -> WalletState:
        wid = wallet_id or self.next_wallet_id()
        state = WalletState(wallet_id=wid, chain=chain)
        self.wallets[wid] = state
        return state

    # ------------------------------------------------------------------
    #Relationship tracking
    # ------------------------------------------------------------------

    def _ensure_relationship(self, a: str, b: str) -> RelationshipState:
        key = f"{a}__{b}" if a < b else f"{b}__{a}"
        if key not in self.relationships:
            self.relationships[key] = RelationshipState(a=a, b=b, type="transacted", count=0)
        return self.relationships[key]

    # ------------------------------------------------------------------
    # Transaction logging — CRITICAL: returns plain dict with "from"/"to"
    # ------------------------------------------------------------------

    def log_transaction(self, from_id: str, to_id: str, amount: float,
                        step: Optional[int] = None,
                        currency: str = "INR",
                        category: str = "retail",
                        device: Optional[str] = None,
                        ip: Optional[str] = None,
                        is_fraud: bool = False,
                        attack_id: Optional[str] = None,
                        trajectory_id: Optional[str] = None) -> Dict[str, Any]:
        """Log a transaction and return a plain dict.

        IMPORTANT: The returned dict uses key names "from" and "to" (not from_id/to_id)
        matching the PRD contract for downstream consumers.
        """
        step = step if step is not None else self.current_step
        tx_id = self.next_tx_id()

        # Build the plain dict with "from" / "to" keys (PRD contract)
        tx = {
            "tx_id": tx_id,
            "step": step,
            "from": from_id,
            "to": to_id,
            "amount": amount,
            "currency": currency,
            "category": category,
            "device": device,
            "ip": ip,
            "is_fraud": is_fraud,
            "attack_id": attack_id,
            "trajectory_id": trajectory_id,
        }
        self.transactions.append(tx)

        # Update balances (only for internal accounts)
        if from_id in self.accounts:
            self.accounts[from_id].balance -= amount
        if to_id in self.accounts:
            self.accounts[to_id].balance += amount

        # Update behavioural history for the sender
        if from_id in self.behavioural_histories:
            bh = self.behavioural_histories[from_id]
            bh.tx_count += 1
            bh.total_amount += amount
            bh.last_tx_step = step
            bh.velocity_window.append(amount)
            # Keep window at most 20 entries
            if len(bh.velocity_window) > 20:
                bh.velocity_window = bh.velocity_window[-20:]
            bh.recent_events.append(tx_id)
            if len(bh.recent_events) > 50:
                bh.recent_events = bh.recent_events[-50:]

        # Update relationship
        rel = self._ensure_relationship(from_id, to_id)
        rel.count += 1

        # Link device to account if not already
        if device and from_id in self.accounts:
            if device not in self.accounts[from_id].linked_devices:
                self.accounts[from_id].linked_devices.append(device)
            if device in self.devices and from_id not in self.devices[device].linked_accounts:
                self.devices[device].linked_accounts.append(from_id)

        return tx

    # ------------------------------------------------------------------
    # Trajectory logging
    # ------------------------------------------------------------------

    def log_trajectory(self, attack_type: str, actions: List[Dict],
                       spec: Dict[str, Any], trajectory_id: Optional[str] = None) -> Dict[str, Any]:
        """Log an attack trajectory and return a plain dict."""
        traj_id = trajectory_id or self.next_trajectory_id()
        traj = {
            "trajectory_id": traj_id,
            "attack_type": attack_type,
            "actions": actions,
            "spec": spec,
        }
        self.trajectories.append(traj)
        return traj

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """Serialise all state to a JSON-compatible dict."""
        return {
            "current_step": self.current_step,
            "customers": {k: asdict(v) for k, v in self.customers.items()},
            "accounts": {k: asdict(v) for k, v in self.accounts.items()},
            "merchants": {k: asdict(v) for k, v in self.merchants.items()},
            "devices": {k: asdict(v) for k, v in self.devices.items()},
            "ips": {k: asdict(v) for k, v in self.ips.items()},
            "wallets": {k: asdict(v) for k, v in self.wallets.items()},
            "relationships": {k: asdict(v) for k, v in self.relationships.items()},
            "behavioural_histories": {k: asdict(v) for k, v in self.behavioural_histories.items()},
            "transactions": list(self.transactions),
            "trajectories": list(self.trajectories),
            "_next_customer": self._next_customer,
            "_next_account": self._next_account,
            "_next_merchant": self._next_merchant,
            "_next_device": self._next_device,
            "_next_ip": self._next_ip,
            "_next_wallet": self._next_wallet,
            "_next_tx": self._next_tx,
            "_next_trajectory": self._next_trajectory,
        }

    def save_json(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump(self.snapshot(), f, indent=2, default=str)

    def load_json(self, path: str) -> None:
        with open(path, "r") as f:
            data = json.load(f)
        self.__dict__.update({k: v for k, v in data.items() if k.startswith("_")})
        self.current_step = data["current_step"]
        # Rebuild entity dicts from serialised dataclasses
        self.customers = {k: CustomerState(**v) for k, v in data["customers"].items()}
        self.accounts = {k: AccountState(**v) for k, v in data["accounts"].items()}
        self.merchants = {k: MerchantState(**v) for k, v in data["merchants"].items()}
        self.devices = {k: DeviceState(**v) for k, v in data["devices"].items()}
        self.ips = {k: IPState(**v) for k, v in data["ips"].items()}
        self.wallets = {k: WalletState(**v) for k, v in data["wallets"].items()}
        self.relationships = {k: RelationshipState(**v) for k, v in data["relationships"].items()}
        self.behavioural_histories = {k: BehavioralHistory(**v) for k, v in data["behavioural_histories"].items()}
        self.transactions = list(data["transactions"])
        self.trajectories = list(data["trajectories"])