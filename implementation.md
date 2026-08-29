# Implementation Plan — Project Prometheus

Live companion to `updates.md`. Every phase below was grounded against the actual
source (file:line verified, not guessed). Status column in §2 is the source of truth.

---

## 1. Ground rules (apply to every phase)

- **Never resecope the holdout lock.** Fingerprint `292cc7f67639cea556948086f8303fb248249da14f45b3d4825cca8f0473a162`
  (A2/A5 held out, mechanism axis empty) is baked into `artifacts/baseline_eval.json`.
  New mechanisms/types are *registered*, not held out, in the shipped lock.
- **Full suite must stay green at every gate.** `pytest tests/ -q` (currently 191/191).
  Any new test failure before all-regression is a phase blocker.
- **Fail loud, never fake.** No silent fallbacks; any fallback path is logged and
  surfaced in the artifact (matches the eval gate philosophy).
- **Deterministic.** Seeded RNGs, stable JSON ordering; reruns reproduce artifacts bit-for-bit.
- **No leakage.** New evidence/mechanisms never read eval-population labels at generation time.
- **CPU-only dev box.** Torch/py code must run in 8GB RAM without GPU.
- **Docs-first evidence.** Every phase updates `PROMETHEUS_CONTEXT.md` and, where it is the
  deliverable, regenerates the `.docx` via `src/docs_gen/build_docx.py`.
- **Commit per phase** on branch `kartik` after the full-suite gate (user-driven commit).

---

## 2. Status dashboard

| Phase | Priority (updates.md) | Item | Status | Gate |
|---|---|---|---|---|
| 0 | — | Baseline checkpoint & guardrails | done — verified this session: `kartik`@`9f386bb`, 155/155 green on `~/.venvs/global`, fingerprint intact | 155/155 green |
| 1 | §7.1 | 2.1 Scoring E/C evidence + band reachability | done — 159/159 green; `w_e`/`w_c` + deterministic mappers + unified `/api/score`↔`/api/investigate` via `case_evidence_context()` | pytest + E/C tests + api consistency |
| 2 | §7.2 | 2.3 Ring-fence funding + loud diagnostics | done — 164/164 green; `funding.py` disjoint deterministic per-type reserves, priciest-first exec, `funding` block in `baseline_eval.json` v4, fp intact | suite + regenerated artifact + double-run determinism |
| 3 | §7.3 | 6.1 Agentic-commerce pillar (RC-1..RC-5, PCAT) | done — 191/191 green; T9 RC-1..RC-5 land naive / blocked 1:1 by PCAT P1-P5, deterministic judges, `protocol_eval.json` verdict (5/5 → 0/5, benign FP 0/5), fp intact | pytest + protocol eval artifact |
| 4 | §7.4 | 2.2 Fit score weights + transparency | pending | pytest + weights artifact + panel |
| 5 | §7.5 | 5 SSE/live visualization | pending | pytest + manual stream check |
| 6 | §7.6 | 6.2 Diffusion-model fidelity critic | pending | pytest + fidelity artifact v2 |
| 7 | §7.7 | 3 Standout panels (timeline / RL / attribution / weights) | pending | pytest + panel data checks |
| 8 | §7.8 | 2.4 Hygiene + finalize (docx, artifacts, demo) | pending | full suite + run-through |

---

## Phase 0 — Baseline checkpoint & guardrails
- Verify branch `kartik` at `9f386bb fix: red-team integrity + eval robustness gate` (13 files, suite 155/155).
- Record baseline artifact fingerprint + timestamps; snapshot `pytest` output path.

**Gate:** `pytest tests/ -q` = 155 passed, exit 0.

---

## Phase 1 — 2.1 Wire E/C evidence into the structured score (CRITICAL)

### Goal
Make the plan's formula real: `R = w_t·T + w_g·G + w_b·B + w_e·E + w_c·C − w_u·U`.
Investigator output (sanctions, OSINT, campaign memory) must land in the score a judge can see.

### Ground truth from source
- `src/scoring/structured_score.py` — `DEFAULT_WEIGHTS` has only `w_t,w_g,w_b,w_u` (L9-14);
  `compute_structured_score(transaction, graph, behavioral, uncertainty, ...)` reads 4 terms only (L26-32, L49-58).
- Production path is the **fitted scorer**: `FittedStructuredScore.predict_row` over 6 signal columns
  (`structured_score.py:153-293`), called by `case_manager.py:306-311` and `/api/score` (`main.py:351-360`).
