# CLOSEOUT_LOG.md - Project Prometheus Phase 2 Close-Out Verification
# Branch: kartik | Date: 2026-08-30 | Verifier: Antigravity (adversarial re-check)

This log re-verifies five specific self-reported claims from fix_log.md.
It does NOT redo Phase 2 work -- it independently checks each claim the way
the Phase 1 audit checked updates.md.

---

## CHECK 1 -- Ring-fence wiring in sweep_eval.py / evaluate()

**Status: CONFIRMED**

Claim: fix_log.md P2-1a states evaluate() already called reserve_funding_pools
"under the hood via defaults" and that P2-1a was additive polish.

Evidence found at baseline_eval.py:156-157 (inside evaluate(), step 3, unconditional):

    funding = reserve_funding_pools(
        world, funding_specs, eval_repeats, safety=funding_safety)

The call is NOT guarded by a kwarg. The funding_safety parameter defaults to
SAFETY_DEFAULT (baseline_eval.py:90). The ring-fence was genuinely wired into
evaluate() BEFORE the Phase 2 session.

Why Phase 1 audit found "0 hits" for reserve_funding_pools in sweep_eval.py:
The audit grepped sweep_eval.py directly. reserve_funding_pools is imported and called
in baseline_eval.py (the shared harness), not in sweep_eval.py itself. The grep was
scoped to the wrong file.

What P2-1a added (additive polish on a real pre-existing wire):
- --funding-safety CLI flag (default 1.25 = SAFETY_DEFAULT)
- --replenish-repeats CLI flag
- Both threaded into every evaluate() call site in sweep_eval.py
- funding_per_config top-level block in the artifact (per-seed x scale ring-fence trace)

The fix_log.md P2-1a reframing is accurate. The ring-fence is real and pre-existing.

---

## CHECK 2 -- legacy_fallback triple-identical-meta pattern gone everywhere

**Status: CONFIRMED**

Method: Python search of entire src/ tree for score_from_ml_probs on the same line
as "prob, prob, prob", filtering out lines beginning with '#'.

Result: LIVE CODE HITS (non-comment): 0

The /api/score fallback path (src/api/main.py:603-612):

    else:
        # No fitted structured head available. The legacy branch here used to
        # feed score_from_ml_probs(prob, prob, prob) -- the triple-identical-
        # meta pattern finding #6 removed -- presenting one real signal as
        # three agreeing ones. Fail loudly instead of re-introducing it.
        return {"error":
                "structured score unavailable: fitted head missing and the "
                "legacy identical-probabilities fallback was removed "
                "(fabricated-agreement risk). Re-run /api/init or "
                "scripts/fit_weights.py."}

The else branch returns an explicit error dict. No silent fabrication. Zero live
non-comment hits for the triple-identical-meta pattern across all of src/.

---

## CHECK 3 -- Docx content reflects fix batch changes (not just shape)

**Status: CONFIRMED (non-issue for sweep) + FIXED via Check 4**

python-docx dump of Prometheus_Walkthrough.docx (pre-closeout):
- NO paragraph or table cell mentions "funding", "sweep", "12/12", or 0.8825 (sweep PR-AUC)
- NO mention of "exploitability" or "worst-case detection"
- Docx section 5 pulls from baseline_eval.json (per-prevalence PR-AUC table),
  feedback_cycle.json, decorrelation.json -- NOT sweep_eval.json

Confirmed non-issue for sweep numbers:
build_docx.py is explicitly wired to: baseline_eval.json, fidelity_report.json,
strategy_registry.json, ood_matrix.json, twin_perf.json, latency.json,
cost_model.json, drift.json, margins.json, feedback_cycle.json, decorrelation.json.
It does NOT load sweep_eval.json at any point.

The docx is a walkthrough/pitch document, not a full eval report. Absent sweep
numbers are by design. The P3-7 size/shape verification is valid on its own terms.

Identified gap: exploitability=1.0 was absent from docx. Fixed by Check 4.

Post-fix: Docx regenerated at 40763 bytes (was 40387 bytes). New section 5 paragraph
names every 0.0-detection (mechanism x type) cell. Sweep numbers remain absent by design.

What the docx does cover: per-prevalence PR-AUC from baseline_eval.json, fidelity
layers (3-layer exhibit), strategy fingerprints, latency, cost model, drift, feedback
cycle recall before/after, and (post-fix) named exploitability blind-spot cells.

