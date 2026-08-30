# Project Prometheus — Work Plan (kartik branch audit, Aug 29 2026)

> **Phase 2 status addendum (2026-08-30):** this document was authored against
> HEAD `9f386bb` and lists items as "open" that are now built and verified.
> Authoritative live status as of HEAD `68bde8c` + the 2026-08-30 fix batch
> is in `AUDIT_REPORT_PHASE1.md` (item-by-item re-verification, §5 of the
> report) and `fix_log.md` (every change in this batch with verification).
> Test count is now **19 test files / 242 passed** (the 12 new API tests live
> in `tests/test_api.py`; the 9-line root `test_api.py` smoke was removed).
> `implementation.md` Phase 0-8 status board records the construction order;
> the Phase 2 fix-list and final gates are in `fix_log.md`. **This document's
> §1 keep-list is now stale**; the items below are re-verified as implemented
> in the Phase 1 audit (the column now reads "verified" rather than
> "keep-as-is").

Source of truth for this document: direct inspection of `harshil-sri/prometheus`
branch `kartik` (HEAD `9f386bb`), cross-checked against `session.md` (the v3
planning doc + technical design spec). This is not a re-statement of the plan —
every item below was verified by reading the actual source.

---

## 0. Headline finding

Most of what `session.md` calls "future work" or lists as a gap is **already
built**, mainly via a large commit (`f43bc83`) that landed before the
`daksh`/`kartik` split. The project is materially ahead of its own plan in
several places (see §1). Treat this document as the punch-list for the
remaining ~15%, not a rebuild plan.

---

## 1. Already implemented — keep these, do not rebuild

| Component | File(s) | Note |
|---|---|---|
| Stateful Financial Digital Twin, 8 AMLSim typologies, open system | `src/twin/` | `margin_ratio` per-hop on scatter_gather; EXT_SALARY/EXT_BANK flows |
| Two-axis holdout (attack-type **and** generation-mechanism) | `src/blue/splits.py` | sha256 fingerprint lock on holdout membership — stronger than the plan's sketch |
| Attack Compiler, funding-aware entity selection | `src/attack/compiler.py` | kartik's fix: tiered funded-pool selection (100/50/20%) |
| Weakness-directed feedback loop, max-2-round cap | `src/feedback/loop.py` | |
| Investigator / Case Manager, Strix delegation pattern | `src/investigate/case_manager.py` | orchestrator never executes work itself, enforced by structure |
| OSINT signals (twin-derived synthetic namespace) | `src/investigate/osint_fixtures.py` | avoids any real-PII risk |
| Sanctions screening | `src/investigate/sanctions.py` | fixture mode live; yente live transport honestly raises `NotImplementedError` behind a documented fallback (not faked) |
| Agent security guardrails | `src/investigate/guardrails.py` | prompt-injection sanitization, secret redaction, HTTPS enforcement |
| Three-class memory (case/attack/defender) | `src/investigate/memory.py` | content-hash deduped |
| LLM-as-strategist mechanism | `src/attack/mechanisms/llm_strategist.py` | free-tier only, deterministic fallback, honest provenance tags |
| RL attacker with **pre-registered kill criterion** | `src/attack/mechanisms/rl_stretch.py` | DQN, potential-based reward shaping (Ng–Harada–Russell), willing to ship an honest negative result — genuinely strong methodology |
| Shadow-gradient adversarial mechanism | `src/shadow/distill.py`, `src/attack/mechanisms/shadow_pgd.py` | real surrogate distillation → PGD → materialized realizable transactions |
| Three-layer fidelity evaluation | `src/eval/fidelity.py` | statistical / behavioral / adversarial, all measured not claimed |
| Latency budget, honestly measured | `src/eval/latency.py` | |
| Drift monitoring | `src/eval/drift.py`, `artifacts/drift.json` | |
| `.docx` walkthrough, auto-built from artifacts | `src/docs_gen/build_docx.py` | can never drift out of sync with the code — better than the plan |
| Test coverage | `tests/` (19 files, 242 passed as of Phase 2 fix batch) | investigator, fidelity, mechanisms, shadow, blue, phase integrity, **API** (`tests/test_api.py`, 12 cases), funding, weights, protocol, feedback, events, etc. all covered. The 9-line root `test_api.py` smoke was removed in favor of the real pytest file. |

