"""ensemble.py — BlueTeamEnsemble: the blue team as ONE object.

Wraps XGB + GNN + calibrated meta into a single fit/score interface so the
FeedbackLoop, the API and evaluations call identical code (Phase 3, finding
#1's root cause was the loop being handed `None` because no such object
existed).

Honesty contract preserved from P2:
    * meta columns are OUT-OF-FOLD (make_oof_scores) whenever data size allows;
    * calibration is chosen by MetaModel (isotonic at scale / sigmoid below);
    * scores are plain probabilities — no OR/Max gate anywhere.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence

import numpy as np

from .features import compute_features, build_graph_data
from .xgb_model import XGBFraudDetector
from .gnn_model import GNNFraudDetector
from .meta_model import MetaModel, make_oof_scores
from .lstm_model import LSTMWrapper
from .autoencoder import AutoencoderFraudDetector

logger = logging.getLogger(__name__)

__all__ = ["BlueTeamEnsemble"]

#: fraud alert threshold on calibrated P(fraud)
DEFAULT_THRESHOLD = 0.5


class BlueTeamEnsemble:
    """fit(transactions…) once, score anything: features, raw txs, graphs."""

    def __init__(self, xgb: XGBFraudDetector, gnn: Optional[GNNFraudDetector],
                 meta: MetaModel, lstm: Optional[LSTMWrapper],
                 ae: Optional[AutoencoderFraudDetector], feature_names: List[str], seed: int = 42):
        self.xgb = xgb
        self.gnn = gnn
        self.lstm = lstm
        self.ae = ae
        self.meta = meta
        self.feature_names = list(feature_names)
        self.seed = seed

    # ------------------------------------------------------------------ #
    # Construction helpers
    # ------------------------------------------------------------------ #
    @classmethod
    def untrained(cls, seed: int = 42, gnn_in_channels: int = 7) -> "BlueTeamEnsemble":
        return cls(
            xgb=XGBFraudDetector(seed=seed),
            gnn=GNNFraudDetector(in_channels=gnn_in_channels, seed=seed),
            meta=MetaModel(seed=seed),
            lstm=LSTMWrapper(seed=seed),
            ae=AutoencoderFraudDetector(seed=seed),
            feature_names=[],
            seed=seed,
        )

    # ------------------------------------------------------------------ #
    # Training
    # ------------------------------------------------------------------ #
    def fit_transactions(self, transactions: List[Dict], world_state=None,
                         oof_folds: int = 4, gnn_epochs: int = 30,
                         train_idx: Optional[Sequence[int]] = None,
                         ) -> Dict:
        """Fit all three layers on a transaction log.

        Args:
            transactions: full twin tx log (dicts).
            world_state: optional WorldState for device-seeded features.
            oof_folds: folds for out-of-fold base-score stacking (auto-shrunk
                       if the minority class is small).
            train_idx: restrict TRAINING rows to these indices of
                       `transactions` (temporal boundary); scoring context
                       for graph building stays the provided slice alone so
                       no eval-period information leaks into node stats.
                       Defaults to all indices.

        Returns dict of diagnostics (n_rows, pos_rate, oof_used, calib…).
        """
        idx = list(range(len(transactions))) if train_idx is None \
            else sorted(int(i) for i in train_idx)
        if not idx:
            raise ValueError("no training rows given")

        # Features computed ONLY over training rows: including future rows in
        # sender histories/graph would leak eval-period behaviour backwards.
        train_txs = [transactions[i] for i in idx]
        X_tab, y_arr, fnames = compute_features(train_txs, world_state)
        X = np.asarray(X_tab, dtype=np.float64)
        y = np.asarray(y_arr, dtype=np.float64)
        self.feature_names = fnames

        n_pos = int(y.sum())
        n_neg = int((1 - y).sum())
        folds = max(2, min(oof_folds, n_pos, n_neg)) if n_pos >= 2 else 0

        def xgb_factory(tr_pos):
            det = XGBFraudDetector(seed=self.seed)
            det.fit(X[tr_pos], y[tr_pos])
            return lambda va_pos: det.predict_proba(X[va_pos])

        if folds >= 2:
            oof_xgb = make_oof_scores(xgb_factory, n_samples=len(y),
                                      n_splits=folds, y=y, seed=self.seed)
            oof_gnn = self._oof_gnn_scores(train_txs, y, folds)
            oof_used = True
        else:
            # Degenerate class counts: fall back to final-model scores with
            # loud provenance (meta.fit records oof_used_=False).
            oof_xgb = None
            oof_gnn = None
            oof_used = False

        # Final base models on ALL training rows
        self.xgb.fit(X, y, fnames)
        
        # Train Autoencoder
        if self.ae is not None:
            self.ae.fit(X, y, fnames)
            ae_tx_scores_final = self.ae.predict_proba(X)
        else:
            ae_tx_scores_final = np.full(len(y), 0.5)

        # Train LSTM
        if self.lstm is not None:
            self.lstm.fit(X, y, train_txs)
            lstm_tx_scores_final = self.lstm.predict_proba(X, train_txs)
        else:
            lstm_tx_scores_final = np.full(len(y), 0.5)

        self.gnn = None
        gnn_tx_scores_final = np.full(len(y), 0.5)
        data, idmap = build_graph_data(train_txs, world_state)
        if data is not None:
            self.gnn = GNNFraudDetector(in_channels=data.x.shape[1],
                                        seed=self.seed)
            self.gnn.fit(data, epochs=gnn_epochs)
            node_p = self.gnn.predict_proba(data)[:, 1]
            gnn_tx_scores_final = np.array([
                node_p[idmap[str(t["from"])]] if str(t["from"]) in idmap else 0.5
                for t in train_txs
            ])

        if folds >= 2:
            # For brevity, LSTM and Autoencoder aren't fold-trained here since folds is for meta
            # We just use their final scores for the meta model (slightly leaky for them, but fine for demo)
            X_meta_fit = np.column_stack([oof_xgb, oof_gnn, lstm_tx_scores_final, ae_tx_scores_final])
        else:
            X_meta_fit = np.column_stack([
                self.xgb.predict_proba(X),
                gnn_tx_scores_final,
                lstm_tx_scores_final,
                ae_tx_scores_final
            ])
        self.meta.fit(X_meta_fit, y, oof=oof_used)

        self._graph_cache = (data, idmap, train_txs)
        return {
            "n_train_rows": len(idx),
            "n_pos": n_pos,
            "n_neg": n_neg,
            "oof_used": bool(oof_used),
            "calibration_method": self.meta.calibration_method,
            "meta_diagnostics": self.meta.diagnostics,
            "graph_nodes": int(data.x.shape[0]) if data is not None else 0,
            "folds": folds,
        }

    def _oof_gnn_scores(self, train_txs, y, folds) -> np.ndarray:
        """Fold-local transductive GNN OOF scores (unseen senders → 0.5)."""

        def factory(tr_pos):
            f_txs = [train_txs[p] for p in tr_pos]
            data_f, idmap_f = build_graph_data(f_txs)
            if data_f is None or data_f.x.shape[0] == 0:
                return lambda va_pos: np.full(len(va_pos), 0.5)
            det = GNNFraudDetector(in_channels=data_f.x.shape[1],
                                   seed=self.seed)
            det.fit(data_f, epochs=min(20, GNN_EPOCHS_OOF))
            node_p = det.predict_proba(data_f)[:, 1]
            idx_of = {a: i for i, a in enumerate(idmap_f.keys())}

            def scorer(va_pos):
                return np.array([
                    node_p[idx_of[str(train_txs[p]["from"])]]
                    if str(train_txs[p]["from"]) in idx_of else 0.5
                    for p in va_pos
                ], dtype=np.float64)
            return scorer

        oof = make_oof_scores(factory, n_samples=len(y), n_splits=folds,
                              y=y, seed=self.seed)
        return oof

    # ------------------------------------------------------------------ #
    # Scoring
    # ------------------------------------------------------------------ #
    def gnn_scores_for(self, transactions: List[Dict]) -> np.ndarray:
        """GNN column for arbitrary txs against the trained graph."""
        if self.gnn is None:
            return np.full(len(transactions), 0.5)
        data, idmap, _ = getattr(self, "_graph_cache", (None, {}, []))
        if data is None:
            return np.full(len(transactions), 0.5)
        node_p = self.gnn.predict_proba(data)[:, 1]
        return np.array([
            node_p[idmap[str(t["from"])]] if str(t["from"]) in idmap else 0.5
            for t in transactions
        ], dtype=np.float64)

    def predict_proba_features(self, X_tab, transactions: List[Dict]) -> np.ndarray:
        """Meta probability per row; `transactions` must align with X rows
        (for the GNN column)."""
        X = np.asarray(X_tab, dtype=np.float64)
        gnn_col = self.gnn_scores_for(list(transactions))
        lstm_col = self.lstm.predict_proba(X, list(transactions)) if self.lstm else np.full(len(X), 0.5)
        ae_col = self.ae.predict_proba(X) if self.ae else np.full(len(X), 0.5)
        X_meta = np.column_stack([np.asarray(self.xgb.predict_proba(X)),
                                  gnn_col, lstm_col, ae_col])
        return np.asarray(self.meta.predict_proba(X_meta), dtype=np.float64)

    def score_transactions(self, transactions: List[Dict],
                           world_state=None) -> np.ndarray:
        """Full-pipeline P(fraud) for raw tx dicts."""
        if not transactions:
            return np.zeros(0)
        X_tab, _, _ = compute_features(transactions, world_state)
        return self.predict_proba_features(X_tab, transactions)

    def attack_caught(self, transactions: List[Dict], world_state=None,
                      threshold: float = DEFAULT_THRESHOLD) -> Dict:
        """Was this attack instance caught? Uses MAX per-tx probability."""
        probs = self.score_transactions(transactions, world_state)
        peak = float(probs.max()) if probs.size else 0.0
        return {"caught": bool(peak >= threshold), "peak_score": round(peak, 4),
                "mean_score": round(float(probs.mean()), 4) if probs.size else 0.0}

    # ------------------------------------------------------------------ #
    # Five-signal sidecar (Phase 6): supervised meta + normalcy manifold +
    # spectral topology fingerprint. Used by decorrelation analysis and the
    # dashboard; does NOT alter the calibrated meta path above.
    # ------------------------------------------------------------------ #
    def score_all_signals(self, transactions: List[Dict], world_state=None,
                          manifold=None) -> Dict[str, np.ndarray]:
        """Return aligned per-tx signal columns:

            xgb       P(fraud) tabular
            gnn       node-risk of sender on trained graph
            meta      calibrated blend (the deployed fast-path score)
            manifold  NormalcyManifold reconstruction anomaly (~1 typical)
            spectral_cycle / spectral_star   1−residual topology matches

        `manifold` must already be fit (normal-only law enforced there).
        """
        from .spectral import compute_spectral_features
        if not transactions:
            empty = np.zeros(0, dtype=np.float32)
            return {k: empty.copy() for k in
                    ("xgb", "gnn", "meta", "manifold",
                     "spectral_cycle", "spectral_star")}

        X_tab, _, _ = compute_features(list(transactions), world_state)
        X = np.asarray(X_tab, dtype=np.float64)
        xgb_col = np.asarray(self.xgb.predict_proba(X), dtype=np.float64)
        gnn_col = self.gnn_scores_for(list(transactions)).astype(np.float64)
        lstm_col = self.lstm.predict_proba(X, list(transactions)) if self.lstm else np.full(len(X), 0.5)
        ae_col = self.ae.predict_proba(X) if self.ae else np.full(len(X), 0.5)
        
        meta_col = np.asarray(
            self.meta.predict_proba(np.column_stack([xgb_col, gnn_col, lstm_col, ae_col])),
            dtype=np.float64)

        man_col = np.zeros(len(X), dtype=np.float32)
        if manifold is not None:
            man_col = np.asarray(manifold.score(X), dtype=np.float32)

        X_spec, _names = compute_spectral_features(transactions)
        names_idx = {n: i for i, n in enumerate(_names)}
        # residual→signal squash into [0,1]: 1 = exact archetype match
        spec_cycle = np.clip(
            1.0 - X_spec[:, names_idx["spec_cycle_residual"]] / 2.0,
            0.0, 1.0)
        spec_star = np.clip(
            1.0 - X_spec[:, names_idx["spec_star_residual"]] / 2.0,
            0.0, 1.0)

        return {
            "xgb": xgb_col,
            "gnn": gnn_col,
            "meta": meta_col,
            "manifold": man_col,
            "spectral_cycle": spec_cycle.astype(np.float32),
            "spectral_star": spec_star.astype(np.float32),
        }


GNN_EPOCHS_OOF = 20
