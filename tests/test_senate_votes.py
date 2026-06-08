"""Senate roll-call ingestion + lis↔bioguide crosswalk + per-chamber attribution.

Offline tests using a saved real Senate roll-call fixture (ARRA conference report,
111th Congress, vote 64) and a tiny crosswalk payload. The live end-to-end ingestion +
reproducibility check lives in ``test_pipeline_live.py`` (gated by DZ_RUN_LIVE).
"""

from __future__ import annotations

from pathlib import Path

from degreezeor.core.interfaces import AttributionContext
from degreezeor.ingestion.adapters.congress_legislators import build_lis_bioguide_map
from degreezeor.ingestion.adapters.senate import parse_senate_vote
from degreezeor.scoring.attribution import build_attribution

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "senate_vote_111_1_00064.xml"


def test_parse_senate_vote_fixture() -> None:
    sv = parse_senate_vote(FIXTURE.read_bytes())
    assert sv.document == "H.R. 1"  # ARRA
    assert sv.congress == 111 and sv.session == 1 and sv.vote_number == 64
    assert sv.yea == 60 and sv.nay == 38
    assert sv.margin == 22 and sv.passed is True
    # Members carry the lis id (NOT bioguide) — hence the crosswalk requirement.
    akaka = next(p for p in sv.positions if p.last_name == "Akaka")
    assert akaka.lis_member_id == "S213"
    assert akaka.party == "D" and akaka.state == "HI" and akaka.position == "yea"
    assert sum(1 for p in sv.positions if p.position == "yea") == 60


def test_build_lis_bioguide_map() -> None:
    payload = (
        b'[{"id": {"bioguide": "A000069", "lis": "S213"}, "name": {"last": "Akaka"}},'
        b' {"id": {"bioguide": "B000123"}, "name": {"last": "NoLis"}},'
        b' {"id": {"lis": "S999"}, "name": {"last": "NoBioguide"}}]'
    )
    m = build_lis_bioguide_map([payload])
    assert m == {"S213": "A000069"}  # only fully-identified legislators are bridged


def test_senate_decisive_votes_get_pivotality_attribution() -> None:
    ctx = AttributionContext(
        eu_id=1, action_type="law", sponsor_official_id=None, signer_official_id=None,
        vote_margin=None, member_on_winning_side=None,
        senate_vote_margin=22, senate_decisive_official_ids=[101, 102, 103],
    )
    rows = build_attribution(ctx)
    decisive = [r for r in rows if r.role == "decisive_vote"]
    assert len(decisive) == 3
    assert all(float(r.attribution) > 0 for r in decisive)
    # Residual stays large even with decisive voters.
    assert float(next(r for r in rows if r.is_residual).attribution) >= 0.30


def test_house_and_senate_voters_both_credited_with_own_margins() -> None:
    # A thin-margin chamber should credit its voters MORE than a wide-margin chamber.
    ctx = AttributionContext(
        eu_id=1, action_type="law", sponsor_official_id=None, signer_official_id=None,
        vote_margin=2, member_on_winning_side=True, decisive_official_ids=[1],
        senate_vote_margin=60, senate_decisive_official_ids=[2],
    )
    rows = build_attribution(ctx)
    house = next(r for r in rows if r.official_id == 1)
    senate = next(r for r in rows if r.official_id == 2)
    # margin 2 -> pivotality 1/3 ; margin 60 -> pivotality 1/61 ; same base authority.
    assert float(house.attribution) > float(senate.attribution)


def test_no_senate_data_is_backward_compatible() -> None:
    # Without senate fields, behaviour is identical to before (House-only / no votes).
    ctx = AttributionContext(
        eu_id=1, action_type="law", sponsor_official_id=10, signer_official_id=20,
        vote_margin=None, member_on_winning_side=None,
    )
    rows = build_attribution(ctx)
    assert not [r for r in rows if r.role == "decisive_vote"]
    assert abs(float(sum(r.attribution for r in rows)) - 1.0) < 1e-6
