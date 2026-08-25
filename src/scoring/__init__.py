"""Structured deep-path score — Mastercard 0-1000 bands."""
from .structured_score import (
    compute_structured_score, score_from_ml_probs,
    get_band, get_band_color, DEFAULT_WEIGHTS, BANDS,
)

__all__ = [
    'compute_structured_score', 'score_from_ml_probs',
    'get_band', 'get_band_color', 'DEFAULT_WEIGHTS', 'BANDS',
]