**Decision: keep all of the above as-is and build forward from here.**

---

## 2. Must-fix bugs / logical gaps (priority order)

### 2.1 — CRITICAL: Structured score is missing 2 of 6 terms; investigator evidence doesn't reach the score
- **Where**: `src/scoring/structured_score.py`
- **Problem**: only `w_t, w_g, w_b, w_u` exist. The plan's formula is
  `R = w_t·T + w_g·G + w_b·B + w_e·E + w_c·C − w_u·U`. `w_e` (external/OSINT)
  and `w_c` (campaign) are absent — even though `CaseManager` runs
  `SanctionsAgent`/`OsintAgent`/`SpectralAgent` and produces exactly this
  evidence. The rich investigation output currently has nowhere to land.
- **Fix**:
  1. Add `w_e`, `w_c` to `DEFAULT_WEIGHTS`.
  2. In `CaseManager`, map sanctions/OSINT hits → bounded `[0,1]` external-evidence score.
  3. Map `attack_signatures` memory matches (repeat campaign fingerprint) → campaign-evidence score.
  4. Wire both into `compute_structured_score()`.
- **Effort**: ~2-3 hours. **Priority: do this first** — a technical judge who reads both modules will notice the disconnect immediately.

### 2.2 — HIGH: Score weights are hand-picked, not fitted
- **Where**: `src/scoring/structured_score.py` (`DEFAULT_WEIGHTS`, `DEFAULT_WEIGHTS_PATH`)
- **Problem**: comment says "can be fit via constrained regression"; nothing fits them. `DEFAULT_WEIGHTS_PATH` exists but nothing ever writes to it.
- **Fix**: one-off script — monotonic/constrained regression (`scipy.optimize.nnls` or `sklearn.isotonic`) of standardized evidence terms against eval outcome labels; dump result to `DEFAULT_WEIGHTS_PATH`. Report the fitted weights *and the fact they were fitted* on the dashboard.
- **Effort**: ~half a day including validation.

### 2.3 — HIGH: A5 (scatter_gather) generation fails on seed/scale combinations outside the tested sweep
- **Root cause** (traced through code, not guessed):
  - A5 is the most expensive attack (₹300,000).
  - Eval runs A1→A6 × `--eval-repeats` sequentially against the **same twin `world`**, with **no stepping/replenishment** between attacks — every cash-out permanently exits money via `EXT_BANK`.
  - By the time A5 executes, A1/A4/A6 (also expensive) have already drawn down the funded upper tail of accounts.
  - kartik's funding-aware selection reads *live* balances correctly, but has no defense against **cross-attack-type depletion within a single eval pass** — it can only fall back to unconstrained selection when the funded pool is empty, which then gets clamped by the never-overdraft rule → too few fraud rows → fail-loud gate trips.
  - This is why the **committed sweep** (4 seeds × 3 tested scale configs) shows A5 at a clean 1.0 — those configs happen to have enough headroom — but other seed/scale combinations can hit the depletion wall.
