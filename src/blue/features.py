"""
features.py - Feature engineering for the fraud detection pipeline.

Consumes the Digital Twin transaction log format. Each transaction is a dict:

    {
        'tx_id':         str,
        'step':          int,      # simulation timestep (hour granularity)
        'from':          str,      # sender account id
        'to':            str,      # recipient account / merchant id
        'amount':        float,
        'currency':      str,
        'category':      str,      # merchant category
        'device':        str,
        'ip':            str,
        'is_fraud':      bool,
        'attack_id':     str|None,
        'trajectory_id': str,
    }

Public API:
    compute_features(transactions, world_state=None) -> (X, y, feature_names)
    build_graph_data(transactions, world_state=None) -> torch_geometric Data | None
    get_node_features(account_id, world_state=None)  -> list[float] (cold-start)
"""

import numpy as np
import torch
from collections import defaultdict

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CATEGORIES = [
    'retail', 'grocery', 'dining', 'travel', 'utilities',
    'subscription', 'health', 'education', 'entertainment', 'p2p',
]
CAT_MAP = {c: i for i, c in enumerate(CATEGORIES)}

CURRENCIES = ['USD', 'EUR', 'GBP', 'JPY']
CUR_MAP = {c: i for i, c in enumerate(CURRENCIES)}

NIGHT_HOURS = frozenset(range(0, 6))

HIGH_AMOUNT_THRESHOLD = 50000.0
AMOUNT_SCALE = 100000.0
AMOUNT_CAP = 10.0

VELOCITY_SHORT = 10   # steps
VELOCITY_LONG = 50    # steps

FEATURE_NAMES = [
    'amount',
    'log_amount',
    'amount_roundness',
    'is_high_amount',
    'velocity_10',
    'velocity_50',
    'sender_tx_count',
    'sender_avg_amount',
    'sender_amount_zscore',
    'time_since_last_tx',
    'repeat_recipient_50',
    'is_new_device',
    'device_account_count',
    'ip_account_count',
    'merchant_category',
    'hour_of_day',
    'is_night',
    'is_p2p',
    'is_external',
    'currency_code',
]
N_FEATURES = len(FEATURE_NAMES)

NODE_FEATURE_NAMES = [
    'node_total_degree',
    'node_out_degree',
    'node_in_degree',
    'node_total_sent',
    'node_avg_amount',
    'node_is_account',
    'node_is_merchant',
]
NODE_FEATURE_DIM = len(NODE_FEATURE_NAMES)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_internal(node_id):
    """External entities carry an EXT_ prefix in the twin."""
    return 'EXT_' not in str(node_id)


def _is_account_like(node_id):
    s = str(node_id)
    return ('ACC' in s) or ('CUST' in s)


def _is_merchant_like(node_id):
    return 'MERCHANT' in str(node_id)


def _seed_histories(world_state):
    """
    Optionally seed known device/IP bindings from the WorldState so that
    is_new_device reflects history predating the transaction log.
    Returns (known_devices, known_ips): acct -> set of identifiers.
    """
    known_devices, known_ips = {}, {}
    if world_state is None:
        return known_devices, known_ips

    accounts = getattr(world_state, 'accounts', None)
    if accounts is None and isinstance(world_state, dict):
        accounts = world_state.get('accounts')

    if isinstance(accounts, dict):
        for acct, info in accounts.items():
            info = info if isinstance(info, dict) else {}
            devs = info.get('devices') or []
            addrs = info.get('ips') or []
            if devs:
                known_devices[str(acct)] = {str(d) for d in devs}
            if addrs:
                known_ips[str(acct)] = {str(a) for a in addrs}

    return known_devices, known_ips


# ---------------------------------------------------------------------------
# Tabular features
# ---------------------------------------------------------------------------