- Sanctions evidence **exists but unquantified**: `sanctions.py:136-143` returns `WATCH_HIT/CLEAR` +
  `match_strength ∈ [0.72,0.98]`; case report only has `watch_hit_count` (`case_manager.py:335`).
- OSINT risk fields (`osint_fixtures.py:70-94`: `watch_flags`, `risk_state_at_enrichment`,
  `device_history`, `registrar_churn_seen`) are **dropped** before evidence registration (`case_manager.py:196-201`).
- Band reachability bug: weights sum = 750 → DECLINE band (~unreachable at realistic signals). Adding E/C raises the ceiling.
- Path mismatch: `DEFAULT_WEIGHTS_PATH` → repo-root `artifacts/structured_weights.json` (missing);
  real file is `src/artifacts/structured_weights.json` used by `api/main.py:39-41`.

### Steps
1. Add `w_e`, `w_c` to `DEFAULT_WEIGHTS` and to `compute_structured_score()` (params `external_evidence`, `campaign_evidence`).
2. Deterministic **E mapping** (no LLM): bounded `[0,1]`.
   - Sanctions: `max(match_strength)` over sanctioned senders; else `min(1.0, watch_hit_count / n_senders)`.
   - OSINT: fold `watch_flags` + `risk_state_at_enrichment` + `device_history.distinct_devices` via documented scoring fn; register full dossier (stop dropping risk fields at `case_manager.py:196-201`).
3. Deterministic **C mapping**: campaign evidence from `memory.attack_signatures` — recurrence >1 on
   same mechanism/fingerprint → `min(1.0, recurrence / 3)`; plus same-kind aggregations
   (shared merchant / device motif via `SpectralAgent` residuals as documented partial proxy).
4. Extend `FittedStructuredScore.predict_row` to accept optional `external_evidence`, `campaign_evidence`;
   combination rule: logistic ML prior fills T/G/B/U slots, E and C added linearly with their weights,
   same `[0,1000]` clip. Document the rule in code + dashboard (no opaque blending).
5. **Unify the two score paths**: extract shared `score_case()` used by `/api/score` and `/api/investigate`
   so a tx cannot disagree across endpoints (`main.py:333-382` vs `case_manager.py:298-324`).
6. Add `reason_evidence_ids["osint"]`, keep `["sanctions"]`, add `["campaign"]` provenance in the case report.

### Tests
- E mapping: sanctions hit → `external_evidence > 0`, clean → 0; monotonic in recurrence.
- C mapping: recurrence 1→0, 3→1 clipping; determinism.
- Band reachability: all-1 evidence now reaches DECLINE band (>700).
- `/api/score` and `/api/investigate` consistency on same tx.
- Regression: suite stays green.

### Gate
Full suite green + new E/C tests + one manual dashboard check that the score console shows E/C components.

---

## Phase 2 — 2.3 Ring-fence per-attack-type funding + diagnostics

### Goal
Kill the A5 depletion wall: every attack type is guaranteed a deterministic, disjoint funded sub-pool
so any seed/scale can generate its `min_eval_fraud_per_type` rows; failures become loud diagnostics.

### Ground truth from source
- Eval runs attacks against one un-stepped world (`baseline_eval.py:123-132`); no replenishment between repeats.
- `_select_entities` (`compiler.py:544-622`) is greedy-global with 100/50/20% tiers (L579-586) + all-accounts fallback (L586, L596).
- Direct-debit actions (`small_test_transaction`/`large_transfer`/`cash_out`, `compiler.py:199-298`) bypass
  the `_capacity` clamp that typologies use (`typologies.py:124-159`) — can overdraft (balance floor 0.01).
- Fail-loud gate: `GenerationShortfallError` if any eval type < 5 fraud rows (`baseline_eval.py:175-186`); sweep records per-config errors (`sweep_eval.py:179-187`).
- Sweep scales: 4 seeds × 3 scales, schema v3 (`baseline_eval.py:303`) / v1 (`sweep_eval.py:198`).

### Steps
1. New module `src/attack/funding.py`: `reserve_funding_pools(world, specs_by_type, eval_repeats, safety)`
   → `FundingReservation{.order, .pools, .diag, .warnings}`. Deterministic disjoint pools carved from the
   funded upper tail at eval start (rank = live balance desc + account_id asc — a pure function of the
   world), each pool sized to `amount × eval_repeats × safety`, priciest types claim first; per-type
   diagnostics record pool size/total balance + per-solvency-tier (100/50/20%) funding.
