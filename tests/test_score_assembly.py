"""Score assembly: the confidence gate suppresses the composite (no false precision)."""

from __future__ import annotations

from degreezeor.scoring.score import assemble_score


def _args(confidence):
    return dict(
        s_outcome=90, s_evidence=80, s_attribution=40, s_alignment=95,
        s_dataquality=95, s_durability=70, confidence=confidence,
    )


def test_low_confidence_is_gated_to_insufficient_evidence() -> None:
    res = assemble_score(**_args("0.20"))
    assert res.gated is True
    assert res.composite is None
    assert res.reason == "insufficient_evidence"
    # Components are still present (transparency) even when gated.
    assert {c.name for c in res.components} >= {"outcome", "evidence", "attribution"}


def test_high_confidence_produces_composite() -> None:
    res = assemble_score(**_args("0.90"))
    assert res.gated is False
    assert res.composite is not None
    # Composite is confidence-scaled, so <= the unscaled equal-weight mean.
    assert 0.0 < float(res.composite) <= 100.0


def test_composite_excludes_value_laden_components_by_default() -> None:
    res = assemble_score(**_args("0.90"))
    assert all(not c.is_value_laden for c in res.components)
