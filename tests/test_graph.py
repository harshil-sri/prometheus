from __future__ import annotations

import sys
from pathlib import Path
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest
from fastapi.testclient import TestClient

from twin.twin import FinancialDigitalTwin
from attack.compiler import AttackCompiler
from attack.benchmark_attacks import generate_training_attacks
from blue.ensemble import BlueTeamEnsemble
from api.graph import build_knowledge_graph, list_trajectories_summary, extract_node_profile
from api.main import app, DEMO_STATE


@pytest.fixture(scope="module")
def small_twin():
    twin = FinancialDigitalTwin(seed=42, num_accounts=50, num_merchants=10, num_devices=20, num_steps=6)
    twin.run()
    compiler = AttackCompiler(twin, seed=42)
    generate_training_attacks(compiler, twin.world)
    return twin


def test_build_knowledge_graph_structure(small_twin):
    graph = build_knowledge_graph(small_twin.world, filter_type="overview")
    assert "nodes" in graph
    assert "links" in graph
    assert "stats" in graph

    nodes = graph["nodes"]
    links = graph["links"]
    assert len(nodes) > 0
    assert len(links) > 0

    # Node structure
    node_types = set()
    for n in nodes:
        assert "id" in n
        assert "type" in n
        assert "risk_score" in n
        assert "properties" in n
        node_types.add(n["type"])

    assert "account" in node_types
    assert "customer" in node_types or "merchant" in node_types

    # Link structure
    for l in links:
        assert "source" in l
        assert "target" in l
        assert "type" in l


def test_knowledge_graph_filters(small_twin):
    # Fraud filter
    fraud_graph = build_knowledge_graph(small_twin.world, filter_type="fraud")
    assert all(l.get("is_fraud") for l in fraud_graph["links"] if l["type"] == "TRANSACTION")

    # Trajectory filter
    trajectories = list_trajectories_summary(small_twin.world)
    assert len(trajectories) > 0
    target_traj = trajectories[0]["trajectory_id"]

    traj_graph = build_knowledge_graph(small_twin.world, filter_type="trajectory", trajectory_id=target_traj)
    for l in traj_graph["links"]:
        if l["type"] == "TRANSACTION":
            assert l.get("trajectory_id") == target_traj


def test_extract_node_profile(small_twin):
    accounts = list(small_twin.world.accounts.keys())
    assert accounts
    prof = extract_node_profile(small_twin.world, accounts[0])
    assert prof["node_id"] == accounts[0]
    assert prof["type"] == "account"
    assert "balance" in prof["details"]


def test_api_graph_endpoints():
    client = TestClient(app)
    # Init twin
    init_res = client.post("/api/init", json={"seed": 42, "num_accounts": 60, "num_steps": 10})
    assert init_res.status_code == 200
    assert init_res.json()["status"] == "ok"

    # Graph endpoint
    graph_res = client.get("/api/graph?filter=overview")
    assert graph_res.status_code == 200
    graph_data = graph_res.json()
    assert "nodes" in graph_data
    assert "links" in graph_data

    # Trajectories endpoint
    traj_res = client.get("/api/graph/trajectories")
    assert traj_res.status_code == 200
    assert "trajectories" in traj_res.json()

    # Node profile endpoint
    sample_node = graph_data["nodes"][0]["id"]
    node_res = client.get(f"/api/graph/node/{sample_node}")
    assert node_res.status_code == 200
    assert node_res.json()["node_id"] == sample_node

    # Investigate endpoint
    inv_res = client.post("/api/investigate", json={"case_id": "CASE_TEST", "tx_ids": []})
    assert inv_res.status_code == 200
    assert inv_res.json()["case_id"] == "CASE_TEST"
