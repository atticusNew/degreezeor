"""Attribution: pivotality monotonicity, normalization, and the residual floor."""

from __future__ import annotations

from degreezeor.core.interfaces import AttributionContext
from degreezeor.scoring.attribution import (
    MAX_HUMAN_TOTAL,
    build_attribution,
    normalize,
    pivotality_from_margin,
)
from degreezeor.core.interfaces import AttributionContribution
from degreezeor.core.numeric import D


def test_pivotality_decreases_with_margin() -> None:
    tie = pivotality_from_margin(0)
    one = pivotality_from_margin(1)
    lopsided = pivotality_from_margin(200)
    assert float(tie) == 1.0
    assert float(one) == 0.5
    assert float(one) > float(lopsided)
    assert float(lopsided) < 0.01


def test_attributions_plus_residual_sum_to_one() -> None:
    ctx = AttributionContext(
        eu_id=1, action_type="law", sponsor_official_id=10, signer_official_id=20,
        vote_margin=None, member_on_winning_side=None,
    )
    rows = build_attribution(ctx)
    total = sum((D(r.attribution) for r in rows), D(0))
    assert abs(float(total) - 1.0) < 1e-6
    assert any(r.is_residual for r in rows)


def test_residual_is_large_no_human_gets_everything() -> None:
    ctx = AttributionContext(
        eu_id=1, action_type="law", sponsor_official_id=10, signer_official_id=20,
        vote_margin=None, member_on_winning_side=None,
    )
    rows = build_attribution(ctx)
    residual = next(r for r in rows if r.is_residual)
    assert float(residual.attribution) >= 0.30


def test_normalization_caps_human_total() -> None:
    # Five heavy contributors would exceed the cap; normalization must scale them down.
    contribs = [
        AttributionContribution(
            official_id=i, role="sponsor", authority=D("0.3"), pivotality=D(1),
            raw_weight=D("0.3"), raw_low=D("0.2"), raw_high=D("0.4"),
        )
        for i in range(5)
    ]
    rows = normalize(contribs)
    human_total = sum((D(r.attribution) for r in rows if not r.is_residual), D(0))
    assert float(human_total) <= float(MAX_HUMAN_TOTAL) + 1e-9
