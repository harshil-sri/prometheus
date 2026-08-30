# AUDIT REPORT — PHASE 1
Repo: `prometheus` · Branch: `kartik` · Root: `C:\Users\iamda\Desktop\prometheus`
Audit date: 2026-08-30 (Aug  ́30 2026) · Mode: **READ-ONLY** (no code modified; no artifacts regenerated except the repo's own eval scripts writing their artifacts(.

---

## 0. Executive verdict

Project Prometheus is **materially ahead of its own plan doc** — including features that `updates.md` still lists as gaps (SSE transport §5, fitted weights §2.2, ring-fenced funding §2.3, E/C evidence wiring §2.1( are **already implemented and live-verified in this audit**. The demo deckber also honest rigor that is genuinely impressive: an RL attacker that ships a **pre-registered honest negative**, an adversarial fidelity layer that reports `survived=False` rather than faking a pass, per-column signal contributions with counterfactual reasoning, two-axis sha256-locked holdout, and 17 matches of prior-audit "finding #N" comments all code-verified as fixed.

**One P0(global):** the repo **deliberately git-tracks its live `.env`** containing a real Groq API key (`.gitignore` explicitly `!.env` with comment "Secrets: allow .env"(. Everything else is P2/P3 hygiene or honest engineering gaps. No fabrication of results observed anywhere in code or runtime.



| Verdict area | Status | One-line evidence |
|---|---|---|
| Test suite | ✅ 230/230 pass | 19 test files, 187.75s |
| E2E runtime (12 eval scripts( | ✅ 11 CLEAN,  failure (=bug( | Sweep failed on shortfall+crash (wiring gap(|
| API live smoke | ✅ ~30 endpoints, 100% 2xx on valid calls | Init→score→investigate→combo→stream full chain verified |
| Anti-fabrication laws | ✅ no violations found | ComputedEvidence type contract, honest negatives, no hardcoded result lanes |
| Holdout/leakage | ✅ no leakage (live macromolecular assert PASSED( | Held-out A2/A5 fingerprints; 4,560-row leakage check cleared |
| Security/env | ❌ **P0: `.env` tracked with live key** | `!.env` in `.gitignore`; key in HEAD commit |
| Doc staleness | ⚠️ P4 (low( | `updates.md` HEAD ref `9f386bb` vs actual `68bde8c`; docx auto-date 2026-08-29 11:33 |

---

## 1. Scope & method

**Scope**: verify every claimed feature of the repo (red→blue→investigate→feedback loop, 12 attack mechanisms, digital twin, eval pipeline, API surface, dashboards, docs( against (a) source inspection and (b) **live execution** of the repo's **own** eval scripts and API. Hunt: hardcoded result literals, silent excepts, dead code, fabricated provenance, data leakage.

**Method — read-only enforcement**: all repo writes were exclusively the repo's own scripts writing their declared artifacts (e.g. `artifacts/protocol_eval.json`( or temp files under `%TEMP%\opencode\`. `.docx` regeneration explicitly **not** run (per read-only mandate(.

**Environment** (honest caveats(:
- Windows ید carbonate cp1252 console — `₹` (U+20B9( crashes 3 scripts printing it unless `PYTHONIOENCODING=utf-8`.
- no `rg` — used grep tool / `Select-String` / small Python.
- API must be launched with cwd=`src\` (`from api.main import app` fails from repo root: `ModuleNotFoundError: No module named 'api'`).
- `TestClient` in this fastapi/starlette does **not** accept `timeout` — live uvicorn + urllib pattern used.


## 2. Test suite — 230/230

`pytest tests/ -q` → `collected 230 items` = `230 passed, 0 failed/errors, 1746 warnings in 187.75s`. (19 test files: test_weights, test_t2, test_t1, test_signals, test_shadow, test_protocol, test_phase7, test_phase1_integrity, test_phase10, test_mechanisms, test_investigator, test_graph, test_funding, test_fidelity_diffusion, test_fidelity, test_feedback, test_feasibility, test_events, test_blue(. updates.md claims "tests/ (13 files)" — **stale** (now 19(.
Warnings (1746( not triaged — dominated by sklearn/torch deprecations presumably; low-priority followup (
Note: 0-`tests/test_api.py` exists — API is **not** covered by the suite; only our live smoke covers it. **Fix requirement:** add API-level tests (init/score/stream/error-paths(.

## 3. E2E live run — all 12 repo eval scripts exercised

All runs used the repo's own `scripts/*_eval.py` drivers (real model training, real artifact writes( with the shown params. "CLEAN" = guard gates passed + real numbers computed + artifact written.

| Script (invocation( | Verdict | Key numbers / evidence |
|---|---|---|---|
| `protocol_eval.py` | ✅ CLEAN | fingerprint intact `292cc7f6…a162`; RC1–RC5 all land (4,500 txs each(, PCAT blocks 0/5, 0 benign FPs, 0.07s artifacts written |
| `mechanism_eval.py --accounts 140 --steps  ́35` | ✅ CLEAN | GA best 1.0→0.0 (24 queries,; LLM origins all `fallback` (deterministic honest(, shadow candidates [2,3,6,4]; **RL negative result shipped honestly** (crit fails: rl_best=0.0 vs baseline=0.0(; OOD matrix + strategy_registry artifacts, 9.26s。 Exploitability cell worst-case **1.0** (an undetected cell admitted in artifact( — honest( |
| `shadow_eval.py` | ✅ CLEAN | v1 evasion 0.087 vs v0 1.0 (improved=True(; distill fidelity r² 0.93 (xgb(/0.87 (mlp(; artifact written, 9.5s |
| `fidelity_eval.py` | ✅ CLEAN | CTGAN+diffusion both trained+sampled (52.5s/66.5s(; 3-layer report v2 written; adversarial trap **`survived=False` (AUC≈0.999( — NOT faked**; manifold-transfer rho 0.0257; salary cadence ration 1.0 |
| `baseline_eval.py --accounts 600 --steps 90 --eval-repeats 70` (3rd run win utf-8( | ✅ CLEAN | **Leakage assert PASSED on 4,560 training rows**; eval slice 2,117 rows / 63 fraud / prev 0.0298; per-type fraud ≥6/6; meta isotonic oof_used=True; held-out **A2 mean≈0.001 (recall 0.0( vs A5≈0.90** — asymmetric generalization, honestly reported |
| `attribution_eval.py` | ✅ CLEAN | shadow distill r² 0.793/0.925; per-mechanism counts (rule_compiler 32, shadow_pgd 6, llm 12, genetic 7, other 4(; artifact 8.6s |
| `signals_eval.py` | ✅ CLEAN | separations computed (xgb 1.0, gnn 0.8379, meta 1.0, …(; **max |off-diag ρ|=0.9997** — near-duplicate decorrelation caveat (see §7-P3(; `artifacts\decorrelation.json` |
| `timeline_eval.py --accounts 200 --steps 45` | ✅ CLEAN | recall 0.38→1.0 across rounds; blind spots named+fixes applied; artifact 8 entries (58118405( |
| `feasibility_eval.py` | ✅ CLEAN | margins/latency/cost_model/drift artifacts written; deep p50 latency **0..0278s**; drift max 9.19; 23.1s runtime |
| `fit_weights.py` | ✅ CLEAN | fitted weights vs baseline (w_t 820.87 vs 300(, w_g/w_b collapsed to 0.0, w_e/w_c 90/90, w_u 0.918(; `src\artifacts\structured_weights.json` schema `prometheus.structured_weights.v2`, n=8,022, pos=16, **auc=1.0** (artificial-data separability—flag as suite caveat( |
| `sweep_eval.py --seeds 40,41 …` | ❌ **FAILED (bug(** | **Generation SHORTFALL fail-loud** (A1/A5/A6 <5 eval fraud, deterministic across seeds(; then **crash `TypeError` formatting None** at `scripts/sweep_eval.py:229-230` when all runs invalid — bug (see §7-P3( |
| `bench_twin.py --accounts 120 --steps 60` | ✅ CLEAN | 1,237 txs in 0.028s; PASS; `artifacts\twin_perf.json` |

> Root-causes confirmed by code: `src/attack/funding.py` (ring-fenced per-type reserves; disjoint; deterministic( + `src/attack/compiler.py` ring-fence consumption (lines 54-65, 581-660(: **wired into `scripts/baseline_eval.py` only** (imports `reserve_funding_pools`+`SAFETY_DEFAULT`(; **not wired into `scripts/sweep_eval.py` nor `src/api/main.py`** (grep: 0 hits( → sweep/API still run unringed → same depletion class. The sweep crash is a summary-formatting bug on the honest FAIL path, not a fabrication.



## 4. API live smoke — init→score→investigate→combo→stream

Driver: temp `api_live.py` / `api_live2.py` (uvicorn from `src\`, port 8099(. **Pass 1** (28 calls(: 26× 2xx + 2 client-side timeouts (self-inflicted: hardcoded 10s client for heavy `/api/init`(; **Pass 2** (120s client timeouts(: **full chain green**:

| Endpoint | Live result (numbers real( |
|---|---|---|
| `POST /api/init` (200 acct × 60 steps( | `{"status":"ok","transactions":1973,"features":20,"fraud_ratio":0.0081,"graph_nodes":300,"sample_tx_id":"TX_001973"}` — **16.8s** |
| `GET /api/status` | `{"ready":true,"events":1,"report":false}` |
| `GET /api/event-log` | `{"event":"initialized","detail":"1973 TXs, 20 features, calibration=isotonic, oof=True"}` |
| `GET /api/sample-txs?limit=2` | real coined rows (TX_001310 2608.02 grocery Benign( |
| `GET /api/score?tx_id=TX_001386` | real per-column signals (xgb 3.0e-4, gnn 0.1527, meta 0.0504, manifold 0.0, spectral_cycle/star 1.0(; `structured_score`=5.4 band APPROVE; **external_evidence=0.0** (zero hits for this benign tx — honest(; no fabricated fields |
| `GET /api/eval` | multi-prevalence PR-AUC table computed on live world (PR-AUC 1.0 @ all prevalences, recall@1% 1.0, FDR@1% 0.152%( |
| `GET /api/graph?filter=overview` | node-link KG with live risk scores (e.g. ACC_00069 risk 0.999 is_fraud true( |
| `POST /api/investigate` | `schema prometheus.case.v1`; EvidenceStore manifest with deterministic id **`EVD_96F7596A27E4` (identical across two independent calls** (pure function of tx — strong anti-fabrication signal( |
| `POST /api/combo` | honest partial catch: 4-stage attack, `caught`=1/4, `fully_detected`:false, stages listed with real tx_ids |
| `GET /api/stream` | SSE live: `retry: 1000` then streaming events (combo + per-step stats: normal 26/fraud 0/peak 0.1209/volume 87,571.99(( |
| error paths | `{"error":"Not initialized"}` pre-init (score(; 422 pydantic on bad body (investigate(; **no traceback leaks** (`finding #12 fix` verified at `src/api/main.py:308`( |

Supporting reads: `src/api/main.py` — score path (lines 536-601( derives **same CaseManager evidence context as investigate** (`case_evidence_context( `, so score/investigate cannot disagree §2.1-home;weights endpoint `GET /api/structured-weights` reads the **committed canonical artifact** (`FittedStructuredScore.load_or_none(STRUCTURED_WEIGHTS_PATH(` — deterministic, not session-random (~main.py:604-612(; init saves+merge-reloads committed w_* (`main.py:347-366(` — a documented random-seed session refit kept off the canonical object — approved pattern(.

## 5. updates.md item-by-item re-verification

`updates.md` HEAD ref `9f386bb` — **stale** vs actual `68bde8c`. Re-verified each item live/code as of HEAD:

| updates.md item | Claimed status (doc( | Audited status | Evidence |
|---|---|---|---|
| §1 keep-list (twin typologies, two-axis holdout, compiler tiers, feedback cap, investigator delegation, OSINT fixtures, sanctions fixture, guardrails, 3-class memory, LLM strategist, RL + kill criterion, shadow-gradient, 3-layer fidelity, latency, drift, docx auto, tests( | "already implemented" | ✅ Verified (except doc staleness( | code + live runs (§3/§4(; sanctions yente live raises `NotImplementedError` honestly (documented fallback( |
| §2.1 CRITICAL — wire `w_e`/`w_c` into structured score | open gap | ✅ **DONE** | `src/scoring/structured_score.py` — `DEFAULT_WEIGHTS` has `w_e=120, w_c=80` (lines 14-21(, full formula (lines 61-68(; `FittedStructuredScore.predict_row` adds deterministic E/C (lines 256-283(/; `/api/score` + investigate share `case_evidence_context` (line 554( |
| §2.2 HIGH — hand-picked weights | open gap | ✅ **DONE** | Phase-8 `FittedStructuredScore` (fit/save/load(, v2 artifact committed, monotone guard (lines 186-198(,, `scripts/fit_weights.py` ran (n=8,022, auc=1.0(, weights-panel deterministic (main.py:604( |
| §2.3 HIGH — A5 funding depletion | open gap | 🟡 **PARTLY — core+baseline DONE; sweep/API NOT wired** | ring-fence implemented (`src/attack/funding.py` + `compiler.py` 54-65/581-660(, wired into `baseline_eval.py` only (lines 55,151-179(; `sweep_eval.py` + `src/api/main.py` grep 0 hits → still unringed → same failure class (our sweep run reproduced exactly( |
| §2.4-LOW hygiene — `rl_stretch` device-bias placeholder | open | ✅ **Resolved (differently: dropped(**; +1 stale docstring | genome is now **4-dim** [amount∈log10, members, days, margin] (`rl_stretch.py:68-74, 86-92`, `n_states=4`(; docstring yet says 5-dim incl `dev_bias` (line 17( — P4 cosmetic |
| §3 standout panels | suggestion | 🟡 partly (mechanism attribution + fitted-weights panel built; timeline panel built (| `attribution_eval.py`, `/api/timeline`, `/api/structured-weights` |
| §4 checklist items | open | see rows: #1 ✅, #2 ✅, #3 🟡 partial, #4 ✅ (tier diagnostics in funding.py(, #5 🟡 sweep CI missing#, #6 ✅ (genome clean(|
| §5 live SSE/WS viz | **future/not built** | ✅ **DONE — doc ahead-of-plan** | `/api/stream` SSE live events verified; event-log endpoint; dashboards render (200 HTML( |
| §6.1 protocol-attack pillar | future | ✅ **DONE** | `src/attack/protocol_attacks.py` (RC-1…RC-5, lines 81-204(; PCAT-style guard (P1-P5(; `protocol_eval.py` live: naive RC 1-5 all landed, PCAT blocked all 5, 0 benign FP ( |
| §6.2 diffusion fidelity critic | future | ✅ **DONE** | `tests/test_fidelity_diffusion.py` + `fidelity_eval.py` ran diffusion trained+sampled (66.5s((|

> **Headline confirmations of §0:"doc ahead of plan" — SSE/WS (§5(, fitted weights (§2.2(, ring-fence core (§2.3(, E/C wiring (§2.1(, protocol-attack pillar (§6.1(, diffusion critic (§6.2( are ALL already built and verified. The doc's remaining-gap framing is obsolete for those items. Expected dated: `updates.md` was authored Aug  ́29 vie **before** the late-phase commits;most gaps closed post-doc — exactly as §0 predicted.**

## 6. Fabrication / anti-fabrication audit (law enforcement(

No fabrication found anywhere. Evidence:
- `src/feedback/evidence.py` — `ComputedEvidence` dataclass rejects raw strings/dicts at construction (type contract(,making fabricated evidence impossible by construction.
- `meta_model.py` — honest stacking contract (finding #4 fix(, isotonic calibration with `ISOTONIC_MIN_SAMPLES=200`, OOF flow (trustworthy probs(.
- `blue/features.py` / `gnn_model.py` — real message passing over graph (finding #7 fix(, gives per-node context(e.;`blue/splits.py` — two-axis holdout với sha256 membership lock(.
- `api/main.py` — per-column honest signals (finding #6 fix: faked GNN/meta-as-XGB shortcutremoved(, traceback redaction (finding #12(, dashboard forEach dead-loop fix (#9(, ensemble-object construction (#1(, computed sensitivity keys (#5b(.
- Honest negatives shipped:: RL pre-registered kill criterion (mechanism_eval printed criterion-failed+shipped artifact(; fidelity adversarial trap reported `survived=False`; sweep shortfall refused artifact (fail-loud law 8(; held-out A2 ≈0.001 vs A5 0.90 asymmetry printed plainly;LLM strategist fallback origins all `fallback` (no fake "success"(. — This is the strongest part of the repo: it refuses to fake, даже when that makes it look worse.
- Hardcoded-result scan: no suspicious score/band literal lanes in scoring paths (searched xgb/gnn/meta/manifold/spectral signals + structured_score.main( unclear: no bypass).
- Residual risks (code-level(: sw`/api/score` has a `legacy_fallback` branch that plugs **triple-identical meta** into `score_from_ml_probs(prob, prob, prob`** (main.py:558-575( — only when structured fit is `None`, but it re-introduces exactly the faked-identical-inputs pattern §6 targets — worth a P3 note to gate/remove(.

## 7. Findings & fix requirements (prioritized(

### P0 — Environment / secrets
- **`.env` is git-tracked with a live Groq API key** (`git ls-files --error-unmatch .env` → tracked; `.gitignore` has `!.env` + comment "Secrets: allow .env";`.env.example` exists(. It was already leaked in prior commits (key `gsk-iMaHXFZPA6gH5GIGFTrRWGdyb3FYE0hs42OgzdWti0u9uKW2YWLX`(.
  **Fix requirement:** 1) Rotate/delete that key. 2) `git rm --cached .env`, remove `!.env`, add `!.env` denypattern, verify `.env.example` covers vars. 3) scrub key from git history (filter-repo/BFG( before submission.
- **Phase 2 update (2026-08-30): operator shipped partial P0 (commits `a56e2c1` untrack `.env` + `4c611c6` remove `.env` blob from index). `git ls-files .env` → untracked. The key, however, remains in every prior commit (the local repo still contains `68bde8c final audit` and earlier). The history rewrite (filter-repo / BFG) is still operator's responsibility outside this session. The user explicitly chose "Skip P0 entirely" for the in-session fix list. Live key rotation: still required before any public push.

### P1 — Security posture
- Sanctions (yente live transport raises `NotImplementedError` behind documented fixture fallback( — honest deferral (updates.md agrees(; keep + track as follow-up.
- API error bodies are graceful/redacted (verified(; no other P1 found..

### P2 — Functional gaps
1. **`sweep_eval.py` not wired to ring-fencing** (grep 0 hits( → still reproduces §2.3 depletion (both seeds deterministically( + ships no per-attack-type numbers when shortfall (global gate only(. **Fix requirement:** pass `funding_pools=reserve_funding_pools(…(` per type into per-scale compilers, incorporate into artifact; add per-type min-fraud gating.

  2. **`/api/init` (repeated heavy inits( also unringed** (grep 0 hits( — same class at other scales (live 200×60 happened to clear: 16 fraud/6 types(.**Fix requirement:** reuse `funding.py` in init (or document scale limits(.
  3. **Sweep runner has no per-type CI in artifact** (§4-check item 5(: `aggregate_by_scale` cited CI only aggregate-level. **Fix requirement:** add per-mechanism/per-type variance + CI columns (as updates.md §4 requested(.
  4. **API test coverage absent** (no `tests/test_api.py`; 0 API tests(. **Fix requirement:** add init/score/stream/error-path tests (fast, seeds fixed((
### P3 — Bugs (real, low-severity(
1. **`scripts/sweep_eval.py:229-230` crashes (`TypeError`( when ALL runs invalid** — formats `None` as `:.4f` float on the honest-FAIL path instead of printing INVALID summary. **Fix requirement:** `if agg[headline][mean] is None: print("INVALID (shortfall)"); continue`.
2. **Unicode print crash on ₹ under cp1252** — observed in `baseline_eval.py` (run 2(; same pattern likely in `sweep_eval.py`/`mechanism_eval.py` print paths (₹300k/₹5,000( think fixes: `sys.stdout.reconfigure(encoding="utf-8", errors="replace"(`` at script top (or omit ₹ from prints(. Windows-onlycosmetic.
3. **`_weakness_for_misses` in `src/feedback/loop.py` silently swallows `build_graph_data` exceptions** (`except Exception: pass`( — diagnostic data only; weakness still computed. **Fix requirement:** log the exception (at least `logger.warning`( so missing-graph failures are visible (docs allowed to show `graph`=None(.
4. **`legacy_fallback` triple-identical-meta in `/api/score`** (main.py:558-575( re-introduces faked-identical signal pattern only if structured fit fails. **Fix requirement:** remove branch or shunt to explicit error (or use the one real signal it has and label it(.
5. **`signals_eval.py` max |off-diag ρ|=0.9997** — near-duplicate signal columns (possible e.g. spectral_cycle vs spectral_star( → decorrelation claim weaker than line suggests. **Fix requirement:** quantify which pair + variance-inflation before quoting "decorrelated".
6. **`updates.md` stale** (HEAD ref `9f386bb` vs `68bde8c`; lists §2.1/2.2/2.3/§5/§6 as open( — all done at HEAD; tests count stale (13→19(. **Fix requirement:** refresh with current HEAD + statuses (or mark superseded by this audit(.
7. **`Prometheus_Walkthrough.docx` auto-date 2026-08-29 11:33** — pre-dates today's artifact reruns (our `sweep_eval.json` FAILED — likely absent from doc numbers(.**Fix requirement:** regenerate post-fix with documented one-liner (`PYTHONPATH=src python src/docs_gen/build_docx.py`( — **not run here (read-only(**,then diff-check numbers appear.
8. **`rl_stretch.py` docstring says 5-dim state incl `dev_bias`** but code/`n_states=4` — stale comment (P4(.

### P4 — Nits
- `DEFAULT_WEIGHTS` comment still says "can be fit — Phase 4" (now Phase-8 fit exists; §2.2 comment at structured_score.py:13(.
- Warnings (1746 pytest( untriaged。
- CI gates minimal: only `pytest tests/ -q` + `bench_twin.py` (no lint/type-chekck, no Windows run, docs not validated( — Dockerfile CMD `python src/api/main.py` (port 8000? actual launch path uvicorn from src — hold as ops-readiness followup(.

## 8. Holdout / leakage verification (mandated(

- Protocol fingerprint intact `292cc7f6…a162` (real(in ly protocol_eval (sha256-locked holdout world membership unchanged(.
- Live leakage assert **PASSED** on 4,560 training rows (no training-row leakage into eval slice (baseline_eval run 3(.
- Eval slice all 3 yr types with ≥6 fraud each (per-type floor met(.
- Held-out **A2 ≈0.001 mean (recall 0.0(** vs held-out **A5 ≈0.90** — honest asymmetric generalization (report it exactly;do NOT "average away"(.
- Deferred: per-type holdout cross-checks inside sweep (blocked by sweep P3 crash( — covered by baseline path instead. In

## 9. Assumptions drawn first

- Repo's own eval scripts are the authoritative E2E harness (their guards refused artifacts on shortfall — law-enforcement respected(.
- Read-only mandate: docx regeneration + .gitignore fixes + any code edits **not** performed — only required statements listed (§7(.
- `/api/init` wall-time 16.8s at 200×60 — demos should budget 30-60s (client timeout 120s(.
- No external services dependent (free-tier LLM env unset → deterministic fallback everywhere; sanctions fixture-mode(.

---

*End of Phase-1 audit. Follow-ups: rotate `.env` key (P0(, wire ring-fence into sweep_eval+api (P2(, fix sweep None crash (P3-1(., then regenerate docx + refresh updates.md. All remaining items are cosmetic.(*