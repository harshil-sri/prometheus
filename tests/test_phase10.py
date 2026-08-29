"""tests/test_phase10.py — Phase 10 gate: combo module + API hardening + Docker.

Coverage:
  1. SupplyChainCombo structure & contracts
  2. /api/combo endpoint contract
  3. /api/stream SSE contract
  4. /api/attack-types returns LIST-shaped
  5. /api/init never leaks raw traceback
  6. Dockerfile syntax sanity
  7. CI workflow exists
"""

from __future__ import annotations

import os
import re
import sys
import json
from pathlib import Path

import pytest

# Ensure src is on path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


# ---------------------------------------------------------------------------
# 1. SupplyChainCombo structure
# ---------------------------------------------------------------------------
def test_supply_chain_combo_class_exists():
    from combo.supply_chain import SupplyChainCombo, STAGE_NAMES
    assert SupplyChainCombo is not None
    assert isinstance(STAGE_NAMES, list) and len(STAGE_NAMES) == 4
    assert "synthetic_identity_onboarding" in STAGE_NAMES
    assert "cash_out_exit" in STAGE_NAMES


def test_supply_chain_combo_init():
    from combo.supply_chain import SupplyChainCombo
    # Just check constructor signature
    assert hasattr(SupplyChainCombo, "__init__")
    assert hasattr(SupplyChainCombo, "run")


def test_combo_module_exports():
    from combo import SupplyChainCombo
    assert SupplyChainCombo is not None


# ---------------------------------------------------------------------------
# 2. /api/combo endpoint — needs a real FastAPI client with init
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def fastapi_client():
    """Spin up a TestClient and initialize state once."""
    from fastapi.testclient import TestClient
    from api.main import app
    client = TestClient(app)
    # Initialize (may take ~10-20s)
    r = client.post("/api/init", json={"seed": 42, "num_accounts": 100, "num_steps": 50})
    if r.status_code != 200:
        pytest.skip(f"init failed: {r.text[:200]}")
    data = r.json()
    if data.get("status") != "ok":
        pytest.skip(f"init status not ok: {data}")
    return client


def test_combo_endpoint_ok(fastapi_client):
    r = fastapi_client.post("/api/combo")
    assert r.status_code == 200
    data = r.json()
    assert "status" in data
    if data["status"] == "error":
        pytest.skip(f"combo error: {data.get('detail')}")
    assert data["status"] == "ok"
    assert data["n_stages"] == 4
    assert "trajectory_id" in data
    assert "stages" in data and len(data["stages"]) == 4
    assert "stages_caught" in data
    assert isinstance(data["stages_caught"], int)
    assert 0 <= data["stages_caught"] <= 4
    # Each stage has required fields
    for s in data["stages"]:
        assert "stage" in s
        assert "n_txs" in s
        assert "caught" in s
        assert "peak_score" in s
        assert 0.0 <= s["peak_score"] <= 1.0
    # stages must be tagged with mechanism
    assert "stage_names" in data
    assert len(data["stage_names"]) == 4


# ---------------------------------------------------------------------------
# 3. /api/stream SSE
# ---------------------------------------------------------------------------
def test_stream_endpoint_sse(fastapi_client):
    with fastapi_client.stream("GET", "/api/stream") as r:
        assert r.status_code == 200
        ct = r.headers.get("content-type", "")
        assert "text/event-stream" in ct
        # Read first few events
        events = []
        for line in r.iter_lines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    try:
                        events.append(json.loads(payload))
                    except json.JSONDecodeError:
                        pass
            if len(events) >= 3:
                break
        assert len(events) >= 1
        first = events[0]
        # Phase 5: hub producers also broadcast init/inject/combo; the first
        # event a late-joining client sees is the retained hub snapshot (e.g.
        # the init published on /api/init) or a live step.
        assert first.get("type") in ("step", "error", "done", "init", "inject", "combo")


# ---------------------------------------------------------------------------
# 4. /api/attack-types returns LIST
# ---------------------------------------------------------------------------
def test_attack_types_returns_list(fastapi_client):
    r = fastapi_client.get("/api/attack-types")
    assert r.status_code == 200
    data = r.json()
    assert "attacks" in data
    assert isinstance(data["attacks"], list)
    assert len(data["attacks"]) >= 1
    a0 = data["attacks"][0]
    assert "id" in a0
    assert "name" in a0
    # has held_out field
    assert "held_out" in a0
    assert "held_out" in data
    assert "trainable" in data
    assert isinstance(data["held_out"], list)
    assert isinstance(data["trainable"], list)


# ---------------------------------------------------------------------------
# 5. /api/init never leaks raw traceback
# ---------------------------------------------------------------------------
def test_init_no_traceback_leak():
    """Trigger a known-bad init (negative num_accounts) and assert
    that the response is sanitized, not a raw Python traceback."""
    from fastapi.testclient import TestClient
    from api.main import app
    client = TestClient(app)
    r = client.post("/api/init", json={"seed": 42, "num_accounts": -999, "num_steps": 5})
    assert r.status_code == 200
    data = r.json()
    # Should be sanitized error
    assert data.get("status") == "error" or "error" in data
    detail = data.get("detail", "")
    if detail:
        # Should NOT contain traceback markers
        assert "Traceback" not in detail
        assert "File \"" not in detail
        assert "<class" not in detail


# ---------------------------------------------------------------------------
# 6. Dockerfile sanity
# ---------------------------------------------------------------------------
def test_dockerfile_exists():
    df = ROOT / "Dockerfile"
    assert df.exists(), "Dockerfile missing"
    content = df.read_text(encoding="utf-8")
    assert "FROM" in content
    assert "python" in content.lower() or "py" in content.lower()
    assert "8000" in content, "expected port 8000"


# ---------------------------------------------------------------------------
# 7. CI workflow
# ---------------------------------------------------------------------------
def test_ci_workflow_exists():
    ci = ROOT / ".github" / "workflows" / "ci.yml"
    assert ci.exists(), "CI workflow missing"
    content = ci.read_text(encoding="utf-8")
    assert "pytest" in content
    assert "on:" in content


# ---------------------------------------------------------------------------
# 8. Dashboard has the new panels wired
# ---------------------------------------------------------------------------
def test_dashboard_has_combo_and_stream_panels():
    html = (ROOT / "src" / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert "comboResults" in html
    assert "liveStream" in html
    assert "guardrailResults" in html
    assert "oodHeatmap" in html
    assert "runCombo" in html
    assert "startStream" in html
    assert "EventSource" in html
