"""latency.py — Measured P50/P95/P99 latencies for fast vs deep paths.

Feasibility law: numbers are MEASURED on the dev machine with declared
parameters (iterations, warmup, payload sizes) — never claimed. Deep path
includes real delegated agents (signals, OSINT, sanctions, spectral,
narrative-fallback, fitted scorer), so its timing is an honest upper bound
of what judges see, minus live LLM network latency (documented).
"""

from __future__ import annotations

import gc
import platform
import statistics
import time
from typing import Any, Callable, Dict

__all__ = ["measure_latency", "environment_info"]


def measure_latency(fn: Callable[[], Any], *, warmup: int = 3,
                    iterations: int = 20) -> Dict[str, Any]:
    """Wall-clock timing around fn() with deterministic cleanup.

    Returns seconds stats; also returns raw samples for transparency."""
    for _ in range(warmup):
        fn()
    gc.collect()
    samples = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    s = sorted(samples)
    n = len(s)

    def q(p):
        idx = min(n - 1, max(0, int(round(p * (n - 1)))))
        return round(s[idx], 5)

    return {
        "iterations": n,
        "p50": q(0.50),
        "p95": q(0.95),
        "p99": q(0.99),
        "mean": round(statistics.fmean(s), 5),
        "min": round(s[0], 5),
        "max": round(s[-1], 5),
        "samples_s": [round(x, 5) for x in s],
    }


def environment_info() -> Dict[str, Any]:
    import multiprocessing
    return {
        "python": platform.python_version(),
        "os": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": multiprocessing.cpu_count(),
        "cpu_only": True,
        "clock": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