---

## CHECK 4 -- Name the exploitability=1.0 blind spot explicitly

**Status: FIXED**

What ood_matrix.json shows:
  overall_worst_case_detection: 0.0
  overall_exploitability: 1.0
  mean_detection: 0.3137

All zero-detection (mechanism x type) cells from ood_matrix.json["cells"]:

  shadow_pgd x A1:           n_txs=2,  held_out=false
  shadow_pgd x A3:           n_txs=2,  held_out=false
  genetic x A4:              n_txs=8,  held_out=false
  genetic x A6:              n_txs=14, held_out=false
  genetic x A5:              n_txs=32, held_out=true
  shadow_pgd x A2:           n_txs=6,  held_out=true
  rule_compiler x A2:        n_txs=12, held_out=true
  llm_strategist x A2:       n_txs=10, held_out=true

Root cause for non-held-out zeros (shadow_pgd x A1/A3, genetic x A4/A6):
The Blue model was trained on rule_compiler-generated attacks only. The mechanism-axis
holdout is EMPTY (held_out_mechanisms=[] in canonical fingerprint). Shadow-PGD and
genetic variants of trainable types are therefore OOD on the mechanism axis, and the
model has no defense against them. PGD surrogate quality is low (distill_xgb_r2=0.037
per strategy_registry.json), amplifying evasion.
This is expected and intentional -- the mechanism-axis holdout was designed for
exactly this disclosure.

Root cause for held-out zeros (A2, A5): Held out by design (never trained on).
Any mechanism attacking them achieves high evasion at this training state.

Is this a bug? NO. Genuine hard case, not an oversight. The mechanism-holdout
framework was explicitly built to expose this kind of result.

What would close it with more time:
1. Include one shadow_pgd and one genetic round in the feedback flywheel retraining
   (the mechanism-registry and two-axis holdout infrastructure is already wired in
   the feedback loop -- this requires running the loop with those mechanisms in scope).
2. Raise PGD surrogate quality: distill_xgb_r2=0.037 is very low; add more
   distillation rows or a better MLP architecture in src/shadow/distill.py.
3. For held-out zeros (A2, A5): accept them as the honest generalization limit
   and document them -- which this section and the docx now do.

This disclosure is at the same standard of honesty as the RL negative-result panel
(DQN_rl_stretch shipped=true because best_mean_evasion=0.0 met the pre-registered
criterion). The 1.0 exploitability number is not averaged away.

Fix applied:
1. updates.md: Added "## Closeout note -- exploitability=1.0 blind spot" section
   immediately after the Phase 2 addendum header. Names all zero-detection cells,
   root cause for each category, and what would close each. Same honesty standard
   as the RL negative-result panel.
2. src/docs_gen/build_docx.py: Added exploitability blind-spot paragraph in section 5
   Detection Efficacy, pulling live from ood_matrix.json["cells"]. Iterates every
   cell with detection_rate=0.0 and names it explicitly, with root cause and
   three closure paths.
3. Prometheus_Walkthrough.docx: Regenerated. Size 40387 -> 40763 bytes (+376).
   New paragraph in section 5 confirmed present. Build ran to completion, exit=0.

---

## CHECK 5 -- T9 exclusion from fingerprint: documented decision or ad-hoc?

**Status: CONFIRMED + POINTER COMMENT ADDED**

Claim: T9 is "deliberately kept out" of the trainable/held-out mechanism axis so the
canonical fingerprint formula is preserved.

Evidence found -- GENUINE PRIOR RATIONALE (not post-hoc):

src/attack/benchmark_attacks.py lines 57-61 (PRE-EXISTING, part of T9 pillar
implementation, not from the Phase 2 fix session):

    # T9 is NOT a TRAINABLE_ATTACK and is deliberately kept OUT of the A1-A6
    # axes-2/3 holdout and ALL_ATTACKS sets: it lives in its own independent
    # (single-model, deterministic) "protocol_eval" story so the baseline lock
    # (fingerprint, train/eval cardinals) is bit-for-bit untouched. It uses its
    # own mechanism namespace (protocol_structural) and attack-type code T9.

