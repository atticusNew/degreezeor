"""Integrity-at-scale monitoring tests (PLAN.md §9.12).

Covers the two outcome-monitoring guarantees that complement the static
party-blindness guard:
  1. Behavioural party-invariance: swapping the party attached to an official cannot
     change a stored score, so per-party means reflect the world, not the formula.
  2. The party-symmetry report correctly buckets/weights scored outcomes by party,
     tracks coverage (scored vs. attributed), and flags systematic gaps for review.
"""

from __future__ import annotations

from datetime import date

from degreezeor.core.models import (
    Action,
    AttributionWeight,
    EUScore,
    EvaluationUnit,
    MethodologyVersion,
    OfficeTerm,
    Official,
    Party,
    ScoreRun,
)
from degreezeor.integrity import party_symmetry_report


def _mv(session) -> int:
    mv = MethodologyVersion(semver="test-0", git_sha="deadbeef")
    session.add(mv)
    session.flush()
    return mv.id


def _party(session, abbrev: str, name: str) -> int:
    p = Party(abbrev=abbrev, name=name)
    session.add(p)
    session.flush()
    return p.id


def _official(session, name: str, party_id: int | None) -> int:
    o = Official(full_name=name)
    session.add(o)
    session.flush()
    if party_id is not None:
        session.add(OfficeTerm(official_id=o.id, party_id=party_id))
        session.flush()
    return o.id


def _scored_eu(
    session, mv_id: int, *, official_id: int, attribution: float,
    composite: float | None, confidence: float, gated: bool, role: str = "sponsor",
) -> int:
    action = Action(type="law", title="Law", action_date=date(2020, 1, 1), source_id=1,
                    source_url="https://x", native_identifier=f"PL-{official_id}-{composite}")
    session.add(action)
    session.flush()
    eu = EvaluationUnit(action_id=action.id, status="scored" if not gated else "insufficient_evidence")
    session.add(eu)
    session.flush()
    session.add(AttributionWeight(
        eu_id=eu.id, official_id=official_id, role=role, authority=attribution,
        pivotality=1, attribution=attribution, attr_ci_low=attribution, attr_ci_high=attribution,
    ))
    # Mandatory residual so attribution is never 100% (mirrors the real pipeline).
    session.add(AttributionWeight(
        eu_id=eu.id, official_id=None, role="unattributable_residual", authority=0,
        pivotality=0, attribution=1 - attribution, attr_ci_low=1 - attribution,
        attr_ci_high=1 - attribution, is_residual=True,
    ))
    run = ScoreRun(eu_id=eu.id, methodology_version_id=mv_id, data_snapshot_id="snap",
                   seed=1, reproducible_hash="h")
    session.add(run)
    session.flush()
    session.add(EUScore(score_run_id=run.id, confidence=confidence, composite=composite,
                        gated=gated, coverage=1))
    session.flush()
    return eu.id


def test_party_invariance_means_are_identical_when_only_party_differs(session) -> None:
    """Two parties whose officials produced IDENTICAL stored composites must show
    identical mean composites and a zero gap — proving the score is party-blind."""
    mv = _mv(session)
    dem = _party(session, "D", "Democratic")
    rep = _party(session, "R", "Republican")
    d_off = _official(session, "Dee", dem)
    r_off = _official(session, "Arr", rep)
    # Same composites for both parties, just different party labels.
    for comp in (40.0, 60.0):
        _scored_eu(session, mv, official_id=d_off, attribution=0.2, composite=comp,
                   confidence=0.7, gated=False)
        _scored_eu(session, mv, official_id=r_off, attribution=0.2, composite=comp,
                   confidence=0.7, gated=False)

    report = party_symmetry_report(session, min_scored=2)
    by = {p.abbrev: p for p in report.parties}
    assert by["D"].mean_composite == by["R"].mean_composite
    assert report.composite_gap == 0
    assert report.review_required is False


def test_report_buckets_and_attribution_weights(session) -> None:
    mv = _mv(session)
    dem = _party(session, "D", "Democratic")
    d_off = _official(session, "Dee", dem)
    # Attribution-weighted mean: composites 30 (w=0.1) and 90 (w=0.3) -> 75.0
    _scored_eu(session, mv, official_id=d_off, attribution=0.1, composite=30.0,
               confidence=0.65, gated=False)
    _scored_eu(session, mv, official_id=d_off, attribution=0.3, composite=90.0,
               confidence=0.85, gated=False)

    report = party_symmetry_report(session)
    dee = next(p for p in report.parties if p.abbrev == "D")
    assert dee.attributed_eus == 2
    assert dee.scored_eus == 2
    assert dee.scored_share == 1
    # (0.1*30 + 0.3*90) / (0.1+0.3) = 30/0.4 = 75
    assert dee.mean_composite == 75


