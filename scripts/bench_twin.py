"""
bench_twin.py — Phase 1 performance gate.

Runs the Financial Digital Twin at the gate scale (2000 accounts x 200 steps,
CPU-only, single process) and writes artifacts/twin_perf.json with the measured
wall-clock time. The gate passes when elapsed_seconds <= budget (30s on the
8 GB dev laptop).

Usage (repo root):
    python scripts/bench_twin.py
    python scripts/bench_twin.py --accounts 2000 --steps 200 --budget 30

Exit code 1 if the budget is exceeded.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time

# Script lives in scripts/; bootstrap repo-root + src/ import roots
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
for _p in (ROOT, SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from twin.twin import FinancialDigitalTwin  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Twin performance benchmark")
    parser.add_argument("--accounts", type=int, default=2000)
    parser.add_argument("--merchants", type=int, default=200)
    parser.add_argument("--devices", type=int, default=800)
    parser.add_argument("--ips", type=int, default=200)
    parser.add_argument("--steps", type=int, default=200)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--budget", type=float, default=30.0)
    args = parser.parse_args()

    print(f"[bench] twin {args.accounts} accounts x {args.steps} steps "
          f"(seed={args.seed}, budget={args.budget}s) ...")
    t0 = time.perf_counter()
    twin = FinancialDigitalTwin(
        seed=args.seed,
        num_accounts=args.accounts,
        num_merchants=args.merchants,
        num_devices=args.devices,
        num_ip_blocks=args.ips,
        num_steps=args.steps,
    )
    bootstrap_s = time.perf_counter() - t0

    t1 = time.perf_counter()
    txs = twin.run()
    run_s = time.perf_counter() - t1

    elapsed = time.perf_counter() - t0 + 0.0  # bootstrap + run
    summary = twin.state_summary()

    perf = {
        "num_accounts": args.accounts,
        "num_merchants": args.merchants,
        "num_devices": args.devices,
        "num_ip_blocks": args.ips,
        "num_steps": args.steps,
        "seed": args.seed,
        "tx_count": len(txs),
        "fraud_tx": summary["fraud_transactions"],
        "bootstrap_seconds": round(bootstrap_s, 3),
        "run_seconds": round(run_s, 3),
        "elapsed_seconds": round(elapsed, 3),
        "seconds_per_1k_steps_x_accounts": round(
            elapsed / max(1, (args.steps * args.accounts) / 1000.0), 4),
        "budget_seconds": args.budget,
        "passed": bool(elapsed <= args.budget),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_only": True,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    out_dir = os.path.join(ROOT, "artifacts")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "twin_perf.json")
    with open(out_path, "w") as f:
        json.dump(perf, f, indent=2)

    status = "PASS" if perf["passed"] else "FAIL"
    print(f"[bench] elapsed={perf['elapsed_seconds']}s "
          f"(bootstrap {perf['bootstrap_seconds']}s + run {perf['run_seconds']}s) "
          f"tx={perf['tx_count']} -> {status}")
    print(f"[bench] artifact written: {out_path}")
    return 0 if perf["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
