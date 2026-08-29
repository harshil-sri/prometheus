"""SSE fan-out hub for the live War-Room stream (updates.md 5 / Phase 5).

Replaces the single self-contained inline generator in /api/stream with an
`asyncio.Queue` fan-out bus:

    * ONE producer simulates the twin and publishes step/done events;
      every subscribed EventSource client receives the SAME run (previously
      each tab ran its own 30-step simulation, racing over the world state
      and `pending_injections`).
    * Publishers are non-blocking: publish() from ANY thread (FastAPI sync
      endpoints run in a threadpool) schedules delivery on the loop thread,
      so a slow HTML stream can never stall an attack/init endpoint.
    * Late joiners immediately get a bounded snapshot of the latest event
      (`_last`), so a post-reload dashboard is not blank until the next step.
    * Bounded subscriber queues with drop-oldest: a stalled client is evicted
      (best-effort live view), it can never back-pressure the hub.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_HEARTBEAT_S = 15.0
_SUB_MAXSIZE = 128
# Keep-alive events the snapshot never needs to carry.
_NON_SNAPSHOT_TYPES = {"error"}
# Event types that represent the latest world status for late joiners.
_SNAPSHOTABLE_TYPES = {"init", "step", "done", "combo", "inject", "status"}


class EventHub:
    """Bounded fan-out pub/sub. Thread-safe publish; queue ops on the loop."""

    def __init__(self, maxsize: int = _SUB_MAXSIZE) -> None:
        self._maxsize = maxsize
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subscribers: set[asyncio.Queue] = set()
        self._last: Optional[Dict[str, Any]] = None
        # publish() may be called from a worker thread before any subscribe has
        # bound the loop; hold the latest event under a lock until first bind.
        self._pre_bind_lock = threading.Lock()
        self._pre_bind_last: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------ #
    # Producer side
    # ------------------------------------------------------------------ #
    def publish(self, event: Dict[str, Any]) -> None:
        """Fire-and-forget broadcast. Safe from any thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            with self._pre_bind_lock:
                if event.get("type") in _SNAPSHOTABLE_TYPES:
                    self._pre_bind_last = event
            return
        try:
            loop.call_soon_threadsafe(self._deliver, event)
        except RuntimeError:
            logger.warning("event loop unavailable; event dropped")

    def heartbeat_interval(self) -> float:
        return _HEARTBEAT_S

    # ------------------------------------------------------------------ #
    # Subscriber side (must be called on the running loop thread)
    # ------------------------------------------------------------------ #
    async def subscribe(self) -> asyncio.Queue:
        """Register a subscriber queue; seeds it with the latest snapshot."""
        queue = asyncio.Queue(maxsize=self._maxsize)
        with self._pre_bind_lock:
            last = self._pre_bind_last
            self._pre_bind_last = None
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        self._subscribers.add(queue)
        if last is not None:
            self._last = last
        if self._last is not None:
            try:
                queue.put_nowait(event_to_sse(self._last))
            except asyncio.QueueFull:
                self._drop_oldest(queue)
                try:
                    queue.put_nowait(event_to_sse(self._last))
                except asyncio.QueueFull:
                    pass
        return queue

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def clear_snapshot(self) -> None:
        """Drop the retained snapshot (called on the loop thread when a fresh
        producer run starts, so a subscriber cannot be seeded with a stale
        \u201cdone\u201d)."""
        self._last = None

    # ------------------------------------------------------------------ #
    # Internals (loop thread only)
    # ------------------------------------------------------------------ #
    def _deliver(self, event: Dict[str, Any]) -> None:
        if event.get("type") in _SNAPSHOTABLE_TYPES:
            self._last = event
        payload = event_to_sse(event)
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                self._drop_oldest(queue)
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    pass
            except Exception:                     # noqa: BLE001
                logger.exception("hub deliver failed")

    @staticmethod
    def _drop_oldest(queue: asyncio.Queue) -> None:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass


def event_to_sse(event: Dict[str, Any]) -> str:
    return "data: " + json.dumps(event, default=str) + "\n\n"