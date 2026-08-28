"""Blue Team - detection stack."""
from .features import compute_features, build_graph_data, get_node_features, NODE_FEATURE_NAMES, NODE_FEATURE_DIM
from .xgb_model import XGBFraudDetector
from .gnn_model import GNNFraudDetector, FraudGNN
from .meta_model import MetaModel, make_oof_scores
from .calibrate import platt_calibrate, isotonic_calibrate, reliability_curve, expected_calibration_error
from .splits import (
    HoldoutSpec,
    lock_holdout,
    load_holdout_spec,
    assert_no_leakage,
    split_by_step,
    register_mechanism,
)

__all__ = [
    'compute_features', 'build_graph_data', 'get_node_features',
    'NODE_FEATURE_NAMES', 'NODE_FEATURE_DIM',
    'XGBFraudDetector', 'GNNFraudDetector', 'FraudGNN',
    'MetaModel', 'make_oof_scores',
    'platt_calibrate', 'isotonic_calibrate', 'reliability_curve', 'expected_calibration_error',
    'HoldoutSpec', 'lock_holdout', 'load_holdout_spec', 'assert_no_leakage',
    'split_by_step', 'register_mechanism',
]
