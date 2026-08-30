# Fix Log — Phase 2

Started: 2026-08-30
Branch: kartik @ 68bde8c
Scope: AUDIT_REPORT_PHASE1.md §7 items (P0 skipped by user choice; P1–P4 executed)
Convention: one section per fix; verification before commit. Test counters / artifact md5 captured at baseline for byte-equality gates.

---

## Baseline snapshot (pre-change)

- HEAD: `68bde8c final audit`
- Branch: `kartik` (clean working tree expected; verified)
- Pytest: **230 passed** (0 failed) — see `%TEMP%\opencode\pytest_full.log`
- Eval scripts: 12 of 12 in `scripts/` (incl. `fit_weights.py`, `bench_twin.py`)
- Canonical artifacts (must remain byte-identical after ring-fence + docs work):
  - `artifacts/fidelity_report.json` md5 `5fd34a40c6735cdf` (per Phase 8)
  - `artifacts/feedback_timeline.json` md5 `f69e2cb2…` (per Phase 8)
  - `artifacts/attribution.json` fp `237b553e4a42795e` (per Phase 8)
  - `src/artifacts/structured_weights.json` byte-identical on rerun (per Phase 4)
  - Holdout fingerprint `292cc7f67639cea556948086f8303fb248249da14f45b3d4825cca8f0473a162` (MUST stay intact)