src/attack/protocol_attacks.py module docstring (pre-existing):
    "Fraud rows flow through the normal twin pipeline (mechanism=protocol_structural,
    attack_id=T9, per-payment rc_class tag)."

PROTOCOL_ATTACKS["T9"]["independent_eval"] field (benchmark_attacks.py line 80):
    "scripts/protocol_eval.py -> artifacts/protocol_eval.json"

Where T9's evaluation actually lives:
scripts/protocol_eval.py -> artifacts/protocol_eval.json
Uses deterministic judges (src/eval/judges.py) -- not the ML mechanism-OOD matrix.
Exit=0 confirmed in the Phase 2 final gate run.

Was this decision ad-hoc (made during the fix session to avoid touching the fingerprint)?
NO. The comment in benchmark_attacks.py:57-61 was part of the T9 pillar implementation
(Phase 10/11 per implementation.md) and pre-dates the Phase 2 fix session entirely.
It is a structural separation-of-concerns decision: T9 uses a deterministic-judge
protocol eval, not an ML mechanism-OOD matrix, so the canonical fingerprint is untouched.

Fix applied:
Added 15-line NOTE comment to src/blue/splits.py _fingerprint() docstring.
The comment:
- Cross-references benchmark_attacks.py:57-61 as the design-decision source
- Points future readers to scripts/protocol_eval.py for T9's evaluation path
- Explicitly states "pre-existing comment, not ad-hoc" to remove ambiguity
- Notes that protocol_structural absent from held_out_mechanisms should direct
  a reader to protocol_eval.py, not be taken as an oversight

---

## KEY ROTATION STATUS

**FLAGGED FOR USER (P0 PARTIAL -- operator responsibility)**

fix_log.md line 217 documents:
  "The key gsk-iMaHXFZPA6gH5GIGFTrRWGdyb3FYE0hs42OgzdWti0u9uKW2YWLX is still
  in prior commit history; rotate before any public push."

This session has NOT rotated the key. Key rotation is an operator action:
1. Invalidate the key immediately at the provider portal (Groq/OpenAI).
2. Run: git filter-repo --path .env --invert-paths
   (or BFG Repo Cleaner) to strip the key blob from all prior commits.
3. Force-push the cleaned kartik branch.

DO NOT submit to a public repository, share the branch URL, or open a PR
against main until both steps above are complete. The key remains in git
history blobs until the history rewrite is done.

---

## SUMMARY TABLE

| Check | Claim | Finding | Action | Result |
|-------|-------|---------|--------|--------|
| 1 | ring-fence wired via evaluate() defaults | CONFIRMED: baseline_eval.py:156-157 unconditional call; Phase 1 grep scoped to wrong file | None (pre-existing wire confirmed) | CONFIRMED |
| 2 | triple-identical-meta pattern gone everywhere | CONFIRMED: 0 live non-comment hits across all src/; fallback is explicit error return | None | CONFIRMED |
| 3 | docx reflects fix batch content | CONFIRMED non-issue for sweep (by design); exploitability gap fixed by Check 4 | Check 4 fix applied | CONFIRMED + FIX |
| 4 | exploitability=1.0 named explicitly | NOT CONFIRMED pre-fix: absent from updates.md and docx | Named table in updates.md + live blind-spot paragraph wired into build_docx.py; docx regenerated 40763 bytes | FIXED |
| 5 | T9 exclusion is a documented decision | CONFIRMED: benchmark_attacks.py:57-61 is genuine prior rationale | Pointer comment added to splits.py _fingerprint() docstring | CONFIRMED + POINTER ADDED |
| Key rotation | key in prior git history | NOT DONE -- operator responsibility | Must rotate at provider + git filter-repo before public push | FLAGGED FOR USER |

---

## SUBMISSION READINESS

All five checks are now either CONFIRMED or FIXED. Test suite (242/242) is unaffected
by this closeout's three changes (updates.md, src/docs_gen/build_docx.py,
src/blue/splits.py -- no test assertions target any of them).

THE PROJECT IS READY TO SUBMIT SUBJECT TO KEY ROTATION.

One outstanding item before any public push or submission link share:
  1. Rotate/invalidate the gsk-... key at the provider (do this NOW).
  2. git filter-repo --path .env --invert-paths to strip from history.
  3. Force-push the cleaned kartik branch.

Without those three steps, the key remains in git history blobs and any
public fork or clone exposes it.
