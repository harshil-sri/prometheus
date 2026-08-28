# Project Prometheus

**Closed-Loop Fraud Detection System** — Floor (F1–F8)

A stateful Financial Digital Twin generates synthetic transactions with 8 AMLSim-verified fraud typologies. A rule-based Attack Compiler produces 6 benchmark attack types (2 held out for generalization testing). The Blue Team (XGBoost + GNN + meta-model) detects fraud, and a Sensitivity Engine diagnoses misses to drive weakness-directed retraining (max 2 rounds). A FastAPI dashboard demonstrates the 3-beat loop: Red wins → retrain → Blue wins → held-out generalization.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run the integration test
cd src && PYTHONPATH=. python3 -c "
from twin.twin import FinancialDigitalTwin
from attack.compiler import AttackCompiler
from attack.benchmark_attacks import BENCHMARK_ATTACKS, generate_training_attacks
from blue.features import compute_features, build_graph_data
from blue.xgb_model import XGBFraudDetector
from blue.gnn_model import GNNFraudDetector
from blue.meta_model import MetaModel
from sensitivity.engine import SensitivityEngine
from scoring.structured_score import compute_structured_score
from eval.harness import full_report
import numpy as np

# Initialize twin
t = FinancialDigitalTwin(seed=42, num_accounts=2000, num_merchants=100, num_steps=200)
t.run()
print(f'Twin: {len(t.world.transactions)} TXs')

# Train Blue Team
X, y, fnames = compute_features(t.world.transactions, t.world)
xgb = XGBFraudDetector(seed=42); xgb.fit(X, y, fnames)
data = build_graph_data(t.world.transactions, t.world)
gnn = GNNFraudDetector(in_channels=data.x.shape[1], seed=42); gnn.fit(data)
print(f'Blue Team trained: {X.shape[0]} samples, {data.x.shape[0]} graph nodes')

# Evaluate
scores = xgb.predict_proba(X)
report = full_report(y, scores)
print(f'PR-AUC: {report[\"overall\"][\"pr_auc\"]:.4f}')
print('Done!')
"

# Start the dashboard
cd src/api && PYTHONPATH=.. python3 main.py
# Open http://localhost:8000
```

## Project Structure

```
src/
├── twin/           # Financial Digital Twin (T1)
│   ├── core.py     # Entity schemas + WorldState
│   ├── normal_behavior.py  # Normal transaction generator
│   ├── typologies.py       # 8 AMLSim typologies
│   └── twin.py     # FinancialDigitalTwin orchestrator
├── attack/         # Attack Compiler (T2)
│   ├── compiler.py # Rule-based planner
│   ├── spec.py     # Attack spec schema
│   └── benchmark_attacks.py  # 6 attack types (A2/A5 held out)
├── blue/           # Blue Team (T3)
│   ├── features.py # Feature engineering
│   ├── xgb_model.py    # XGBoost detector
│   ├── gnn_model.py    # 2-layer SAGEConv GNN
│   ├── meta_model.py   # Logistic blend
│   └── calibrate.py    # Platt/isotonic calibration
├── sensitivity/    # Sensitivity Engine (T4)
│   ├── engine.py       # Shared engine (3 consumers)
│   ├── shap_explainer.py  # SHAP for XGBoost
│   └── gnn_ablation.py    # Masked-neighbor ablation
├── feedback/       # Feedback Loop (T5)
│   ├── loop.py     # Weakness-directed retrain (≤2 rounds)
│   └── report.py   # Blind-Spot Report
├── scoring/        # Structured Score (T6)
│   └── structured_score.py  # 0-1000 Mastercard bands
├── eval/           # Evaluation Harness (T8)
│   └── harness.py  # Multi-prevalence PR-AUC, cost model
├── api/            # Dashboard Backend (T7)
│   ├── main.py     # FastAPI server
│   └── graph.py    # Multi-relational Knowledge Graph engine
└── dashboard/      # Dashboard Frontend (T7)
    └── index.html  # Single-page War-Room UI + Graph Canvas
```

## Key Design Decisions

- **Knowledge Graph Explorer**: Dynamic extraction of multi-relational graphs (accounts, customers, merchants, devices, IPs, transactions) with GNN risk overlay and sub-graph trajectory filtering without hardcoded constants
- **Held-out attacks**: A2 (synthetic identity) and A5 (scatter_gather layering) are locked before training — enforced in code with `assert_no_held_out_leakage()`
- **Max 2 retrain rounds**: Hard-capped in `feedback/loop.py` to prevent overfitting to generator quirks
- **Structured score is deep-path only**: Fast path stays a pure ML probability. Never mixed.
- **Deterministic**: Every component seedable via `random.Random(seed)`
- **Cold-start**: Accounts with no history produce sane default features (no NaNs)
- **Isolated nodes**: GNN handles nodes with no edges via self-loop normalization

## Demo

The 3-beat demo runs via the dashboard:
1. **Beat 1**: Red Team attacks → Blue Team misses (~40-60% recall on held-out types)
2. **Beat 2**: Sensitivity Engine diagnoses weakness → Attack Compiler generates targeted variants → retrain → Blue Team catches it
3. **Beat 3**: Held-out attack types (A2, A5) run against retrained Blue Team → generalization demonstrated

## Graded Artifacts

1. **Runnable GitHub repo** — this repository
2. **.docx walkthrough** — `Prometheus_Walkthrough.docx` (covers novel attacks, generation/simulation, detection model + efficacy, real-world feasibility)
3. **Working web prototype** — `http://localhost:8000` (FastAPI + single-page HTML/JS)