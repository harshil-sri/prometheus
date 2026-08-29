"""Tests for the SSE fan-out hub (updates.md 5 / Phase 5).

No pytest-asyncio dependency: each test drives its own event loop via
asyncio.run(), so the hub binds the loop on first subscribe just as it does
under uvicorn.
"""

import asyncio
import json
import sys
import threading
from unittest.mock import patch
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from api.events import EventHub, event_to_sse  # noqa: E402


def _file_path():
    return Path(__file__)


@pytest.fixture
def hub():
    return EventHub(maxsize=8)


def _frames(frames):
    return [json.loads(f[6:]) for f in frames]


async def _drain(queue, n):
    got = []
    for _ in range(n):
        got.append(await asyncio.wait_for(queue.get(), timeout=2))
    return got


def test_fan_in_single_subscriber(hub):
    """Events publish to a subscriber in exactly the order they were sent."""
    async def scenario():
        sub = await hub.subscribe()
        for i in range(5):
            hub.publish({"type": "step", "step": i + 1})
        got = await _drain(sub, 5)
        return _frames(got)

    assert [e["step"] for e in asyncio.run(scenario())] == [1, 2, 3, 4, 5]


def test_fan_out_every_subscriber(hub):
    """All subscribers receive the same events (one producer, N viewers)."""
    async def scenario():
        subs = [await hub.subscribe() for _ in range(3)]
        hub.publish({"type": "step", "step": 1})
        hub.publish({"type": "step", "step": 2})
        steps = []
        for sub in subs:
            frames = _frames(await _drain(sub, 2))
            steps.append([e["step"] for e in frames])
        return steps, hub.subscriber_count()

    steps, count = asyncio.run(scenario())
    assert steps == [[1, 2]] * 3
    assert count == 3


def test_late_joiner_snapshot(hub):
    """A subscriber attaching mid-run is seeded with the latest event."""
    async def scenario():
        s1 = await hub.subscribe()
        hub.publish({"type": "step", "step": 7})
        await _drain(s1, 1)
        s2 = await hub.subscribe()
        return None if s2.empty() else json.loads(s2.get_nowait()[6:])

    assert asyncio.run(scenario()) == {"type": "step", "step": 7}


def test_snapshot_cleared(hub):
    """clear_snapshot() removes the retained event (fresh-run seeding)."""
    async def scenario():
        s1 = await hub.subscribe()
        hub.publish({"type": "done"})
        await _drain(s1, 1)
        hub.clear_snapshot()
        s2 = await hub.subscribe()
        return s2.empty()

    assert asyncio.run(scenario())


def test_slow_client_cannot_block_hub(hub):
    """A full subscriber queue drops-oldest instead of stalling delivery."""
    async def scenario():
        s1 = await hub.subscribe()
        s2 = await hub.subscribe()
        total = hub._maxsize + 6
        for i in range(total):  # all published with NO draining: no block
            hub.publish({"type": "step", "step": i + 1})
        await asyncio.sleep(0.05)  # let the loop deliver all publishes
        s1_steps = []
        while not s1.empty():
            s1_steps.append(json.loads(s1.get_nowait()[6:])["step"])
        return s1_steps

    s1_steps = asyncio.run(scenario())
    last_max = list(range(hub._maxsize + 7 - hub._maxsize, hub._maxsize + 7))
    assert s1_steps == last_max  # oldest dropped, newest retained, in order


def test_unsubscribe_stops_delivery(hub):
    async def scenario():
        s1 = await hub.subscribe()
        s2 = await hub.subscribe()
        await hub.unsubscribe(s1)
        hub.publish({"type": "step", "step": 1})
        await asyncio.sleep(0.05)  # let the loop deliver
        return s1.empty(), json.loads(s2.get_nowait()[6:])

    s1_empty, s2_event = asyncio.run(scenario())
    assert s1_empty
    assert s2_event == {"type": "step", "step": 1}


def test_threadsafe_publish_from_worker(hub):
    """publish() from a non-loop thread is delivered on the loop."""
    async def scenario():
        sub = await hub.subscribe()
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            hub.publish({"type": "status", "transactions": 42})

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        barrier.wait()
        frame = await asyncio.wait_for(sub.get(), timeout=2)
        t.join(timeout=2)
        return json.loads(frame[6:])

    assert asyncio.run(scenario()) == {"type": "status", "transactions": 42}


def test_pre_bind_publish_seeds_first_subscriber(hub):
    """publish() before anyone subscribes (threadpool init) seeds joiners."""
    async def scenario():
        hub.publish({"type": "init", "transactions": 1000})
        s1 = await hub.subscribe()
        return None if s1.empty() else json.loads(s1.get_nowait()[6:])

    assert asyncio.run(scenario()) == {"type": "init", "transactions": 1000}


def test_event_to_sse_frame():
    assert event_to_sse({"type": "done"}) == 'data: {"type": "done"}\n\n'


def test_not_ready_stream_emits_error():
    """Contract: /api/stream before /api/init yields an SSE error frame."""
    import api.main as main

    async def scenario():
        resp = await main.live_stream()
        chunks = [chunk async for chunk in resp.body_iterator]
        return "".join(chunks)

    saved = main.DEMO_STATE.get("ready")
    main.DEMO_STATE["ready"] = False
    try:
        body = asyncio.run(scenario())
    finally:
        main.DEMO_STATE["ready"] = saved
    assert body.startswith('data: {"type": "error"')
    assert '"detail": "not initialized"' in body