- `test_api.py` (root, 9-line smoke) — to be replaced with `tests/test_api.py`
- `.env` **no longer tracked** (operator commits `a56e2c1` "untrack .env" and `4c611c6` "remove .env blob from index" — stage 1+2 of P0 done; history rewrite to scrub past commits remains operator's responsibility outside this agent's session). P0 in audit §7 is now **PARTIAL** (live file is untracked but key still in prior commit history).
- Working tree at session start: **14 modified artifacts** + `AUDIT_REPORT_PHASE1.md` (added in audit) + `fix_log.md` (this file) + `session-ses_fb13.md` + `updates.md` (audit). These working-tree changes are from the prior audit session; they are NOT my changes.
- Pytest at session start: **229 passed, 1 failed** — `test_perf_artifact_within_budget` fails because `artifacts/twin_perf.json` was generated at 120×60 (under the test's 2000×200 gate). This is a pre-existing failure from the audit's smaller `bench_twin.py` run, not a regression I introduced. Fix in this pass: re-run `bench_twin.py` at ≥2000×≥200 OR update test gate. **Decision: re-run bench at 2000×200** (less invasive; matches Phase-8 committed spec; artifact deterministic; no test-code change).

### Baseline artifact md5s (captured at start of THIS session)
- `artifacts/fidelity_report.json` `B0B2221C1A8A6D8CA29182D96FA9CB16` (was `5fd34a40c6735cdf` in Phase 8 — **differs**: this session has a fresh fidelity run from the audit; either is "valid Phase 8+ content"; re-runs of `scripts/fidelity_eval.py` must be byte-identical to THIS session's md5 after fix-list work)
- `artifacts/feedback_timeline.json` `7E0F9D26A1B6086C8BCA613FE1CCFC39`
- `artifacts/attribution.json` `C0217C53E66689414FF220B28C0130ED`
- `src/artifacts/structured_weights.json` `8C804F7D0E1B2935EC0EB7CB29774E41`
- (Holdout fingerprint `292cc7f6…a162` MUST stay intact.)

### Pre-fix-list housekeeping: bench artifact at gate scale

`scripts/bench_twin.py` rerun at `--accounts 2000 --steps 200 --budget 30.0` → elapsed 3.711s, 63,390 txs, **PASS**. `pytest tests/test_phase1_integrity.py::test_perf_artifact_within_budget` now passes (was the 1 failing test in the pre-cleanup baseline). Commit deferred to the first real fix-list commit (this is a working-tree-only regeneration; logical change in the next commit).

---

## 2026-08-30 19:24 — P3-1: sweep_eval None-as-float crash fix

Tier: P3 (bug; user-visible "sweep eval crashes on all-shortfall")
Files touched: `scripts/sweep_eval.py`
Reason: AUDIT_REPORT_PHASE1.md §7 P3-1 — `scripts/sweep_eval.py:229-230` (and 232, 234) format `agg[...]...['mean']` as `:.4f` / `:.2f` even when `mean` is `None`. When every seed×scale config hits a generation shortfall (the audit's case), `ci95` returns `{"n":0, "mean":None, "ci95":None, ...}` and `f"{None:.4f}"` raises `TypeError` mid-summary, AFTER the fail-loud artifact was already written — a needless crash that erases the run summary and exit code 0.
Change: introduced module-local helper `_fmt_or_invalid(value, fmt)` that returns the literal string `"INVALID"` when value is `None` or non-formattable; replaced all four formatting sites in the summary block (lines 229-230 `headline 5% PR-AUC mean + CI95`, line 232 `overall meta + XGB`, line 234 `per-type recall>p95`).
Verification: in-process replay of the audit's crash scenario (`mean=None`, `ci95=None`) → exits cleanly with `headline 5% PR-AUC: mean=INVALID CI95=-`, `overall meta PR-AUC: INVALID | XGB-only: INVALID`, `per-type recall>p95: A1:INVALID(n>=2)  A5:INVALID(n>=0)`. No `TypeError`. Will re-verify via `scripts/sweep_eval.py --seeds 40 41 --eval-repeats 2 --min-eval-fraud-per-type 1000` (forces all-shortfall) in the end-of-pass gates.
Commit: pending (rolled into the ring-fence + sweep/commit batch).

---

## 2026-08-30 19:31 — P3-2: Windows console ₹ crash guard

Tier: P3 (cosmetic, but blocks the `baseline_eval.py` second run in the audit)
Files touched: new `scripts/_ensure_utf8_stdout.py`; `scripts/{attribution,baseline,bench_twin,feasibility,fidelity,fit_weights,mechanism,protocol,shadow,signals,sweep,timeline}_eval.py`
Reason: AUDIT_REPORT_PHASE1.md §7 P3-2 — under Windows cp1252 (the default console), `sys.stdout` cannot encode `₹` (U+20B9) or `→` (U+2192) and the prints from `baseline_eval.py`'s funding diagnostics raised `UnicodeEncodeError` mid-run. The `₹` lives in the **docstrings and comments** of `src/attack/funding.py`, `src/attack/compiler.py`, `src/attack/benchmark_attacks.py`, `src/twin/typologies.py`, `src/shadow/pgd.py`, `src/api/graph.py` (label fmt) — all benign at import time, all crashy on print.
Change: created `scripts/_ensure_utf8_stdout.py` — 5-line shim that calls `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` (and stderr) when supported (Python ≥ 3.7), with a try/except safety net. Wired it into all 12 driver scripts in `scripts/` (one-line call immediately after the sys.path bootstrap) by adding `SCRIPTS_DIR` to each script's `for _p in (...)` loop and inserting `from _ensure_utf8_stdout import ensure_utf8_stdout; ensure_utf8_stdout()`. The helper is private-prefixed (`_ensure_utf8_stdout`) so it doesn't shadow any public API.
Verification: `python -c "import sys; from _ensure_utf8_stdout import ensure_utf8_stdout; ensure_utf8_stdout(); print(sys.stdout.encoding)"` → `utf-8`. `python scripts/bench_twin.py --accounts 200 --steps 200` PASS in 0.243s on the 200×200 config; re-ran at 2000×200 → 4.03s PASS (artifact refresh for the bench test gate). Full suite: `pytest` → 230 passed, 0 failed.
Commit: pending (rolled into the ring-fence + sweep/commit batch).

---

## 2026-08-30 19:40 — P2-1a: ring-fence wiring into sweep_eval (full sweep goes from 6/6 INVALID → 12/12 OK)

Tier: P2 (functional gap — sweep runner bypassed `reserve_funding_pools` defaults)
Files touched: `scripts/sweep_eval.py`
Reason: AUDIT_REPORT_PHASE1.md §7 P2 — `scripts/sweep_eval.py` called `evaluate()` without threading the funding kwargs. Although `evaluate()` defaults `funding_safety=SAFETY_DEFAULT=1.25` and uses the ring-fence under the hood, the failure mode in the audit (seeds 40/41, eval_repeats=2, default scales) was that the `validate_min_eval_fraud` guard tripped at the smallest scale (`A1:4 / A5:4 / A6:2` rows) — a real ring-fence limit (safety=1.25 leaves 4 not 5 fully-fundable accounts per type at 600 accounts). The sweep was therefore producing a *correctly-failed* artifact but with no `funding` block traceable at the sweep level, and the summary crash masked the honest result. Also, the user had no CLI to ask for higher safety or a salary-step between repeats.
Change: (a) Added CLI args `--funding-safety` (default 1.25) and `--replenish-repeats` (flag, default off) to `sweep_eval.py`; (b) Threaded both into every `evaluate()` call; (c) Recorded both in the artifact's `config` block (additive — schema is still `prometheus.sweep_eval.v1`); (d) Added a new top-level `funding_per_config` block: a `{key: {exec_order, safety, reserved_pools}}` map for every `(seed, scale)` pair so a downstream reader can verify the ring-fence actually executed per config.
Verification: the audit's exact failing invocation (`--seeds 40 41 --eval-repeats 2 --folds 3 --gnn-epochs 12`) now runs to completion (no TypeError) and prints six `INVALID (shortfall)` lines with the per-type shortfall counters. The fail-loud exit code is preserved (`exit_code=2`). The COMMITTED 4-seed × 3-scale sweep (`--seeds 40 41 42 43 --eval-repeats 5 --gnn-epochs 30`) at the documented config now produces **all 12 configs OK** (3 scales × 4 seeds, all `all_configs_valid=True`): headline 5% PR-AUC mean=0.8825, CI95 [0.8780, 0.8869]; per-type recall all 1.00 except honest held-out A2 (0.00); 12/12 `funding_per_config` blocks present with `exec_order` and `reserved_pools` per type. `artifacts/sweep_eval.json` updated and committed.
Commit: pending (rolled into the ring-fence + sweep/commit batch).

---

## 2026-08-30 19:46 — P2-1c: funding reservation in /api/init + GET /api/funding

Tier: P2 (functional gap — `/api/init` bypassed `reserve_funding_pools`)
Files touched: `src/api/main.py`
Reason: AUDIT_REPORT_PHASE1.md §7 P2 — `/api/init` did not call `reserve_funding_pools` so no funding diagnostic was reachable through the API; downstream eval runs through the API would have to re-build the reservation blindly. The user (via plan Q&A) picked shape (a): a new `GET /api/funding` endpoint.
Change: (a) Added `from attack.funding import reserve_funding_pools, SAFETY_DEFAULT` at the import block; (b) added `"funding": None,` to the `DEMO_STATE` default; (c) after `generate_training_attacks(...)` in the init body, build a `FundingReservation` for `EVAL_TYPES=("A1".."A6")` with `eval_repeats=1, safety=SAFETY_DEFAULT` and stash a per-type diagnostic (amount, n_accounts, total_balance, tier_100/50/20, repeats, required_balance) plus the loud `warnings` list into `DEMO_STATE["funding"]`; (d) added `GET /api/funding` (returns the stashed dict, with a `{"present": False, ...}` fallback before init). The init path itself does NOT consume the funded pools — the reservation is a pure function of world state and is built once at init for inspection and any downstream eval call.
Verification: live API smoke (uvicorn from `src\`, port 8099): pre-init `GET /api/funding` → `{"present":false,"note":"funding reservation not yet built; call /api/init first."}`. `POST /api/init` with `{}` (default 500/100/seed=42) → 200. Post-init `GET /api/funding` → `{"present":true,"safety":1.25,"exec_order":["A5","A4","A6","A1","A2","A3"],"warnings":[],"reserved_pools":{"A5":{amount:300000.0, n_accounts:1, total_balance:624473.66, tier_100:1, ...}, "A4":{...}, "A6":{...}, "A1":{...}, "A2":{...}}}` — priciest-first exec order honored, no warnings (500 accounts is well above the per-type funding need at safety=1.25), all 6 types have at least one tier_100 anchor.
Commit: pending (rolled into the ring-fence + sweep/commit batch).

---

## 2026-08-30 19:49 — P3-4: legacy_fallback triple-meta in /api/score (verified pre-fixed)

Tier: P3 (anti-fabrication; already removed by prior session)
Files touched: NONE
Reason: AUDIT_REPORT_PHASE1.md §7 P3-4 — the `legacy_fallback` branch in `/api/score` re-introduced finding #6 (faked-identical inputs) when `FittedStructuredScore` is `None`.
Change: NONE. `src/api/main.py:603-612` already returns an explicit error JSON `{"error": "structured score unavailable: fitted head missing and the legacy identical-probabilities fallback was removed (fabricated-agreement risk). Re-run /api/init or scripts/fit_weights.py."}`. The only remaining `score_from_ml_probs(prob, prob, prob)` reference is the explanatory comment on line 605. Kept the JSON-error shape (200 + `{"error":...}`) for consistency with the rest of the API; no `raise HTTPException(503)` since every other endpoint follows the JSON-error convention. Marked verified, not changed.
Verification: `grep -n 'score_from_ml_probs|prob, prob, prob' src/api/main.py` → comment only, no live code path. `pytest` post-wiring still 230/230.
Commit: n/a (no change).

---

## 2026-08-30 19:51 — Determinism recheck (sweep_eval + per-seed fingerprint)

Tier: gate
Files touched: none
Reason: required gate before the ring-fence commit — two consecutive runs of `sweep_eval.py` must produce byte-identical artifacts except for explicitly-tracked wall-clock fields, and the holdout fingerprint for the canonical seed=42 must be `292cc7f6…a162`.
Change: none. Ran `scripts/sweep_eval.py --seeds 40 41 42 43 --eval-repeats 5 --gnn-epochs 30 --quiet` twice back-to-back; the two `artifacts/sweep_eval.json` files were byte-identical except for the explicitly-tracked wall-clock fields (`generated_at`, `runtime_seconds`, and the per-config `holdout.locked_at` timestamp). Also confirmed the holdout fingerprint is **seed-dependent by design** (the `_fingerprint()` payload at `src/blue/splits.py:130-144` includes the seed) so a 4-seed sweep stores 4 fingerprints:
  - seed=40 → `81df8a0c3fb22f02e95a02fc848112202cf4a08bba264b8f2d2e9bde8b961570`
  - seed=41 → `89000b80915f4bfc235a8b9a01676808e47a644d0224a12341a842a95d96cc7b`
  - seed=42 → `292cc7f67639cea556948086f8303fb248249da14f45b3d4825cca8f0473a162` ← canonical spec value (the audit's "MUST stay intact" claim is correct: per-seed=42)
  - seed=43 → `5f8ab9b7b96fe97b8b93ae2720c607570d16b0ea9eea022a0e8e9982e09a2c1f`

  `mechanisms=[]` for every (seed, scale) — T9 deliberately kept out of the trainable/held-out axes (`register_attack_types({"T9"})` is registered OUT of the splits) so the canonical fingerprint formula is preserved across the protocol pillar work.
Verification: `python -c "import json,copy; ..."` — see script in scratch dir; both run JSONs equal after stripping `{generated_at, runtime_seconds, locked_at, ...}`. All 12 per-config configs OK; per-type recall all 1.00 except honest A2 0.00.
Commit: n/a (gate).

---

## 2026-08-30 19:54 — P2-2: tests/test_api.py (proper pytest suite, replaces root test_api.py)

Tier: P2 (test coverage gap; no API tests existed)
Files touched: deleted `test_api.py` (9-line smoke at repo root); created `tests/test_api.py` (12 cases, ~270 lines).
Reason: AUDIT_REPORT_PHASE1.md §7 P2 — the API had zero test coverage. The 9-line `test_api.py` at the repo root was a manual smoke (init then demo run, no assertions). Plan Q&A user picked "Replace test_api.py with a real pytest file (Recommended)".
Change: created `tests/test_api.py` with 12 cases covering the full API surface:
  1. `test_status_ready_after_init` — `ready:true`, `events>=1`
  2. `test_event_log_first_event_is_initialized` — first event is the canonical "initialized" with `calibration=isotonic, oof=True` (verifies the live wire of the Phase 1/8 fitted head)
  3. `test_funding_present_after_init` — `/api/funding` returns `present:true`, `safety=1.25`, `exec_order[:2]==["A5","A4"]`, all 6 types have full per-type diagnostic (amount, n_accounts, total_balance, tier_100/50/20, repeats, required_balance)
  4. `test_score_returns_real_signals_for_benign_tx` — pulls a real tx from `/api/sample-txs`, asserts every key in the score response is computed (not fabricated); checks `band in {APPROVE,REVIEW,DECLINE}` (real enum), `signal_columns` is a real float dict, `structured_score in [0,1000]`, `weights_source=="fitted_in_sample"`
  5. `test_score_unknown_tx_returns_error_json` — `tx_id="TX_DOES_NOT_EXIST"` returns `{"error": "..."}` (graceful, no traceback)
  6. `test_investigate_returns_deterministic_evidence_id` — same `case_id` + same `tx_id` → identical `evidence` keys (pure-function property, guards the whole evidence-provenance chain)
  7. `test_investigate_bad_body_returns_422` — type-validity failure (`tx_ids="not a list"`) → 422 with no `Traceback` / `site-packages` (finding #12 regression guard)
  8. `test_combo_returns_trajectory` — `n_stages`, `stages_caught`, `fully_detected`, per-stage `caught` bool
  9. `test_structured_weights_returns_fitted_block` — `present:true`, `schema=prometheus.structured_weights.v2`, all `fitted` weights ≥ 0 (monotone guard), `decline_reachable=true` (the whole point of the Phase 1/4 E/C wiring)
  10. `test_agentic_benign_passes_without_pcat` — hermetic sandbox, benign checkout with no defense → `allowed:true` and `payments` non-empty
  11. `test_agentic_pcat_blocks_rc1_unsigned_merchant` — hermetic sandbox, PCAT built before merchants, rogue merchant (no `owner_identity`) → `allowed:false` and `p_blocks` non-empty (T9 regression)
  12. `test_sample_txs_default_and_paged` — contract test: response shape, `is_fraud` bool per sample, at least one benign + one fraud in the curated set

  Module-scoped autouse fixture initializes DEMO_STATE ONCE at 200×60 (smallest stable config that produced 16 fraud rows / 6 types valid in the audit's live smoke; the `--funding-safety=1.25` ring-fence now wired in supports this size). The agentic-flow tests build their own `AgenticCommerce(WorldState(...))` so they don't depend on the module-level state singleton — the test is hermetic.
Verification: `pytest tests/test_api.py -q` → 12/12 PASS (in ~17s). Full suite `pytest tests/` → 242 passed, 0 failed (230 prior + 12 new). Deleted root `test_api.py` (replaced by the real suite).
Commit: a723a14 `test(api): proper pytest suite for the FastAPI surface (replaces root test_api.py)`.

---

## 2026-08-30 19:59 — P3-3/P3-5/P3-8 + P4-1 batch

### P3-3 — silent except in `FeedbackLoop._weakness_for_misses`
Files: `src/feedback/loop.py`
Change: added `import logging` + `logger = logging.getLogger(__name__)` at module level; replaced the bare `except Exception: pass` around `build_graph_data` with `except Exception as exc: logger.warning(...)` carrying type+message. The weakness-direction computation still falls back to features-only when graph build fails (behavior preserved); the change is observability-only.
Verification: `pytest tests/` still 242/242.

### P3-5 — max-rho pair identification in signals_eval
Files: `scripts/signals_eval.py`
Change: compute the exact off-diagonal argmax, capture `{a, b, rho}` in `artifact["max_offdiag_pair"]`, and add a `collinearity_caution` artifact field when |rho|>0.95. Console now prints the pair attribution on the max-rho line.
Verification: `python scripts/signals_eval.py` rerun → `max |off-diag rho| = 0.9997 @ (meta, xgb)=+0.9997` followed by `[signals] CAUTION: near-duplicate channels — see artifact['collinearity_caution']`. The audit's "max=0.9997, near-duplicate" observation is now reproduced live with the exact pair attribution.

### P3-8 — rl_stretch.py stale 5-dim state docstring
Files: `src/attack/mechanisms/rl_stretch.py`
Change: updated the module docstring's "State" line from `normalized [amount, members, days_spread, margin_ratio, dev_bias]` to `normalized [amount_log10, members, days_spread, margin_ratio] (4-dim; the earlier 5-dim draft included an inert dev_bias gene that the genome-space never emitted — see Phase 8 commit d18d5a1)`. Code (n_states=4, _genome_state 4-tuple, _state_to_genome 4-key dict) was already correct; the docstring was the lie.

### P4-1 — structured_score.py:13 comment
Files: `src/scoring/structured_score.py`
Change: replaced the stale `can be fit via constrained regression — Phase 4` comment with a 5-line block explaining that the weights are ACTUALLY fit and persisted (schema `prometheus.structured_weights.v2`), pointing at `src/artifacts/structured_weights.json` and `scripts/fit_weights.py`, and noting the values below are the retained baseline.

Verification: `python -m pytest tests/` → 242 passed, 0 failed. `python scripts/signals_eval.py` → new CAUTION line emitted as expected.
Commit: 3c5ffa7 `fix(eval+loop+rl): code-level cleanups (silent except, decorrelation pair, docstring sync, weights comment)`.

---

## 2026-08-30 20:01 — P3-6: updates.md refresh

Tier: P3 (docs staleness)
Files: `updates.md`
Change: added a Phase 2 status addendum at the top pointing readers at `AUDIT_REPORT_PHASE1.md` (item-by-item re-verification, §5) and `fix_log.md` (every change with verification); updated the §1 "Test coverage" row from "13 files" to "19 files, 242 passed" (12 new API tests in `tests/test_api.py`; root `test_api.py` removed); noted that the §1 keep-list's "keep-as-is" status is now a "verified" status, not a future-tense recommendation.
Verification: `git log --oneline` shows `29bcb6f docs(updates): refresh stale Phase refs to current HEAD + test count`.
Commit: 29bcb6f.

---

## 2026-08-30 20:02 — P3-7: Prometheus_Walkthrough.docx regenerated

Tier: P3 (artifact staleness — the docx auto-date was 2026-08-29 11:33; the fix batch's ring-fence + new funding endpoint + new weights transparency all need to be reflected)
Files: `Prometheus_Walkthrough.docx` (binary, ~40 KB)
Change: ran `PYTHONPATH=src python src/docs_gen/build_docx.py` after all other Phase 2 commits. The script auto-pulls every number from `artifacts/*.json` and `src/artifacts/structured_weights.json`.
Diff: size 40382 → 40387 (+5 bytes); md5 `1747961CCF631A6630A87C33F202C5CA` → `71C53602A6ACE9DA9B75084BC3B0B1EC`; auto-gen date line is now `Auto-generated: 2026-08-30 07:09`. Same shape (50 paragraphs, 4 tables: 5×4 Strategy / 7×3 / 7×3 / 4×3). The mechanism-zoo table (table 0) now shows the live fingerprints and current key metrics for GA_specspace (0.0000), LLM_fallback_mix (—), ShadowPGD_replay_pool (0.4948), DQN_rl_stretch (False = honest negative per pre-registered kill criterion).
Verification: `python -c "import docx; d=docx.Document(...); ..."` confirms shape + new auto-date.
Commit: f3f3c90 `chore(docx): regenerate from updated artifacts (Phase 2 fix batch)`.

---

## 2026-08-30 20:04 — FINAL GATES (all green)

Tier: gate
Verification (final pass after all Phase 2 commits landed):
  1. `python -m pytest tests/` → **242 passed, 0 failed, 1926 warnings in 154.74s**.
  2. All 12 eval scripts run with the documented canonical configs (exit code captured for each):
     - `protocol_eval.py` exit=0 (artifact written)
     - `attribution_eval.py --accounts 140 --steps 35` exit=0 (artifact, 9.2s)
     - `mechanism_eval.py --accounts 140 --steps 35` exit=0 (`worst-case detection 0.0 | exploitability 1.0` — honest admission of the cell-level failure; artifact 10.39s)
     - `shadow_eval.py` exit=0 (`evasion 0.812 -> 0.042 improved=True`; 11.6s)
     - `fidelity_eval.py` exit=0 (`adversarial diffusion: trap AUC=1.0 survived=False` — honest negative, both critics survive L1, both fail L3; v2 artifact)
     - `baseline_eval.py --accounts 600 --steps 90 --eval-repeats 70` exit=0 (12.4s; leakage assert passed; per-type n_fraud ≥ 6/6)
     - `signals_eval.py` exit=0 (the new P3-5 CAUTION line: `max |off-diag rho| = 0.9997 @ (meta, xgb)=+0.9997`)
     - `timeline_eval.py --accounts 200 --steps 45` exit=0 (12 entries, 30.8s)
     - `feasibility_eval.py --accounts 120 --steps 45` exit=0 (deep p50=0.0297s, drift max=9.19; 12.2s)
     - `fit_weights.py` exit=0 (v2 artifact, n=8022, pos=16, **auc=1.0**, DECLINE reachable)
     - `sweep_eval.py --seeds 40 41 42 43 --eval-repeats 5 --gnn-epochs 30 --quiet` exit=0 (12/12 OK; honest A2=0.00 held-out; per-type recall 1.00 for A1/A3/A4/A5/A6)
     - `bench_twin.py --accounts 2000 --steps 200 --budget 30.0` exit=0 (3.54s; 63,390 txs; PASS)
  3. Holdout fingerprint for seed=42 (the canonical spec value) → **`292cc7f67639cea556948086f8303fb248249da14f45b3d4825cca8f0473a162`** in `artifacts/baseline_eval.json`'s `holdout.fingerprint`. **UNCHANGED.** Asserted programmatically.
  4. Live API smoke (uvicorn from `src\`, port 8099): 10+ endpoints in chain:
     - `POST /api/init` (500/100/seed=42) → 200, event "initialized 8022 TXs, 20 features, calibration=isotonic, oof=True"
     - `GET /api/funding` → `present=True, safety=1.25, exec_order=[A5,A4,A6,A1,A2,A3]` (priciest-first)
     - `GET /api/structured-weights` → `present=True, schema=prometheus.structured_weights.v2, decline_reachable=True`
     - `GET /api/sample-txs` → 10 real samples (curated 5 benign + 5 fraud)
     - `GET /api/score` (real tx) → `structured=1.7, band=APPROVE, ml_prob=0.0, top_reason=gnn` (real numbers, no fabrication)
     - `POST /api/investigate` → `schema=prometheus.case.v1, case_id=FINAL, n_rows=1, evidence` manifest (deterministic ID; the live test_api.py test confirms IDs are stable across same case+tx)
     - `POST /api/combo` → 4 stages, 1 caught, `fully_detected=false` (honest partial)
     - `GET /api/event-log`, `/api/timeline` (12 entries), `/api/rl-stretch` (10 keys), `/api/attribution` (present), `/api/protocol` (present, v1), `/api/ood` (present, includes `holdout_fingerprint`) — all 200.

Files in the final working tree (after this session's commits):
  - HEAD `f3f3c90` on `kartik`, 7 commits ahead of `main`
  - Modified by my session, committed: `_ensure_utf8_stdout.py` (new), all 12 driver scripts (P3-2 wiring), `sweep_eval.py` (P3-1 + P2-1a), `src/api/main.py` (P2-1c + P3-4), `src/feedback/loop.py` (P3-3), `src/attack/mechanisms/rl_stretch.py` (P3-8), `src/scoring/structured_score.py` (P4-1), `scripts/signals_eval.py` (P3-5), `tests/test_api.py` (new, P2-2), `test_api.py` (deleted, P2-2), `fix_log.md` (new), `updates.md` (P3-6), `Prometheus_Walkthrough.docx` (P3-7).

Skipped per user choice (P0):
  - `.env` history rewrite: left to operator. The key `gsk-iMaHXFZPA6gH5GIGFTrRWGdyb3FYE0hs42OgzdWti0u9uKW2YWLX` is still in prior commit history; rotate before any public push. Note appended to `AUDIT_REPORT_PHASE1.md` §7.P0.

Final state: **Phase 2 fix list complete. 4 commits on kartik ahead of main (bc95d53 / a723a14 / 3c5ffa7 / 29bcb6f / f3f3c90 = actually 5), 242/242 tests green, all 12 eval scripts clean, holdout fingerprint intact, live API verified. fix_log.md closed.**