2. Compiler: `AttackCompiler(funded_pool=[...])` restricts `_select_entities` EXCLUSIVELY to that reserve
   (main + members); `last_funding_stats` records tier funding of the pool *before* selection every
   compile; pool-aware precondition `funded_pool_N_accounts`; exhausted/thin pool → loud
   `funding.warnings` + observed tier counts in artifact, never a silent empty selection.
3. Eval phase reordered priciest-first (A5→A4→A6→A1→A2→A3 via `funding.order`); optional
   `--replenish-repeats` salary twin step between repeats (default off). Direct-debit overdraft clamp
   (plan v1 #3) DEFERRED decisional: funded-pool selection already prevents overdrafts for eval attacks,
   and touching `log_transaction` semantics risks unrelated regressions — recorded, low value.
4. Diagnostics in artifact: `baseline_eval.json` schema **v4** gain (additive) `funding` block:
   `reserve_policy`, `safety`, `replenish_between_repeats`, `exec_order`, `reserved_pools` (per-type
   ₹-quantified), `observed_at_compile` (per repeat); pool shortfall → `generation_warnings`.
5. `sweep_eval.py` inherits via `evaluate()` kwargs (`funding_safety`, `replenish_repeats`) — no code dup.
6. Regenerate `baseline_eval.json` on the committed 1200×150×140 config (fingerprint intact).

### Tests
- Regression: legacy A5 starved-config logic exercised — new invalidation: A5 runs FIRST with ≥5 fully
  funded anchors; regenerated artifact shows A5 n_fraud=20 on the committed config (was 0/none pre-gate).
- Determinism: two runs → identical pool partition + artifact (double-run diff = wall-clock fields only).
- Diagnostics present per type incl. tier counts; thin-economy pool emits a LOUD funding warning;
  compiler never selects outside its reserve.
- `full_report`/per-type breakdown unchanged in semantics for already-green configs.

### Gate
Full suite green (164/164) + regenerated artifact contains `funding` diagnostics + recorded before/after
A5 headline + holdout fingerprint unchanged + double-run determinism verified.

---

## Phase 3 — 6.1 Agentic-commerce protocol attacks (T9, RC-1..RC-5) + PCAT defense

> **Scope decision (this session):** ALL FIVE RC-1..RC-5 structural attack classes,
> defended 1:1 by the full PCAT policy layer P1–P5 (mirrors updates.md §7.1's
> "do 2-3 RCs well" guidance with a stronger target; RC-2/RC-5 are cheap additions).

### Goal
A brand-new pillar a technical judge hasn't seen from other teams: protocol-level
(STructural) agentic-commerce attacks that succeed on ANY model — modeled inside
the twin's own agentic-checkout flow, graded by deterministic judges, defended by a
PCAT-style policy layer, reported as an independent before/after with FP rate.

### Ground truth from source
- **Nothing** for `T9`, `RC-*`, `PCAT`, `agentic`, `protocol_structural` exists (repo-wide grep: zero hits).
- Mechanism registry is a namespace + self-registration at import (`blue/splits.py:48`,
  `register_mechanism` L58-64; mechanism modules e.g. `attack/mechanisms/shadow_pgd.py:39` call it at import).
  `register_attack_types()` (`splits.py:91-93`) exists but is **never called** yet (T9 will be the first).
- Holdout fingerprint = sha256 over the *held-out sets + seed* (`splits.py:130-144`), NOT the registry →
  registering `protocol_structural` + `T9` CANNOT change `292cc7f6…a162`.
- `log_transaction` (`twin/core.py:254-326`) is a plain-dict writer with `mechanism`/`attack_id`/
  `trajectory_id` params; extra keys (e.g. `rc_class`) are tolerated by downstream consumers.
- Merchants are `world.merchants` states (`domain/category/hosting_asn/template_fingerprint`,
  `core.py` `add_merchant`); a rogue registry entry mirrors into the twin graph naturally.
- FastAPI app builder + CORS at `src/api/main.py:43-51` + `sys.path` bootstrap L21-22;
  existing endpoints must NOT gain mandatory headers (would break the 164-test suite).

### Module map (all new, no core.py edits)
| Module | Responsibility |
|---|---|
| `src/twin/agentic.py` | `AgenticCommerce(world, seed)` coordinator: `Agent`+`Credential` (scoped budget), Mandate-style signed objects (Intent→Cart→Payment, deterministic sha256 signing), merchant `registry` (+ mirrors to `world.merchants`), `checkout()` with policy hooks, audit `events` log, observable-channel `session_log` (RC-3), atomic budget CAS |
| `src/attack/protocol_attacks.py` | T9 RC-1..RC-5 builders + `run_t9_case(agentic, rc, defense, seed)`; `register_mechanism("protocol_structural")` + `register_attack_types({"T9"})` at import |
| `src/eval/judges.py` | Deterministic judges (wallet-string match / regex / event-count / status-code) via `judge_case(rc, events) → verdict` (no LLM; AIP-Bench style) |
| `src/policy/pcat.py` | `PCATPolicy.enforce(op) → (allowed, reason)` — P1 signed registry, P2 identity-bound payout, P3 observable-channel, P4 atomic check-then-deduct (threading.Lock + CAS), P5 preregistered caller identity |
| `scripts/protocol_eval.py` | before/after harness → `artifacts/protocol_eval.json` (per-RC success w/o vs with PCAT + benign FP rate + verbatim citations) |
| `src/api/main.py` | + `POST /api/agentic/checkout` (PCAT-enforced, P4 lock), `GET /api/agentic/status`, `GET /api/protocol` (serves the artifact; Phase 7 panel) — existing endpoints untouched |
| `tests/test_protocol.py` | registry wiring, per-RC before/after, benign FP, determinism, fingerprint guard |

### T9 sub-case ↔ defense mapping (1:1, judge-legible)
| RC | Attack (any model, 100% reproducible) | Judge (deterministic) | PCAT check |
|---|---|---|---|
| RC-1 | unsigned/forged registry entry trusted; agent checks out to attacker payout | wallet-string: payment landed at attacker payout | P1 signed-registry responses |
| RC-2 | federation response returns attacker payout w/o identity binding | payout-string: tx reached unbound payout | P2 caller identity binding |
| RC-3 | agent credential leaked into observable channel, replayed by attacker | regex scan of channel + replay succeeds | P3 secure channel + redirect allowlist |
| RC-4 | two concurrent authorizations pass the check → double spend | event-count + paid_sum > budget | P4 atomic check-then-deduct |
| RC-5 | checkout succeeds without required scope/identity | status-code: allowed despite missing scope | P5 tool-call authorization |

### Steps
1. `src/twin/agentic.py` — Agents/Credentials (deterministic), Mandate signing (canonical-JSON + sha256,
   ring-fenced key material), merchant registry mirror, `checkout()` (resolves registry → builds mandate →
   policy hooks → atomic authorize → `world.log_transaction` with `mechanism="protocol_structural"`,
   `attack_id="T9"`, `rc_class`), audit events + observable session_log.
2. `src/attack/protocol_attacks.py` — T9 spec in `benchmark_attacks.py` (NOT added to `TRAINABLE_ATTACKS`
   or the A1-A6 `EVAL_TYPES` — T9 is measured by its OWN protocol_eval, keeping the baseline lock meaning
   intact); `run_t9_case()` wires each RC's prerequisite state + checkout(s); registers the new
   mechanism + type axis members at import.
3. `src/eval/judges.py` — 5 pure judges, dispatch by rc_class; same input ⇒ same verdict.
4. `src/policy/pcat.py` — P1–P5; `checkout()` calls `enforce(op)` at each hook; defense=None ⇒ attacks pass,
   defense=PCAT() ⇒ blocked. Benign flow must pass every check (FP ≈ 0 by construction).
5. `scripts/protocol_eval.py` → artifact with per-RC before/after + benign FP + verbatim citations
   (Louck AIP-Bench arXiv:2607.21824; Mastercard Agent Pay 2025-04-29; Visa TAP 2025; Google AP2 2025).
6. API: `/api/agentic/checkout` (real lock, real enforce), `/api/agentic/status`, `/api/protocol`.
7. Determinism double-run + fingerprint unchanged + full suite green.

### Tests
- Import wiring: `protocol_structural ∈ MECHANISM_REGISTRY`; `attack_type_of_tx("T9_…") == "T9"`;
  holdout fingerprint unchanged.
- Per-RC: no-defense ✅ captured by judge; PCAT-on ❌ blocked (5 before/after pairs).
- RC-4 concurrency: naive impl double-spends (paid_sum > budget); P4 allows exactly one payment.
- Benign agentic checkout: 0 blocked with defense (and `FP_rate < 2%` contract in protocol_eval).
- Deterministic judges + same-seed artifact equality (modulo wall-clock fields).

### Gate
Full suite green + `artifacts/protocol_eval.json` produced (before/after + FP rate) + fingerprint intact.

### Implemented (this session)
- `src/twin/agentic.py` — `AgenticCommerce(world, seed)`: deterministic sha256 Mandate signing
  (canonical-JSON, ring-fenced per-instance key material), scoped `Agent`+`Credential` budgets,
  merchant `registry` with mirror into `world.merchants`, `checkout()` with policy hooks +
  atomic `_authorize_batch` CAS (P4), audit `events` + observable `session_log` (RC-3),
  `is_credential_observed`, `resolve_payout` federation seam (RC-2).
- `src/attack/protocol_attacks.py` — `run_t9_case(world, seed, rc_class, defense_builder)` +
  `benign_checkout` FP control; `register_mechanism("protocol_structural")` +
  `register_attack_types(["T9"])` at import (fingerprint-safe; T9 isolated). T9 spec lives in
  `benchmark_attacks.PROTOCOL_ATTACKS`, OUT of ALL_TRAINABLE_HELDOUT sets.
- `src/eval/judges.py` — 5 pure judges + `judge_benign`, dispatched via `register_judge`;
  verdict is a pure function of the structural facts in the case pack.
- `src/policy/pcat.py` — `PCATPolicy`; *live-wired* to the AgenticCommerce (`for_agentic(ac)`
  resolves certified payouts + authz table at `enforce()` time → construction order never
  matters). P1 verifies the signature itself; P2 certifies only signed + identity-bound
  payouts; P5 requires a pre-registered caller identity with a matching scope subset.
- `scripts/protocol_eval.py` → `artifacts/protocol_eval.json` (schema v1, deterministic payload):
  **naive 5/5 → pcat 0/5, benign FP 0/5**, fingerprint intact, verbatim §8 citations.
- API: `POST /api/agentic/checkout` (real lock + real enforce + real judge verdict),
  `GET /api/agentic/status`, `GET /api/protocol` (serves the real artifact, honest fallback).
  The API sandbox owns a SEPARATE WorldState — the twin/init dataset is never perturbed.
- RC-4 semantics: under PCAT the single legitimate authorization still lands (allowed, 1 payment
  ≤ budget); the TOCTOU overspend is what P4 refuses — the judge keys on `over_spent`.
- Suite 164 → **191/191**; 27 new protocol tests; working tree ready to commit.

---

## Phase 4 — 2.2 Fit score weights + transparency

### Goal
Stop hand-picking weights; fit via constrained reduction, persist to the artifact path, and show the fit.

### Status: ✅ DONE

### Ground truth from source
- `FittedStructuredScore.fit()` uses `LogisticRegression` on 6 columns (`structured_score.py:168-193`);
  fitted artifact exists at `src/artifacts/structured_weights.json` (n=786, pos=16, auc=1.0) — but the
  **weighted-formula** `w_*` (`DEFAULT_WEIGHTS`) are still hand-set, and `DEFAULT_WEIGHTS_PATH` points to a
  missing repo-root file.

### Steps
1. ✅ `scripts/fit_weights.py`: constrained/monotonic regression (`scipy.optimize.nnls` — non-negativity ⇒
   every `w_*` ≥ 0 ⇒ monotone) of standardized evidence terms against deterministic targets (fitted
   logistic P(fraud)×1000 on the canonical twin's real rows + a documented 6-cell calibration grid
   pinning the sparse E/C axes and the w_u penalty) → fitted `w_*`; dump to **one canonical path**
   (`DEFAULT_WEIGHTS_PATH` reconciled to `src/artifacts/structured_weights.json`, schema v2, logistic
   coefs alongside the fitted `w_*`). Artifact renewed on the canonical config (n=8022, pos=16, auc=1.0)
   and, with no wall-clock fields, regenerates byte-for-byte deterministically (verified by double run,
   `cmp`-identical). The pure fit lives in `src/scoring/weight_fit.py`, shared by script + tests.
2. ✅ Report on dashboard: new **Fitted vs Baseline Formula Weights** panel (per-term fitted/baseline/Δ,
   decline-reachability, provenance) fed by `GET /api/structured-weights` (reads the committed artifact,
   deterministic across random-seed session inits); the spectrum card's provenance pill + weights_source
   bubble. `save()` merge-preserves `w_formula`/`baseline_weights`/`w_fit` from the file, so session
   refits (random-seed logistic) never wipe the canonical fitted weights.
3. ✅ Deterministic + documented; both scorers stay interpolatable: `predict_row` uses fitted `w_e`/`w_c`
   additively (seam-equal to the weighted formula, verified by test); `compute_structured_score` accepts
   any weights override. Fitted-vs-baseline + provenance reported in the artifact (`w_fit` diagnostics:
   per-column std, degenerate terms, reachability scale, residual, grid) — including WHY a term may fit
   to 0 (near-zero variance / collinearity of ensemble outputs on the twin) instead of hiding it.

### Tests
- ✅ Weights file written to the canonical path, schema-valid (`prometheus.structured_weights.v2`), six
  `w_*` keys, monotone (all ≥ 0, squashing negative-weight load ⇒ ValueError), reachability
  (max raw = 1000 ⇒ every band reachable).
- ✅ Two-score-path consistency re-verified: `predict_row` Δ(E)=w_e, Δ(E+C)=w_e+w_c (test_weights seam).
- ✅ Determinism: `fit_w_star` on a fixed matrix twice ⇒ identical dicts/diagnostics (in-suite);
  full `scripts/fit_weights.py` double-run byte-identical (implementation-time check).

### Gate
✅ Full suite green (198 passed incl. 6 new test_weights) + `artifacts/structured_weights.json` renewed
(n=8022/pos=16) containing fitted `w_*` + dashboard loads the `/api/structured-weights` panel.

---

## Phase 5 — 5 SSE / live visualization

### Goal
Live event pushes so the dashboard shows the loop streaming, not only snapshots.

### Ground truth from source
- No broadcast hub exists; the streaming endpoint is a single self-contained generator at `main.py:521-636`.
- OOD heatmap does not re-render after page reload (`checkStatus()`, `index.html:2209-2226`).

### Steps
1. `src/api/events.py`: `asyncio.Queue` fan-out hub; publishers on init/scan/demo/inject/news events (non-blocking).
2. Dashboard `EventSource` subscribes; panels re-render live; keep existing poll/init path as fallback.
3. Fix `checkStatus()` to re-render the OOD heatmap after reload.

### Implementation notes
1. ✅ `src/api/events.py` — `EventHub`: late-joiner snapshot (every subscriber is seeded with the latest
   retained event; `clear_snapshot()` drops it when a brand-new sim run starts, so a reconnect can never
   replay an old `done`); bounded per-subscriber queues with **drop-oldest** so one stalled HTML stream
   cannot back-pressure the loop; thread-safe `publish()` via `loop.call_soon_threadsafe` (FastAPI sync
   endpoints run in a threadpool). Pre-bind publishes (e.g. `/api/init` from a reloader) are remembered
   and flush to the first subscriber.
2. ✅ `src/api/main.py` — the old per-client inline generator (`/api/stream`) became ONE producer task
   (`_stream_producer`, 30 twin steps + dynamic attacks + `done`) guarded by `stream_running` under a
   per-event-loop `asyncio.Lock` (side-effect-free broker restart on fresh connect-after-done). `/api/stream`
   now subscribes to the hub (fresh-run ⇒ snapshot cleared ⇒ client seeded with live steps), keeps the SSE
   contract (`retry: 1000`, `data:` JSON frames, 15 s heartbeat `: heartbeat`, client-side `done` break),
   and returns the pre-init `{"type":"error"}` frame unchanged. Non-blocking publishers added on:
   `/api/init` (`type: init`), `/api/stream/inject` (`type: inject` — in-flight directives are still
   consumed by the sim via `pending_injections`), `/api/combo` (`type: combo` + result summary).
3. ✅ `index.html` — panel live re-render: SSE gains handling for `init`/`inject`/`combo` frames,
   `streamStatus` now shows a live `Step n/30 · peak · caught` tally; step/done contract unchanged; and
   `checkStatus()` now calls `renderOODHeatmap()` so the Mechanism × Type matrix re-renders after reload.

### Tests
- ✅ `tests/test_events.py` (10 new): fan-in ordering, fan-out to every subscriber, late-joiner snapshot,
  snapshot clear, bounded drop-oldest cannot stall the hub, unsubscribe stops delivery, thread-safe
  publisher from a worker thread, pre-bind publish seeds first subscriber, SSE frame encoding, and the
  `/api/stream`-not-ready `error` frame contract. No pytest-asyncio dependency (each test drives its own
  loop via `asyncio.run`, mirroring the uvicorn loop bind).
- ✅ End-to-end: TestClient init → SSE stream yields `init`/`step`/`done` (probe-verified); `first.get("type")
  in (step, error, done, init, inject, combo)` in `test_phase10.py` (Phase 5 hub producers extend the set).
- ✅ Manual: start server, watch events stream on dashboard.

### Gate
✅ Full suite green (209 passed, +10 new test_events) + live stream probe verified: single producer fan-out,
late-joiner snapshot, dashboard live re-render, OOD heatmap re-renders after reload.

---

## Phase 6 — 6.2 Diffusion-model fidelity critic

### Goal
Fidelity claim holds against a diffusion critic, not just CTGAN.

### Ground truth from source
- CTGAN critic in `src/eval/fidelity.py` (statistical L1 and adversarial L3 pathway, `fidelity.py:56-57,242-245`);
  report built by `build_fidelity_report` (`fidelity.py:328-347`); runner `scripts/fidelity_eval.py:78-95`.
- Deps already pinned: torch, scipy, sklearn, sdv in requirements.

### Steps
1. Add a small TabDDPM-style diffusion generator in `scripts/fidelity_eval.py` alongside CTGAN
   (few-hundred-line off-the-shelf approach; CPU-friendly config).
2. Feed both CTGAN and diffusion critics into L1/L3; report both in schema (additive schema bump to v2).
3. Docs: copy the exact citations (arXiv 2603.13566 EmDT; arXiv 2604.13125 fraud-pattern benchmark).

### Tests
- Critic runs on small config deterministically; artifact carries both critic sections.

### Gate
Full suite green + `fidelity` artifact containing both critics.

---

## Phase 7 — 3 Standout panels

### Goal
Four high-credibility visuals; panels reuse the `renderOODHeatmap` pattern.

### Steps
1. **Blind-spot timeline** — persist every retrain cycle's report (extend feedback loop to append to a
   timeline artifact); render blind-spot → fixes → recall trajectory across all rounds.
