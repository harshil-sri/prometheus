# Implementation Plan — Project Prometheus

Live companion to `updates.md`. Every phase below was grounded against the actual
source (file:line verified, not guessed). Status column in §2 is the source of truth.

---

## 1. Ground rules (apply to every phase)

- **Never resecope the holdout lock.** Fingerprint `292cc7f67639cea556948086f8303fb248249da14f45b3d4825cca8f0473a162`
  (A2/A5 held out, mechanism axis empty) is baked into `artifacts/baseline_eval.json`.
  New mechanisms/types are *registered*, not held out, in the shipped lock.
- **Full suite must stay green at every gate.** `pytest tests/ -q` (currently 164/164).
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
| 3 | §7.3 | 6.1 Agentic-commerce pillar (RC-1..RC-5, PCAT) | pending | pytest + protocol eval artifact |
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

## Phase 3 — 6.1 Agentic-commerce protocol attacks + PCAT defense (differentiator)

### Goal
New pillar, scoped to **RC-1 + RC-4** minimum (RC-2/3/5 optional), graded by deterministic judges,
defended by a PCAT-style policy layer (P1–P5), reported as before/after with false-positive rate.

### Ground truth from source
- **Nothing** for `T9`, `RC-*`, `PCAT`, `agentic`, `protocol_structural` exists (repo-wide grep: zero hits).
- Attack registry: `benchmark_attacks.py` A1-A6 (amounts L138-211); mechanism registry `splits.py:48`
  (`rule_compiler`, `shadow_pgd`; others via `register_mechanism`, L58-64); type registry `splits.py:88-93`.
- `log_transaction` (`core.py:296-299`) unconditionally debits; no signature/identity binding concept.
- Merchants live in `world.merchants`; no registry signature check.
- API: `src/api/main.py` — injection/demo/init routes exist (CORS at L51); policy layer inserts after CORS.
- Deterministic-grading ethos already matches the paper's AIP-Bench (measured, not claimed).

### Steps
1. **Twin agentic-checkout flow** (`src/twin/`): agent entity holding a scoped payment credential;
   Mandate-style signed objects (Intent → Cart → Payment) attached to the world; checkout commits via
   `log_transaction` with an integrity check hook.
2. **Attack type `T9`** registered via `register_attack_types`; spec in `benchmark_attacks.py`;
   action sequence in compiler `_build_action_sequence` + executor; each row tagged `mechanism="protocol_structural"`,
   **also registered** via `register_mechanism` (NOT held out — lock unchanged). Sub-cases tagged `rc_class` (RC-1..RC-5).
   - RC-1 (unsigned registry content): rogue merchant entry with no signature check trusted by agent.
   - RC-4 (payment TOCTOU): concurrent authorization vs one budget → double-spend.
   - Optional: RC-2 (untrusted payment destination), RC-3 (credential in observable channel), RC-5 (authz scope not enforced).
3. **Deterministic judges** (`src/eval/judges.py`): wallet-string match, log-pattern regex, event count, status code — no LLM judging.
4. **PCAT policy layer** (`src/policy/pcat.py`): P1 signed responses, P2 caller-identity binding,
   P3 secure-channel enforcement, P4 atomic payment state (asyncio.Lock / compare-and-swap around
   check-then-deduct), P5 tool-call authorization (pre-registered identity header). Inserted after CORS at `main.py:51`
   gating injection/demo/init endpoints.
5. **Protocol eval** (`scripts/protocol_eval.py`): attack-success-rate before/after PCAT + FP rate on benign
   traffic → `artifacts/protocol_eval.json`; surfaced on dashboard panel (§7).
6. Citations kept verbatim for the `.docx` (§8 of updates.md).

### Tests
- RC-1 fake registry entry caught by P1; RC-4 concurrent double-spend blocked by P4 (race test).
- T9 rows carry `mechanism="protocol_structural"` + `rc_class`; fingerprint unchanged; no leakage.
- Deterministic judges: same input → same verdict; FP < 2% on benign traffic in protocol_eval.
- New mechanism present in `splits.MECHANISM_REGISTRY`.

### Gate
Full suite green + `protocol_eval.json` produced (before/after numbers + FP rate) + fingerprint intact.

---

## Phase 4 — 2.2 Fit score weights + transparency

### Goal
Stop hand-picking weights; fit via constrained reduction, persist to the artifact path, and show the fit.

### Ground truth from source
- `FittedStructuredScore.fit()` uses `LogisticRegression` on 6 columns (`structured_score.py:168-193`);
  fitted artifact exists at `src/artifacts/structured_weights.json` (n=786, pos=16, auc=1.0) — but the
  **weighted-formula** `w_*` (`DEFAULT_WEIGHTS`) are still hand-set, and `DEFAULT_WEIGHTS_PATH` points to a
  missing repo-root file.

### Steps
1. `scripts/fit_weights.py`: constrained/monotonic regression (`scipy.optimize.nnls` or `sklearn.isotonic`)
   of standardized evidence terms against eval outcome labels → fit the `w_*` (weighted formula);
   dump to **one canonical path** (reconcile the mismatch: point `DEFAULT_WEIGHTS_PATH` at
   `src/artifacts/structured_weights.json`, keeping the logistic coefs alongside the fitted `w_*`).
2. Report on dashboard: fitted vs baseline weights + provenance (`fit_meta`).
3. Keep deterministic + documented; both scorers remain interpolatable.

### Tests
- Weights file written, schema-valid, monotonic constraints satisfied.
- Two-score-path consistency re-verified (Phase 1 seam).
- Determinism.

### Gate
Full suite green + `artifacts/structured_weights.json` renewed containing fitted `w_*` + dashboard loads it.

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

### Tests
- Hub unit test (fan-in → fan-out, ordering).
- Manual: start server, watch events stream on dashboard.

### Gate
Full suite green + manual stream check on the Dashboard.

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
- `pytest tests/ -q` after every phase (target 164 + new tests, exit 0).
- Determinism spot-check: rerun the committed eval config; artifact hashes must match (modulo regenerated_at).
- Holdout fingerprint asserts: `artifacts/baseline_eval.json["holdout"]["fingerprint"]` stays `292cc7f6…a162`.
- `python scripts/baseline_eval.py --help` reflects any new flags before demo.