- **Fix (in order of effort)**:
  1. Evaluate priciest attack types first (A4, A5) so they get first claim on the funded pool.
  2. **Ring-fence funding per attack type at the start of the eval phase** — partition/reserve a disjoint sub-pool of accounts sized to each type's amount requirement, seeded and deterministic. *(This is the real fix — do this.)*
  3. Insert one "recovery" step (trigger the twin's salary/injection pass) between `eval_repeats` iterations.
  4. Best long-term: run each `eval_repeats` iteration against a `copy.deepcopy`'d `world` so repeats don't compound depletion on each other.
  5. **Make funding failures loud**: log the funded-pool size at each tier (100%/50%/20%) *before* selection, per attack type, into the eval artifact. Right now a failure just says "not enough fraud rows" with no visibility into *why*.
- **Do #2 + #5 together.** #5 alone would have made this bug obvious in minutes instead of 4-5 manual seed sweeps.
- **Effort**: ~1 day for #2+#5; the rest are optional hardening.

### 2.4 — LOW: minor hygiene
- `src/attack/mechanisms/rl_stretch.py` — `device bias placeholder` comment on a genome dimension; confirm it's intentionally unused or wire it to an actual device-selection signal.
- Confirm `PROMETHEUS_SANCTIONS_URL` / yente live-transport path has a tracked follow-up ticket (already correctly deferred, not a bug).

---

## 3. Standout / differentiation ideas (for judges from industry)

1. **Blind-Spot Report as a timeline, not a snapshot.** Persist every retrain cycle's report; render blind-spot → generated-fixes → recall trajectory across *all* rounds run during development, not just the last one. Shows the loop actually ran repeatedly, not once for the demo.
2. **Feature the RL negative result on its own dashboard panel**, not buried. A rigorously pre-registered kill criterion with an honest negative result is something industry judges specifically respect — it signals real methodology over a cherry-picked demo.
3. **Mechanism + evidence-source attribution readout** — when an attack is caught, show *which generation mechanism* produced it (rule_compiler / shadow_pgd / llm_strategist) next to *which evidence source* flagged it (XGB / GNN / OSINT / sanctions). Sells "closes blind spots against unseen mechanisms" visually instead of in a paragraph.
4. **Fitted-weights transparency panel** (pairs with §2.2) — plot of the constrained regression next to the resulting weights. Cheap, high-credibility.

---

## 4. Backend / ML strengthening checklist

- [ ] Wire `E`/`C` evidence terms into structured score (§2.1)
- [ ] Fit score weights via constrained regression, stop hand-picking (§2.2)
- [ ] Ring-fence per-attack-type funding pools in eval (§2.3)
- [ ] Add funded-pool diagnostics to eval artifact for every attack type (§2.3)
- [ ] Extend `sweep_eval.json` with per-mechanism variance/CI, not just per-attack-type (raises "detection algorithm efficacy" evidence quality)
- [ ] Confirm `rl_stretch.py` genome dims are all live signals, not placeholders

---

## 5. Live visualization plan (red-team vs. blue-team, real time, for judges)

- **Transport**: replace the single synchronous `/api/demo/run` POST with **Server-Sent Events or a WebSocket** that streams each action (`compromise_account`, `large_transfer`, `cash_out`, `retrain_round`, `recheck`) as it happens, instead of returning one blob after 1-3 minutes.
- **Visual**: a node-link graph (accounts = nodes, transactions = animated edges) via D3/canvas. Color code: red = undetected fraud edge, amber = under investigation, green = legit. Animate each transaction as a pulse traveling along its edge as the SSE/WS event arrives.
- **Animation policy**: functional, not decorative — pulse/fade on new edges, subtle glow on the account currently under attack, a visible progress indicator during retrain. Avoid gratuitous motion; industry judges read restraint as maturity.
- **Blind-Spot Report**: animate the diagnosis and recall-before/after numbers into the same view when Beat 2 completes, rather than requiring a tab switch.
- **Effort**: a few hours to a day, given the event log already contains everything needed in order — this is a wiring/rendering task, not new backend logic.

---

## 6. GROUND-BREAKING ADDITIONS (research-grounded, do these to actually stand out)

Everything above is solid engineering hygiene. This section is different: it's
the part meant to make judges say "I haven't seen this before." Both items are
grounded in specific, dated, real, verifiable sources — cite them by name in
the `.docx`. Build these **inside the existing simulation** (the digital
twin's own agentic-checkout flow) — this is defensive security research
applied to your own sandbox, not an instruction to target any real live
platform.

### 7.1 — NEW PILLAR: Structural vs. Semantic Agentic-Commerce Protocol Attacks

**Why this is the standout move**: this Mastercard hackathon is judged by
people who work at Mastercard. Mastercard launched a real live product called
**Agent Pay** (April 2025, "Agentic Tokens" extending MDES) that lets AI
agents check out on a user's behalf. Visa has the parallel **Trusted Agent
Protocol**, and Google has **AP2** (Agent Payments Protocol, Sept 2025, 60+
launch partners including Mastercard). This is the actual current frontier of
"GenAI payment fraud" — not a hypothetical.

A July 2026 paper — **"Protocol-Level Attacks on Agentic Commerce Platforms:
A Cross-Platform Taxonomy, AIP-Bench, and Unified Defense"** (arXiv
2607.21824, Louck, Ariel University) — tested three real agentic-commerce
platforms (Google's AP2, Fetch.ai, CoralOS) and found **33 vulnerabilities**
that work 100% of the time *regardless of which AI model is running the
agent*. It formally splits attacks into two classes:

- **Structural attacks** (RC-1 through RC-5): protocol/plumbing bugs —
  unsigned registry responses, payment destinations trusted from an
  untrusted source, credentials leaked via logs/URLs, payment
  check-then-execute races (TOCTOU), missing authorization checks. These
  succeed on *any* model, aligned or not. No amount of "better AI" fixes them.
- **Semantic attacks** (RC-6): the familiar prompt-injection kind — success
  depends on the model; the paper shows this concretely (weak/cheap models:
  99-100% attack success; frontier aligned models: 0%).

Almost every other team will only build the semantic kind (fake merchant
site, phishing text, prompt injection). Almost nobody will build the
structural kind, because it requires knowing this paper exists.

**What to build (concrete, scoped for the time you have):**

1. **Model an agentic-checkout flow inside the twin**: an "agent" entity that
   holds a scoped payment credential and can complete checkout with a
   merchant without a human step, modeled after the real Mandate pattern
   (Intent → Cart → Payment, each a signed object).
2. **Add attack type T9 — "Protocol-Structural Agentic Commerce Attack"**,
   implemented as concrete sub-cases mapped 1:1 to the paper's root causes:
   - **RC-1 (unsigned registry content)**: a rogue "merchant" entry injected
     into the twin's merchant list with no signature check — the agent trusts
     it and completes checkout with a fraudulent payee.
   - **RC-2 (untrusted payment destination)**: a federation-style call
     returns an attacker-controlled payout account, trusted without identity
     binding.
   - **RC-3 (credential in an observable channel)**: an agent's session
     secret ends up in a log/URL and gets replayed by an attacker.
   - **RC-4 (payment TOCTOU race)**: two concurrent authorization requests
     against the same budget both pass the check before either completes —
     a double-spend.
   - **RC-5 (authorization scope not enforced)**: a request succeeds despite
     lacking the correct scope/identity for that action.
   - Tag all of these `mechanism="protocol_structural"` in
     `src/blue/splits.py`'s mechanism registry — this is a **third
     generation-mechanism axis** for your existing two-axis holdout, and it
     plugs directly into infrastructure you've already built.
3. **Grading**: use *deterministic* judges, not an LLM judge — a wallet-string
   match, a log-pattern regex, an event count, a status code — exactly as
   the paper does with AIP-Bench. This keeps grading honest and reproducible,
   consistent with your project's existing "measured, not claimed" ethos.
4. **Build the defense**: a small policy layer in front of your own
   `src/api/main.py`, modeled on the paper's **PCAT** (Protocol-level
   Commerce Agent Trust) — five checks, each individually simple:
   - **P1 Response integrity**: signed responses only; reject anything
     claiming to be from a registered merchant/registry that isn't signed.
   - **P2 Caller identity binding**: payment-destination-carrying requests
     must resolve to a verified identity, not be trusted blind.
   - **P3 Secure channel enforcement**: block credentials that show up in a
     URL/log; reject cross-origin state-changing requests without a token;
     validate redirect targets against an allowlist.
   - **P4 Atomic payment state**: wrap check-then-deduct in a single lock
     (Python `asyncio.Lock` or an atomic compare-and-swap) so two concurrent
     requests can't both succeed.
   - **P5 Tool-call authorization**: sensitive actions (checkout, issue
     credential) require a pre-registered caller identity header; reject
     otherwise.
5. **Report it like the paper does**: attack-success-rate before/after the
   defense, plus false-positive rate on benign traffic. This is a second,
   completely independent "before/after" story on top of your existing
   Blind-Spot Report — and it's the one most judges will not have seen
   anyone else attempt.
- **Effort**: 1.5-2 days for a scoped version (2-3 of the RC classes done
  well beats all 5 done shallow). RC-1 and RC-4 are the cheapest to build and
  the most visually convincing (a fake registry entry; a race that visibly
  double-spends).

### 7.2 — Upgrade the fidelity baseline from CTGAN to a diffusion model

Your plan's fidelity critique targets CTGAN as the thing "most other teams
will reach for by default." That's still true, but CTGAN is now the *easy*
strawman in the research literature — the field moved to **diffusion
models** (TabDDPM, and the improved TabSyn) as the actual state of the art
for tabular data, and there is now a **fraud-specific diffusion model**
published March 2026: **EmDT (Clustered Embedding Diffusion-Transformer)**,
arXiv 2603.13566, built specifically to generate realistic fraud rows for
imbalanced fraud datasets. There's also a directly relevant March 2026
benchmark paper, **"Synthetic Tabular Generators Fail to Preserve Behavioral
Fraud Patterns"** (arXiv 2604.13125), which is a stronger, more specific
version of the CTGAN-failure citation your plan already uses — cite this
exact paper instead of the vaguer "a 2026 benchmark" reference.

**What to build**: in `src/eval/fidelity.py`'s Layer 1/3 (statistical and
adversarial fidelity), add a diffusion-based critic (a small TabDDPM-style
model — a few hundred lines using an off-the-shelf implementation) alongside
the existing CTGAN critic, and report both. Being able to say "our twin's
behavioral fidelity holds up even against a diffusion-based critic, not just
the easier CTGAN one" is a stronger, more current fidelity claim than almost
anyone else will make.
- **Effort**: 0.5-1 day if using an existing lightweight TabDDPM
  implementation rather than writing one from scratch.

