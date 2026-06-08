"""Confidence — attribution-weighted c_attrib (regression guard).

Attaching a large roll-call (hundreds of voters, each with a NEGLIGIBLE attribution
share but a narrow interval) must NOT spuriously inflate confidence. c_attrib weights
interval widths by attribution share, so only meaningful contributors move it.
"""

from __future__ import annotations

from degreezeor.scoring.confidence import compute_confidence

_COMMON = dict(
    best_method="declared_target_direct", ci_low=1.0, ci_high=2.0, model_dependence=0.0,
    data_tier=1, data_completeness=1.0,
)


def test_c_attrib_weighting_prevents_rollcall_inflation() -> None:
    widths = [(0.15, 0.15), (0.15, 0.15)] + [(0.0005, 0.0005)] * 500
    weighted = float(compute_confidence(attribution_widths=widths, **_COMMON).c_attrib)

    # The OLD (buggy) UNWEIGHTED mean of all 502 widths would collapse toward 0,
    # inflating c_attrib to ~1.0. Reproduce that here to prove the fix diverges from it.
    raw = [w for _, w in widths]
    unweighted_avg = sum(raw) / len(raw)
    unweighted_c_attrib = 1 - unweighted_avg
    assert unweighted_c_attrib > 0.99  # what the bug produced

    # The attribution-WEIGHTED c_attrib stays bounded (meaningful contributors dominate),
    # nowhere near the inflated value — attaching a roll-call can't manufacture confidence.
    assert weighted < 0.95
    assert unweighted_c_attrib - weighted > 0.05


def test_zero_share_contributors_fall_back_to_unweighted_mean() -> None:
    # Degenerate all-zero shares must not divide by zero; falls back to unweighted mean.
    r = compute_confidence(attribution_widths=[(0.0, 0.10), (0.0, 0.20)], **_COMMON)
    assert abs(float(r.c_attrib) - (1.0 - 0.15)) < 1e-6


def test_no_attribution_widths_uses_neutral_half() -> None:
    r = compute_confidence(attribution_widths=[], **_COMMON)
    assert float(r.c_attrib) == 0.5