def compute_features(transactions, world_state=None):
    '''
    Compute per-transaction feature vectors from a list of transaction dicts.

    Args:
        transactions: list of dicts (Digital Twin tx log format)
        world_state: optional WorldState for seeding device/IP history

    Returns:
        X: np.ndarray of shape (n, N_FEATURES), float32
        y: np.ndarray of shape (n,) - float labels (1.0 = fraud)
        feature_names: list of str
    '''
    n = len(transactions)
    if n == 0:
        return (
            np.zeros((0, N_FEATURES), dtype=np.float32),
            np.zeros((0,), dtype=np.float32),
            list(FEATURE_NAMES),
        )

    # ---- Sender histories: acct -> sorted [(step, idx, amount, to)] ----
    sender_txs = defaultdict(list)
    for idx, tx in enumerate(transactions):
        sender_txs[str(tx['from'])].append(
            (int(tx.get('step', 0)), idx, float(tx.get('amount', 0.0)),
             str(tx.get('to')) if tx.get('to') is not None else '')
        )
    for k in sender_txs:
        sender_txs[k].sort(key=lambda h: h[0])

    # ---- Streaming state over device / IP sharing ----
    known_devices, known_ips = _seed_histories(world_state)
    device_accounts = defaultdict(set)
    ip_accounts = defaultdict(set)
    for acct, devs in known_devices.items():
        device_accounts[acct].update(devs)
    for acct, addrs in known_ips.items():
        ip_accounts[acct].update(addrs)

    # Process in step order so device/IP state respects causality,
    # but write results back to original positions.
    order = sorted(range(n), key=lambda i: int(transactions[i].get('step', 0)))

    X = np.zeros((n, N_FEATURES), dtype=np.float32)
    y = np.zeros((n,), dtype=np.float32)

    for idx in order:
        tx = transactions[idx]
        from_id = str(tx['from'])
        to_raw = tx.get('to')
        to_id = str(to_raw) if to_raw is not None else ''
        amount = float(tx.get('amount', 0.0) or 0.0)
        step = int(tx.get('step', 0))
        device = tx.get('device')
        ip = tx.get('ip')
        category = tx.get('category') or 'retail'
        currency = str(tx.get('currency') or 'USD')

        f = []

        # --- Amount features -------------------------------------------
        f.append(amount)                                        # 1
        f.append(float(np.log1p(max(amount, 0.0))))             # 2
        f.append(1.0 if amount >= 1000 and amount % 1000 == 0 else 0.0)  # 3
        f.append(1.0 if amount > HIGH_AMOUNT_THRESHOLD else 0.0)         # 4

        # --- Sender velocity / behavioral history -----------------------
        past = [h for h in sender_txs[from_id] if h[0] < step]
        vel_10 = sum(1 for h in past if step - h[0] <= VELOCITY_SHORT)
        vel_50 = sum(1 for h in past if step - h[0] <= VELOCITY_LONG)
        f.append(float(vel_10))                                 # 5
        f.append(float(vel_50))                                 # 6
        f.append(float(len(past)))                              # 7

        if past:
            amounts = np.array([h[2] for h in past], dtype=np.float64)
            mean_amt = float(amounts.mean())
            std_amt = float(amounts.std())
            z = (amount - mean_amt) / std_amt if std_amt > 1e-9 else 0.0
            f.append(mean_amt)                                  # 8
            f.append(float(np.clip(z, -10.0, 10.0)))            # 9
            f.append(float(step - past[-1][0]))                 # 10
        else:
            f.append(0.0)                                       # 8
            f.append(0.0)                                       # 9
            f.append(-1.0)                                      # 10 (no history)

        # Repeat recipient within the long window (mule / laundering hint)
        rep = sum(
            1 for h in past
            if h[3] == to_id and step - h[0] <= VELOCITY_LONG
        )
        f.append(float(rep))                                    # 11

        # --- Device / IP reputation -------------------------------------
        if device:
            dkey = str(device)
            seen = device_accounts.get(from_id, set())
            f.append(1.0 if dkey not in seen else 0.0)          # 12
            device_accounts[from_id].add(dkey)
            f.append(float(max(len(device_accounts[dkey]) - 1, 0)))  # 13
        else:
            f.append(0.0)                                       # 12
            f.append(0.0)                                       # 13

        if ip:
            ikey = str(ip)
            ip_accounts[from_id].add(ikey)
            f.append(float(max(len(ip_accounts[ikey]) - 1, 0))) # 14
        else:
            f.append(0.0)                                       # 14

        # --- Temporal / contextual --------------------------------------
        f.append(float(CAT_MAP.get(category, 0)))               # 15
        hour = step % 24
        f.append(float(hour))                                   # 16
        f.append(1.0 if hour in NIGHT_HOURS else 0.0)           # 17

        # --- Topology ----------------------------------------------------
        f.append(1.0 if to_id and _is_account_like(to_id) else 0.0)       # 18
        f.append(0.0 if (_is_internal(from_id) and _is_internal(to_id))
                 else 1.0)                                                # 19

        # --- Currency ------------------------------------------------------
        f.append(float(CUR_MAP.get(currency, len(CURRENCIES))))  # 20

        X[idx, :] = f
        y[idx] = 1.0 if tx.get('is_fraud') else 0.0

    X = np.nan_to_num(X, nan=0.0, posinf=1e6, neginf=-1e6)
    return X, y, list(FEATURE_NAMES)


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_graph_data(transactions, world_state=None):
    '''
    Build a PyTorch Geometric Data object from transactions.

    Nodes: internal accounts + merchants (EXT_ entities excluded).
    Edges: transactions (bidirectional) + self-loops fallback.
    Edge attrs: [scaled_amount, normalized_hour, repeat_pair_count].

    NOTE: labels (is_fraud) are deliberately excluded from all graph
    attributes to prevent label leakage into the GNN.

    Returns torch_geometric.data.Data or None if unavailable/empty.
    '''
    try:
        from torch_geometric.data import Data
    except ImportError:
        return None

    if not transactions:
        return None

    # ---- Collect unique internal node ids ----
    node_set = set()
    for tx in transactions:
        node_set.add(str(tx['from']))
        if tx.get('to') is not None:
            node_set.add(str(tx['to']))

    internal_nodes = sorted(nid for nid in node_set if _is_internal(nid))
    if not internal_nodes:
        return None

    node_to_idx = {nid: i for i, nid in enumerate(internal_nodes)}
    n_nodes = len(internal_nodes)

    # ---- Edges from transactions (sorted for deterministic repeat counts) --
    edge_list = []
    edge_attr_list = []
    pair_counts = defaultdict(int)

    for tx in sorted(transactions, key=lambda t: int(t.get('step', 0))):
        f_id = str(tx['from'])
        t_raw = tx.get('to')
        t_id = str(t_raw) if t_raw is not None else ''
        if f_id in node_to_idx and t_id in node_to_idx:
            u, v = node_to_idx[f_id], node_to_idx[t_id]
            pair_counts[(u, v)] += 1
            amt = float(np.clip(
                float(tx.get('amount', 0.0)) / AMOUNT_SCALE, 0.0, AMOUNT_CAP
            ))
            hour = (int(tx.get('step', 0)) % 24) / 24.0
            repeat = float(pair_counts[(u, v)] - 1)
            attr = [amt, hour, repeat]
            edge_list.append([u, v])
            edge_attr_list.append(attr)
            # Reverse edge so message passing is not direction-limited
            edge_list.append([v, u])
            edge_attr_list.append(attr)

    if not edge_list:
        # Fully isolated nodes: fall back to self-loops
        edge_list = [[i, i] for i in range(n_nodes)]
        edge_attr_list = [[0.0, 0.0, 0.0] for _ in range(n_nodes)]

    edge_index = torch.tensor(edge_list, dtype=torch.long).t().contiguous()
    edge_attr = torch.tensor(edge_attr_list, dtype=torch.float)

    # ---- Aggregated node features ----
    out_deg = defaultdict(int)
    in_deg = defaultdict(int)
    sent_sum = defaultdict(float)
    for tx in transactions:
        f_id = str(tx['from'])
        t_raw = tx.get('to')
        t_id = str(t_raw) if t_raw is not None else ''
        out_deg[f_id] += 1
        sent_sum[f_id] += float(tx.get('amount', 0.0) or 0.0)
        if t_id:
            in_deg[t_id] += 1

    x_rows = []
    for nid in internal_nodes:
        deg = out_deg.get(nid, 0) + in_deg.get(nid, 0)
        deg = deg if deg > 0 else 1  # avoid log(0)
        x_rows.append([
            deg / 100.0,
            out_deg.get(nid, 0) / 100.0,
            min(sent_sum.get(nid, 0.0) / 1000000.0, 10.0),
            1.0 if 'ACC' in str(nid) else 0.0,
            1.0 if 'MERCHANT' in str(nid) else 0.0,
        ])

    x = torch.tensor(x_rows, dtype=torch.float)

    # ---- Node labels (1 if any fraud TX involves this node) ----
    node_fraud = defaultdict(bool)
    for tx in transactions:
        if tx.get('is_fraud'):
            f = str(tx['from'])
            t = str(tx.get('to', ''))
            if f in node_to_idx:
                node_fraud[f] = True
            if t in node_to_idx:
                node_fraud[t] = True

    y_rows = [1.0 if node_fraud.get(nid, False) else 0.0 for nid in internal_nodes]
    y = torch.tensor(y_rows, dtype=torch.float)

    return Data(x=x, edge_index=edge_index, edge_attr=edge_attr, y=y)


def get_node_features(account_id, world_state):
    """Return default node features for an account. Handle cold-start."""
    return [0.0, 0.0, 0.0, 1.0, 0.0]