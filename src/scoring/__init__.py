"""Structured deep-path score — Mastercard 0-1000 bands."""
from .structured_score import (
    compute_structured_score, score_from_ml_probs,
    get_band, get_band_color, DEFAULT_WEIGHTS, BANDS,
)
from .evidence_mapping import (
    sanctions_evidence, osint_evidence, external_evidence, campaign_evidence,
)

__all__ = [
    'compute_structured_score', 'score_from_ml_probs',
    'get_band', 'get_band_color', 'DEFAULT_WEIGHTS', 'BANDS',
    'sanctions_evidence', 'osint_evidence', 'external_evidence',
    'campaign_evidence',
]