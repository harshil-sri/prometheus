"""Monotone-constrained fitting of the weighted-formula w_* weights.

Updates.md 2.2: replace the hand-picked w_* with a constrained/monotonic
regression (non-negative least squares) of the standardized evidence terms
T, G, B, E, C, U against the in-sample evaluation targets. Lives in the
`scoring` package so both scripts/fit_weights.py (generation) and the test
suite (schema/determinism gates) import the SAME code.

Design rules (documented in the artifact's w_fit block):
  * Standardize each column on the REAL dataset rows only — the calibration
    grid cells must never shift the reference scale.
  * Non-negativity (scipy.optimize.nnls) ⇒ every w_* ≥ 0 ⇒ the reduced
    formula is monotone: raising an evidence term cannot lower the raw score.
  * Reachability rescale: raw-std-space back-transform is rescaled so that
    max(raw) = (sum of positive w_*) - w_u lands exactly on BAND_MAX. That
    guarantees every decision band on the 0-1000 scale is reachable.
  * Returns diagnostics (column std, degenerates, residual) so the artifact
    EXPLAINS a term collapsing to 0 (near-zero variance / collinearity on
    the synthetic twin) instead of silently omitting it.
"""

from typing import Any, Dict, List, Tuple

import numpy as np
from scipy.optimize import nnls

BAND_MAX = 1000.0
FORMULA_TERMS = ["w_t", "w_g", "w_b", "w_e", "w_c", "w_u"]
assert len(FORMULA_TERMS) == 6

# Calibration grid: rows spanning the E/C/uncertainty space the real rows
# never cover, so w_e/w_c/w_u are identifiable and DECLINE is reachable.
# Columns: T, G, B, E, C, U → target (0-1000).
# Model the grid pins: the ML prior supplies the base (full prior = 700,
# i.e. strong but not alone-DECLINE); E and C are ADDITIVE kill-shots that
# push a suspicious prior across the 700 line (everything = 1000); a single
# evidence term alone (E or C without any prior support) is NOT auto-decline
# (250); and maximal model disagreement pulls a full prior down (700→500) so
# w_u is a real penalty. Feasible + monotone, documented in w_fit.
GRID: List[Tuple[float, float, float, float, float, float, float]] = [
    (0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.0),     # clean
    (1.00, 1.00, 1.00, 0.00, 0.00, 0.00, 700.0),   # full ML prior (not alone)
    (1.00, 1.00, 1.00, 0.00, 0.00, 1.00, 500.0),   # full prior, max disagreement
    (0.00, 0.00, 0.00, 1.00, 0.00, 0.00, 250.0),   # E alone (no prior support)
    (0.00, 0.00, 0.00, 0.00, 1.00, 0.00, 250.0),   # C alone (no prior support)
    (1.00, 1.00, 1.00, 1.00, 1.00, 0.00, 1000.0),  # everything → kill-shot DECLINE
]


def fit_w_star(X, y, n_real: int, grid: List[Tuple[float, ...]] = GRID) -> Tuple[Dict[str, float], Dict[str, Any]]:
    """Fit the six weighted-formula weights by non-negative least squares.

    Sign convention — the weighted formula subtracts the uncertainty penalty:

        R = w_t·T + w_g·G + w_b·B + w_e·E + w_c·C − w_u·U        (w_u ≥ 0)

    so the DESIGN negates the U column:  D = [T, G, B, E, C, −U]. The nnls
    coefficient for that design column IS w_u, a non-negative penalty that is
    monotone-DECREASING in U (raising uncertainty can never raise R). A
    naive +U design (coefficient = w_u·U) would contradict the formula and
    make the "max disagreement pulls a full prior down" calibration cell
    mathematically unreachable, so it is not an option.

    Returns (fitted w dict, diagnostics dict). See module docstring.
    """
    X = np.asarray(X, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    SIGN = np.array([1.0, 1.0, 1.0, 1.0, 1.0, -1.0])
    D = X * SIGN
    mu = D[:n_real].mean(axis=0)
    s = D[:n_real].std(axis=0)
    flat = s < 1e-12
    s_safe = np.where(flat, 1.0, s)
    Xs = (D - mu) / s_safe
    sol, residual = nnls(Xs, y)
    w_raw = [float(sol[i] / s_safe[i]) for i in range(len(sol))]

    pos_sum = sum(v for i, v in enumerate(w_raw) if i != 5)
    w_u = w_raw[5]
    max_raw = pos_sum - w_u
    if max_raw <= 0.0:
        raise ValueError(
            f"fitted weights have non-positive reachable max raw "
            f"({max_raw:.2f}) — can never reach a decision band. Refusing "
            f"to persist a broken formula.")
    scale = BAND_MAX / max_raw
    w_raw = [v * scale for v in w_raw]
    w = {term: round(float(v), 4)
         for term, v in zip(FORMULA_TERMS, w_raw)}

    terms = ["T", "G", "B", "E", "C", "U"]
    diagnostics = {
        "design": "D = [T, G, B, E, C, -U] so the nnls coefficient of "
                  "column 5 IS w_u (penalty, monotone-decreasing in U)",
        "real_row_column_std": {terms[i]: round(float(s[i]), 4)
                                for i in range(len(terms))},
        "degenerate_on_real_rows": [terms[i] for i in range(len(terms))
                                    if bool(flat[i])],
        "reachability_scale": round(float(scale), 6),
        "max_raw": round(float(max_raw), 4),
        "residual": round(float(residual), 6),
        "grid": [{"t": r[0], "g": r[1], "b": r[2], "e": r[3], "c": r[4],
                  "u": r[5], "target": r[6]} for r in grid],
    }
    return w, diagnostics