def test_gated_eus_count_for_coverage_not_composite(session) -> None:
    mv = _mv(session)
    dem = _party(session, "D", "Democratic")
    d_off = _official(session, "Dee", dem)
    _scored_eu(session, mv, official_id=d_off, attribution=0.2, composite=80.0,
               confidence=0.7, gated=False)
    # A gated EU: counts as attributed but NOT scored (insufficient evidence, not a low score).
    _scored_eu(session, mv, official_id=d_off, attribution=0.2, composite=None,
               confidence=0.3, gated=True)

    report = party_symmetry_report(session)
    dee = next(p for p in report.parties if p.abbrev == "D")
    assert dee.attributed_eus == 2
    assert dee.scored_eus == 1
    assert dee.scored_share == 0.5
    assert dee.mean_composite == 80  # gated EU excluded from the composite mean


def test_systematic_composite_gap_is_flagged_for_review(session) -> None:
    mv = _mv(session)
    dem = _party(session, "D", "Democratic")
    rep = _party(session, "R", "Republican")
    d_off = _official(session, "Dee", dem)
    r_off = _official(session, "Arr", rep)
    # >= min_scored per party so the gap is comparable; large 50-point gap. Pass an explicit
    # small min_scored so the fixture exercises the flag logic without seeding the full default.
    for _ in range(2):
        _scored_eu(session, mv, official_id=d_off, attribution=0.2, composite=80.0,
                   confidence=0.7, gated=False)
        _scored_eu(session, mv, official_id=r_off, attribution=0.2, composite=30.0,
                   confidence=0.7, gated=False)

    report = party_symmetry_report(session, min_scored=2)
    assert report.composite_gap == 50
    assert report.review_required is True
    assert any("gap" in r.lower() for r in report.review_reasons)


def test_scored_share_gap_is_flagged(session) -> None:
    mv = _mv(session)
    dem = _party(session, "D", "Democratic")
    rep = _party(session, "R", "Republican")
    d_off = _official(session, "Dee", dem)
    r_off = _official(session, "Arr", rep)
    # D: all scored; R: all gated -> scored-share gap = 1.0 (> 0.25 threshold).
    _scored_eu(session, mv, official_id=d_off, attribution=0.2, composite=60.0,
               confidence=0.7, gated=False)
    _scored_eu(session, mv, official_id=r_off, attribution=0.2, composite=None,
               confidence=0.3, gated=True)

    # min_attributed=1 so this tiny fixture exercises the gap logic directly.
    report = party_symmetry_report(session, min_attributed=1)
    assert report.scored_share_gap == 1
    assert report.review_required is True


def test_tiny_party_samples_excluded_from_share_gap_by_default(session) -> None:
    """A party represented by a single legislator on one (gated) law must NOT trigger a
    systematic-coverage review flag — that is noise, not a pattern (regression guard)."""
    mv = _mv(session)
    dem = _party(session, "D", "Democratic")
    fringe = _party(session, "ID", "Independent Democrat")
    d_off = _official(session, "Dee", dem)
    f_off = _official(session, "Lone", fringe)
    # D has a scored law; the fringe label has a single gated attribution (1 EU).
    _scored_eu(session, mv, official_id=d_off, attribution=0.2, composite=60.0,
               confidence=0.7, gated=False)
    _scored_eu(session, mv, official_id=f_off, attribution=0.2, composite=None,
               confidence=0.3, gated=True)

    report = party_symmetry_report(session)  # default min_attributed
    # Neither party meets the default min_attributed, so no share gap is computed/flagged.
    assert report.scored_share_gap is None
    assert report.review_required is False


def test_officials_without_party_are_excluded(session) -> None:
    mv = _mv(session)
    no_party = _official(session, "Independent Exec", None)
    _scored_eu(session, mv, official_id=no_party, attribution=0.5, composite=70.0,
               confidence=0.8, gated=False)
    report = party_symmetry_report(session)
    assert report.parties == []
    assert report.composite_gap is None
    assert report.review_required is False


def test_public_dict_is_json_friendly_and_carries_disclaimer(session) -> None:
    mv = _mv(session)
    dem = _party(session, "D", "Democratic")
    d_off = _official(session, "Dee", dem)
    _scored_eu(session, mv, official_id=d_off, attribution=0.2, composite=60.0,
               confidence=0.7, gated=False)
    payload = party_symmetry_report(session).to_public_dict()
    assert isinstance(payload["parties"][0]["mean_composite"], float)
    assert "party-blind" in payload["disclaimer"]
    assert "review_required" in payload
