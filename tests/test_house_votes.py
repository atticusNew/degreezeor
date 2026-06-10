"""House roll-call parsing + decisive-vote attribution propagation."""

from __future__ import annotations

from datetime import date

from degreezeor.core.interfaces import AttributionContext
from degreezeor.ingestion.adapters.house_clerk import parse_house_vote
from degreezeor.pipeline import _norm_legis_num
from degreezeor.scoring.attribution import DecisiveVoteChannel, build_attribution

_XML = b"""<?xml version="1.0"?>
<rollcall-vote>
  <vote-metadata>
    <vote-question>On Passage</vote-question>
    <vote-result>Passed</vote-result>
    <legis-num>H R 1</legis-num>
    <vote-desc>Test Act</vote-desc>
  </vote-metadata>
  <vote-data>
    <recorded-vote><legislator name-id="A000001" unaccented-name="Alpha" party="D" state="CA">Alpha</legislator><vote>Yea</vote></recorded-vote>
    <recorded-vote><legislator name-id="B000002" unaccented-name="Beta" party="R" state="TX">Beta</legislator><vote>Nay</vote></recorded-vote>
    <recorded-vote><legislator name-id="C000003" unaccented-name="Gamma" party="D" state="NY">Gamma</legislator><vote>Yea</vote></recorded-vote>
    <recorded-vote><legislator name-id="D000004" unaccented-name="Delta" party="R" state="OH">Delta</legislator><vote>Not Voting</vote></recorded-vote>
  </vote-data>
</rollcall-vote>"""


def test_parse_house_vote_counts_and_ids() -> None:
    v = parse_house_vote(_XML)
    assert v.question == "On Passage"
    assert v.result == "Passed"
    assert (v.yea, v.nay, v.not_voting) == (2, 1, 1)
    assert v.margin == 1
    assert v.passed is True
    assert {p.bioguide_id for p in v.positions} == {"A000001", "B000002", "C000003", "D000004"}
    yea_ids = {p.bioguide_id for p in v.positions if p.position == "yea"}
    assert yea_ids == {"A000001", "C000003"}


_XML_META = b"""<?xml version="1.0"?>
<rollcall-vote><vote-metadata>
  <congress>119</congress><rollcall-num>16</rollcall-num>
  <legis-num>H R 30</legis-num><vote-question>On Passage</vote-question>
  <vote-result>Failed</vote-result><action-date>16-Jan-2025</action-date>
</vote-metadata><vote-data>
  <recorded-vote><legislator name-id="A000001" unaccented-name="Alpha">Alpha</legislator><vote>Yea</vote></recorded-vote>
</vote-data></rollcall-vote>"""


def test_parse_house_vote_extracts_congress_roll_and_date() -> None:
    v = parse_house_vote(_XML_META)
    assert v.congress == 119
    assert v.rollcall_num == 16
    assert v.vote_date == date(2025, 1, 16)
    assert v.result == "Failed"


def test_norm_legis_num_maps_to_bill_number_or_none() -> None:
    assert _norm_legis_num("H R 1") == "HR1"
    assert _norm_legis_num("H RES 5") == "HRES5"
    assert _norm_legis_num("H J RES 7") == "HJRES7"
    assert _norm_legis_num("S 1") == "S1"
    assert _norm_legis_num("QUORUM") is None
    assert _norm_legis_num("") is None


def test_decisive_vote_attribution_is_sorted_and_pivotality_weighted() -> None:
    ctx = AttributionContext(
        eu_id=1, action_type="law", sponsor_official_id=None, signer_official_id=None,
        vote_margin=1, member_on_winning_side=True, decisive_official_ids=[30, 10, 20],
    )
    contribs = DecisiveVoteChannel().contributions(ctx)
    assert [c.official_id for c in contribs] == [10, 20, 30]  # sorted => deterministic
    assert all(c.role == "decisive_vote" for c in contribs)
    assert all(float(c.pivotality) == 0.5 for c in contribs)  # margin 1 => 1/(1+1)


def test_lopsided_vote_gives_near_zero_decisive_weight() -> None:
    ctx = AttributionContext(
        eu_id=1, action_type="law", sponsor_official_id=None, signer_official_id=None,
        vote_margin=300, member_on_winning_side=True, decisive_official_ids=[1, 2, 3],
    )
    contribs = DecisiveVoteChannel().contributions(ctx)
    assert all(float(c.raw_weight) < 0.001 for c in contribs)


def test_full_attribution_with_votes_keeps_large_residual() -> None:
    ctx = AttributionContext(
        eu_id=1, action_type="law", sponsor_official_id=900, signer_official_id=901,
        vote_margin=9, member_on_winning_side=True, decisive_official_ids=list(range(220)),
    )
    rows = build_attribution(ctx)
    residual = next(r for r in rows if r.is_residual)
    assert float(residual.attribution) >= 0.30  # no overstatement despite 220 voters