2. **RL negative-result panel** — serve the `rl_stretch` measurement artifact (pre-registered kill criterion,
   honest negative) on its own panel.
3. **Mechanism × evidence-source attribution** — per caught attack: generation mechanism
   (rule_compiler / shadow_pgd / llm_strategist / protocol_structural) × evidence source
   (XGB / GNN / OSINT / sanctions), rendered as a matrix.
4. **Fitted-weights transparency** — plot from Phase 4.

### Tests
- Panel data endpoints return defined-schema JSON; dashboard init renders without console errors.

### Gate
Full suite green + all four panels render on the Dashboard.

---

## Phase 8 — 2.4 Hygiene + finalize

### Steps
1. `rl_stretch.py:74` `device bias placeholder` genome dim is inert (only dims 0-3 used, `rl_stretch.py:88-94,170-173`):
   **drop it** OR wire to a real device-selection signal — decision recorded; ensure every genome dim is a live signal.
2. Sanctions/yente live transport: already correctly deferred (`NotImplementedError` behind documented
   fallback) — record a tracked follow-up note in `PROMETHEUS_CONTEXT.md` (not a bug).
3. Regenerate all artifacts (`baseline_eval`, `sweep_eval`, `protocol_eval`, `ood_matrix`, fidelity) post-final-change.
4. Regenerate `.docx` via `src/docs_gen/build_docx.py` (reads `honest_holdout_metrics.meta.multi_prevalence` + `holdout.fingerprint`).
5. Update `PROMETHEUS_CONTEXT.md` (include appended phase log).
6. Final demo checklist: server up, dashboard panels, live stream, one walk-through of the loop script.

### Gate
Full suite green; all artifacts regenerated & referenced; docx regenerated; run-through passes on the dev box.

---

## Cross-cutting verification protocol
- `pytest tests/ -q` after every phase (target 191 + new tests, exit 0).
- Determinism spot-check: rerun the committed eval config; artifact hashes must match (modulo regenerated_at).
- Holdout fingerprint asserts: `artifacts/baseline_eval.json["holdout"]["fingerprint"]` stays `292cc7f6…a162`.
- `python scripts/baseline_eval.py --help` reflects any new flags before demo.