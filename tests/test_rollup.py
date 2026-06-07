"""Official-level roll-up: coverage, attribution-weighting, and gated handling."""

from __future__ import annotations

from decimal import Decimal

from degreezeor.scoring.rollup import ActionContribution, rollup


def _c(eu_id, attr, composite, conf, gated):
    return ActionContribution(
        eu_id=eu_id, attribution=Decimal(str(attr)),
        composite=None if composite is None else Decimal(str(composite)),
        confidence=None if conf is None else Decimal(str(conf)), gated=gated,
    )


def test_all_gated_yields_no_composite_with_coverage() -> None:
    # An official whose every action is gated => insufficient evidence, NOT a low score.
    r = rollup([_c(1, 0.15, None, 0.10, True), _c(2, 0.15, None, 0.19, True)])
    assert r.total_actions == 2
    assert r.scored_actions == 0
    assert float(r.coverage) == 0.0
    assert r.composite is None
    assert r.confidence is None


def test_attribution_weighted_mean_over_scored_only() -> None:
    # Two scored actions with different attribution weights; one gated (excluded).
    r = rollup([
        _c(1, 0.60, 80, 0.7, False),
        _c(2, 0.20, 20, 0.7, False),
        _c(3, 0.50, None, 0.1, True),   # gated -> excluded from composite, counts in coverage
    ])
    assert r.total_actions == 3
    assert r.scored_actions == 2
    assert abs(float(r.coverage) - (2 / 3)) < 1e-3
    # weighted mean = (0.6*80 + 0.2*20)/(0.6+0.2) = (48+4)/0.8 = 65
    assert abs(float(r.composite) - 65.0) < 1e-6


def test_coverage_is_always_reported() -> None:
    r = rollup([_c(1, 0.15, 50, 0.8, False)])
    assert float(r.coverage) == 1.0
    assert r.scored_actions == 1


def test_empty_is_safe() -> None:
    r = rollup([])
    assert r.total_actions == 0
    assert r.composite is None
    assert float(r.coverage) == 0.0