---

## 7. Priority order for remaining time

1. §2.1 — wire OSINT/campaign evidence into the score (2-3 hrs)
2. §2.3 — ring-fence A5/A4 funding + add diagnostics (1 day)
3. **§6.1 — Agentic-Commerce Protocol Attacks (RC-1 + RC-4 minimum) + PCAT-style defense** (1.5-2 days) — **highest differentiation-per-hour item in this whole document**
4. §2.2 — fit score weights (0.5 day)
5. §5 — live SSE/WebSocket visualization (0.5-1 day)
6. §6.2 — diffusion-model fidelity critic upgrade (0.5-1 day)
7. §3 — standout panels (RL negative result, mechanism attribution, fitted-weights transparency, protocol-attack before/after) (0.5 day)
8. §2.4 — hygiene pass

All of §1's existing work stays as-is.

## 8. Citations to use verbatim in the `.docx` walkthrough

- Louck, Y. (2026). *Protocol-Level Attacks on Agentic Commerce Platforms: A Cross-Platform Taxonomy, AIP-Bench, and Unified Defense.* arXiv:2607.21824.
- Mastercard Agent Pay ("Agentic Tokens" on MDES), announced April 29, 2025; Visa Trusted Agent Protocol, announced September/October 2025; Google Agent Payments Protocol (AP2), announced September 2025.
- *EmDT: Embedding Diffusion Transformer for Tabular Data Generation in Fraud Detection.* arXiv:2603.13566 (March 2026).
- *Synthetic Tabular Generators Fail to Preserve Behavioral Fraud Patterns: A Benchmark on Temporal, Velocity, and Multi-Account Signals.* arXiv:2604.13125 — use this in place of the vaguer "2026 benchmark" reference already in the plan.
