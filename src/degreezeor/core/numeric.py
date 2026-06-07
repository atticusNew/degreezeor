"""Numeric policy.

ALL monetary values, scores, weights, and probabilities use :class:`decimal.Decimal`
(never float) to guarantee precision and bit-for-bit reproducibility of score runs.
Statistical kernels that require floating point (e.g. numpy bootstrap) are isolated
and their *outputs* are quantized back to Decimal at fixed precision before storage.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, getcontext
from typing import Iterable

# A generous, fixed precision so reproducibility does not depend on host defaults.
getcontext().prec = 50

# Canonical quantization grids.
Q_SCORE = Decimal("0.0001")  # 0..100 scores and 0..1 weights/probabilities
Q_MONEY = Decimal("0.01")  # USD


def D(value: object) -> Decimal:
    """Coerce to Decimal deterministically (via str to avoid float artifacts)."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(str(value))


def q_score(value: object) -> Decimal:
    return D(value).quantize(Q_SCORE, rounding=ROUND_HALF_EVEN)


def q_money(value: object) -> Decimal:
    return D(value).quantize(Q_MONEY, rounding=ROUND_HALF_EVEN)


def clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))


def clamp01(value: object) -> Decimal:
    return q_score(clamp(D(value), Decimal(0), Decimal(1)))


def clamp_score(value: object) -> Decimal:
    """Clamp to the 0..100 scoring band."""
    return q_score(clamp(D(value), Decimal(0), Decimal(100)))


def dmean(values: Iterable[object]) -> Decimal:
    vals = [D(v) for v in values]
    if not vals:
        raise ValueError("dmean() of empty sequence")
    return sum(vals, Decimal(0)) / Decimal(len(vals))


def dprod(values: Iterable[object]) -> Decimal:
    out = Decimal(1)
    for v in values:
        out *= D(v)
    return out
