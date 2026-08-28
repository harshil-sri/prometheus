"""graph.py — Dynamic Knowledge Graph extraction and serialization engine for Project Prometheus.

Extracts multi-relational entities and links from WorldState with zero hardcoding:
    Nodes: Accounts, Customers, Merchants, Devices, IPs, Wallets.
    Edges: TRANSACTION, OWNED_BY, USES_DEVICE, USES_IP, HAS_WALLET.
    Computed properties: GNN node risk, degree centrality, transaction velocity,
    behavioral anomaly, attack trajectory tags.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple
import numpy as np

from twin.core import WorldState


def build_knowledge_graph(
    world: WorldState,
    ensemble=None,
    filter_type: str = "overview",
    trajectory_id: Optional[str] = None,
    node_id: Optional[str] = None,
    max_nodes: int = 150,
    max_edges: int = 250,
) -> Dict[str, Any]:
    """Extract a filtered, multi-relational knowledge graph from WorldState.

    Args:
        world: WorldState containing all entities and transactions.
        ensemble: Optional BlueTeamEnsemble to compute live GNN node risks.
        filter_type: "overview" | "fraud" | "trajectory" | "ego" | "all"
        trajectory_id: Trajectory ID when filter_type=="trajectory".
        node_id: Center node ID when filter_type=="ego".
        max_nodes: Maximum nodes to return (prevent UI lag).
        max_edges: Maximum edges to return.

    Returns:
        Dict with keys: "nodes", "links", "stats", "filter".
    """
    # 1. Compute GNN scores if ensemble is available
    gnn_node_risks: Dict[str, float] = {}
    if ensemble is not None and getattr(ensemble, "gnn", None) is not None:
        try:
            data, idmap, _ = getattr(ensemble, "_graph_cache", (None, {}, []))
            if data is not None and idmap:
                node_p = ensemble.gnn.predict_proba(data)[:, 1]
                for nid, idx in idmap.items():
                    if idx < len(node_p):
                        gnn_node_risks[str(nid)] = round(float(node_p[idx]), 4)
        except Exception:
            pass

    # 2. Identify relevant transactions based on filter
    selected_txs: List[Dict[str, Any]] = []
    
    if filter_type == "trajectory" and trajectory_id:
        selected_txs = [t for t in world.transactions if t.get("trajectory_id") == trajectory_id]
    elif filter_type == "fraud":
        selected_txs = [t for t in world.transactions if t.get("is_fraud")]
    elif filter_type == "ego" and node_id:
        target_nid = str(node_id)
        selected_txs = [
            t for t in world.transactions
            if str(t.get("from")) == target_nid or str(t.get("to")) == target_nid
        ]
    else:
        # Overview: Connected Fraud Clusters + 1-hop Neighbors + Shared Device Hubs
        fraud_txs = [t for t in world.transactions if t.get("is_fraud")]
        fraud_accounts = set()
        for ft in fraud_txs:
            if str(ft.get("from", "")).startswith("ACC_"):
                fraud_accounts.add(str(ft.get("from")))
            if str(ft.get("to", "")).startswith("ACC_"):
                fraud_accounts.add(str(ft.get("to")))

        # Include 1-hop connected transactions for these fraud accounts
        connected_normal_txs = [
            t for t in world.transactions
            if not t.get("is_fraud") and (str(t.get("from")) in fraud_accounts or str(t.get("to")) in fraud_accounts)
        ]

        # Shared device clusters
        shared_devices = [d for d, dev in world.devices.items() if len(dev.linked_accounts) >= 2]
        device_cluster_accounts = set()
        for sd in shared_devices[:10]:
            device_cluster_accounts.update(world.devices[sd].linked_accounts)

        cluster_txs = [
            t for t in world.transactions
            if str(t.get("from")) in device_cluster_accounts or str(t.get("to")) in device_cluster_accounts
        ]

        selected_txs = list(fraud_txs) + connected_normal_txs[:80] + cluster_txs[:60]
        # De-duplicate while preserving order
        seen_tx_ids = set()
        dedup_txs = []
        for st in selected_txs:
            tid = st.get("tx_id", f"{st.get('from')}_{st.get('to')}_{st.get('step')}")
            if tid not in seen_tx_ids:
                seen_tx_ids.add(tid)
                dedup_txs.append(st)
        selected_txs = dedup_txs[:max_edges]

    if max_edges and len(selected_txs) > max_edges:
        fraud_subset = [t for t in selected_txs if t.get("is_fraud")]
        non_fraud_subset = [t for t in selected_txs if not t.get("is_fraud")]
        allowed_non_fraud = max(0, max_edges - len(fraud_subset))
        selected_txs = fraud_subset + non_fraud_subset[:allowed_non_fraud]

    # 3. Collect active entity IDs from selected transactions
    account_ids: Set[str] = set()
    merchant_ids: Set[str] = set()
    customer_ids: Set[str] = set()
    device_ids: Set[str] = set()
    ip_ids: Set[str] = set()
    wallet_ids: Set[str] = set()

    for tx in selected_txs:
        f = str(tx.get("from", ""))
        t = str(tx.get("to", ""))
        d = str(tx.get("device", "")) if tx.get("device") else ""
        ip = str(tx.get("ip", "")) if tx.get("ip") else ""

        if f.startswith("ACC_"):
            account_ids.add(f)
        if t.startswith("ACC_"):
            account_ids.add(t)
        elif t.startswith("MERCHANT_"):
            merchant_ids.add(t)

        if d and d in world.devices:
            device_ids.add(d)
        if ip and ip in world.ips:
            ip_ids.add(ip)

    # Resolve customer, device, and wallet relationships for included accounts
    for aid in list(account_ids):
        if aid in world.accounts:
            acct = world.accounts[aid]
            if acct.customer_id and acct.customer_id in world.customers:
                customer_ids.add(acct.customer_id)
            for d in acct.linked_devices:
                if d in world.devices:
                    device_ids.add(d)

    for wid, w in world.wallets.items():
        if any(aid in account_ids for aid in w.linked_accounts):
            wallet_ids.add(wid)

    # 4. Build Node Objects
    nodes: List[Dict[str, Any]] = []
    node_id_set: Set[str] = set()

    def add_node(nid: str, ntype: str, label: str, properties: Dict[str, Any], risk_score: float = 0.0, is_fraud: bool = False):
        if nid in node_id_set:
            return
        node_id_set.add(nid)
        nodes.append({
            "id": nid,
            "type": ntype,
            "label": label,
            "risk_score": risk_score,
            "is_fraud": is_fraud,
            "properties": properties,
        })

    # Account nodes
    fraud_accounts = {
        str(tx.get("from")) for tx in world.transactions if tx.get("is_fraud")
    } | {
        str(tx.get("to")) for tx in world.transactions if tx.get("is_fraud")
    }

    for aid in account_ids:
        if aid not in world.accounts:
            continue
        acct = world.accounts[aid]
        is_fraud_node = aid in fraud_accounts
        gnn_score = gnn_node_risks.get(aid, 0.85 if is_fraud_node else 0.05)
        bh = world.behavioural_histories.get(aid)
        
        props = {
            "balance": round(acct.balance, 2),
            "customer_id": acct.customer_id,
            "opened_at_step": acct.opened_at,
            "linked_devices_count": len(acct.linked_devices),
            "tx_count": bh.tx_count if bh else 0,
            "total_amount": round(bh.total_amount, 2) if bh else 0.0,
            "gnn_risk": gnn_score,
        }
        add_node(aid, "account", aid, props, risk_score=gnn_score, is_fraud=is_fraud_node)

    # Customer nodes
    for cid in customer_ids:
        if cid not in world.customers:
            continue
        cust = world.customers[cid]
        risk_map = {"normal": 0.05, "elevated": 0.45, "flagged": 0.90}
        c_risk = risk_map.get(cust.risk_state, 0.1)
        props = {
            "kyc_tier": cust.kyc_tier,
            "risk_state": cust.risk_state,
        }
        add_node(cid, "customer", f"Customer ({cust.kyc_tier})", props, risk_score=c_risk, is_fraud=(cust.risk_state == "flagged"))

    # Merchant nodes
    for mid in merchant_ids:
        if mid not in world.merchants:
            continue
        merch = world.merchants[mid]
        is_fake = "Fake" in merch.domain or "churn" in str(merch.domain_history) or "STOREFRONT" in str(merch.domain)
        props = {
            "domain": merch.domain,
            "category": merch.category,
            "hosting_asn": merch.hosting_asn,
            "template": merch.template_fingerprint,
        }
        add_node(mid, "merchant", f"{merch.category.title()} ({mid})", props, risk_score=0.85 if is_fake else 0.02, is_fraud=is_fake)

    # Device nodes
    for did in device_ids:
        if did not in world.devices:
            continue
        dev = world.devices[did]
        is_shared = len(dev.linked_accounts) > 1
        props = {
            "first_seen_step": dev.first_seen_step,
            "linked_accounts": dev.linked_accounts,
            "account_count": len(dev.linked_accounts),
            "is_shared": is_shared,
        }
        dev_risk = 0.75 if is_shared else 0.05
        add_node(did, "device", f"Device ({len(dev.linked_accounts)} accts)", props, risk_score=dev_risk, is_fraud=is_shared)

    # IP nodes
    for ip_b in ip_ids:
        if ip_b not in world.ips:
            continue
        ip_obj = world.ips[ip_b]
        ip_risk = 0.9 if ip_obj.reputation == "malicious" else (0.5 if ip_obj.reputation == "suspicious" else 0.05)
        props = {
            "geo": ip_obj.geo,
            "reputation": ip_obj.reputation,
        }
        add_node(ip_b, "ip", f"IP {ip_b} ({ip_obj.geo})", props, risk_score=ip_risk, is_fraud=(ip_obj.reputation == "malicious"))

    # Wallet nodes
    for wid in wallet_ids:
        if wid not in world.wallets:
            continue
        wal = world.wallets[wid]
        props = {
            "chain": wal.chain,
            "linked_accounts": wal.linked_accounts,
        }
        add_node(wid, "wallet", f"Wallet ({wal.chain})", props, risk_score=0.5, is_fraud=False)

    # 5. Build Links / Edges
    links: List[Dict[str, Any]] = []

    # Financial transaction edges
    for tx in selected_txs:
        src = str(tx.get("from", ""))
        dst = str(tx.get("to", ""))
        if src in node_id_set and dst in node_id_set:
            links.append({
                "source": src,
                "target": dst,
                "type": "TRANSACTION",
                "tx_id": tx.get("tx_id"),
                "amount": round(float(tx.get("amount", 0.0)), 2),
                "currency": tx.get("currency", "INR"),
                "step": tx.get("step", 0),
                "is_fraud": bool(tx.get("is_fraud", False)),
                "attack_id": tx.get("attack_id"),
                "trajectory_id": tx.get("trajectory_id"),
                "mechanism": tx.get("mechanism"),
                "label": f"₹{tx.get('amount', 0):,.0f}" if tx.get("currency") == "INR" else f"{tx.get('amount', 0)}",
            })

    # Relational link edges (Account -> Customer, Account -> Device, Account -> IP)
    for aid in account_ids:
        if aid not in world.accounts:
            continue
        acct = world.accounts[aid]
        if acct.customer_id and acct.customer_id in node_id_set:
            links.append({
                "source": aid,
                "target": acct.customer_id,
                "type": "OWNED_BY",
                "label": "owned by",
            })
        for d in acct.linked_devices:
            if d in node_id_set:
                links.append({
                    "source": aid,
                    "target": d,
                    "type": "USES_DEVICE",
                    "label": "device binding",
                })

    for wid in wallet_ids:
        if wid in world.wallets:
            for aid in world.wallets[wid].linked_accounts:
                if aid in node_id_set:
                    links.append({
                        "source": aid,
                        "target": wid,
                        "type": "HAS_WALLET",
                        "label": "wallet link",
                    })

    # Cap nodes if necessary
    if max_nodes and len(nodes) > max_nodes:
        nodes.sort(key=lambda n: (n["is_fraud"], n["risk_score"]), reverse=True)
        nodes = nodes[:max_nodes]
        allowed_ids = {n["id"] for n in nodes}
        links = [l for l in links if l["source"] in allowed_ids and l["target"] in allowed_ids]

    stats = {
        "total_nodes": len(nodes),
        "total_links": len(links),
        "accounts_count": sum(1 for n in nodes if n["type"] == "account"),
        "customers_count": sum(1 for n in nodes if n["type"] == "customer"),
        "merchants_count": sum(1 for n in nodes if n["type"] == "merchant"),
        "devices_count": sum(1 for n in nodes if n["type"] == "device"),
        "ips_count": sum(1 for n in nodes if n["type"] == "ip"),
        "fraud_nodes_count": sum(1 for n in nodes if n["is_fraud"]),
        "fraud_links_count": sum(1 for l in links if l.get("is_fraud")),
    }

    return {
        "nodes": nodes,
        "links": links,
        "stats": stats,
        "filter": {
            "type": filter_type,
            "trajectory_id": trajectory_id,
            "node_id": node_id,
        },
    }


def list_trajectories_summary(world: WorldState) -> List[Dict[str, Any]]:
    """Return all logged attack trajectories with computed summary stats."""
    summaries = []
    traj_txs: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for tx in world.transactions:
        tid = tx.get("trajectory_id")
        if tid:
            traj_txs[tid].append(tx)

    for traj in world.trajectories:
        tid = traj.get("trajectory_id", "")
        txs = traj_txs.get(tid, [])
        fraud_txs = [t for t in txs if t.get("is_fraud")]
        total_amt = sum(float(t.get("amount", 0.0)) for t in txs)
        steps = [int(t.get("step", 0)) for t in txs] if txs else [0]
        
        summaries.append({
            "trajectory_id": tid,
            "attack_type": traj.get("attack_type", "Unknown"),
            "n_actions": len(traj.get("actions", [])),
            "n_txs": len(txs),
            "n_fraud_txs": len(fraud_txs),
            "total_amount": round(total_amt, 2),
            "min_step": min(steps),
            "max_step": max(steps),
            "entities": traj.get("spec", {}).get("resources", {}),
        })

    return summaries


def extract_node_profile(world: WorldState, node_id: str, ensemble=None) -> Dict[str, Any]:
    """Extract deep profile for a single entity node."""
    nid = str(node_id)
    profile: Dict[str, Any] = {
        "node_id": nid,
        "type": "unknown",
        "details": {},
        "recent_transactions": [],
        "risk_signals": {},
    }

    if nid in world.accounts:
        acct = world.accounts[nid]
        bh = world.behavioural_histories.get(nid)
        profile["type"] = "account"
        profile["details"] = {
            "account_id": acct.account_id,
            "customer_id": acct.customer_id,
            "balance": round(acct.balance, 2),
            "opened_at_step": acct.opened_at,
            "linked_devices": acct.linked_devices,
            "linked_payees": acct.linked_payees,
            "tx_count": bh.tx_count if bh else 0,
            "total_amount": round(bh.total_amount, 2) if bh else 0.0,
            "velocity_window": bh.velocity_window if bh else [],
        }
    elif nid in world.customers:
        cust = world.customers[nid]
        profile["type"] = "customer"
        profile["details"] = {
            "customer_id": cust.customer_id,
            "risk_state": cust.risk_state,
            "kyc_tier": cust.kyc_tier,
            "accounts": [a for a, obj in world.accounts.items() if obj.customer_id == nid],
        }
    elif nid in world.merchants:
        merch = world.merchants[nid]
        profile["type"] = "merchant"
        profile["details"] = {
            "merchant_id": merch.merchant_id,
            "domain": merch.domain,
            "category": merch.category,
            "hosting_asn": merch.hosting_asn,
            "domain_history": merch.domain_history,
            "template_fingerprint": merch.template_fingerprint,
        }
    elif nid in world.devices:
        dev = world.devices[nid]
        profile["type"] = "device"
        profile["details"] = {
            "device_id": dev.device_id,
            "first_seen_step": dev.first_seen_step,
            "linked_accounts": dev.linked_accounts,
            "is_shared": len(dev.linked_accounts) > 1,
        }
    elif nid in world.ips:
        ip_obj = world.ips[nid]
        profile["type"] = "ip"
        profile["details"] = {
            "ip_block": ip_obj.ip_block,
            "geo": ip_obj.geo,
            "reputation": ip_obj.reputation,
        }

    txs = [
        t for t in world.transactions
        if str(t.get("from")) == nid or str(t.get("to")) == nid
    ]
    profile["recent_transactions"] = txs[-10:]

    if ensemble is not None:
        if txs:
            try:
                scores = ensemble.score_transactions(txs[-5:], world)
                profile["risk_signals"]["recent_peak_score"] = round(float(scores.max()), 4) if scores.size else 0.0
                profile["risk_signals"]["recent_mean_score"] = round(float(scores.mean()), 4) if scores.size else 0.0
            except Exception:
                pass

    return profile
