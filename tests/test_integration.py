"""Full-stack integration: scoring → coverage → rollup → party-symmetry →
reproducibility → scorecard all compose coherently (offline, CI-able).

Each piece has its own unit tests; this guards their COMPOSITION — the integration
that manual end-to-end validation exercised. Uses the curated-fact target path so the
whole chain (including reproducible re-runs) runs without network or cache.
"""

from __future__ import annotations

from datetime import date

from degreezeor.api import presentation
from degreezeor.core.models import (
    Action,
    DataSource,
    EvaluationUnit,
    ExecutiveOrder,
    Metric,
    Objective,
    OfficeTerm,
    Official,
    Party,
)
from degreezeor.core.numeric import D
from degreezeor.integrity import party_symmetry_report
from degreezeor.pipeline import _rescore_target_eu, verify_all_reproducible


def _get_or_create_source(session) -> DataSource:
    from sqlalchemy import select
    src = session.execute(select(DataSource).where(DataSource.name == "Curated")).scalar_one_or_none()
    if src is None:
        src = DataSource(name="Curated", tier=2, base_url="https://example.gov")
        session.add(src)
        session.flush()
    return src


def _get_or_create_party(session, abbrev: str, name: str) -> Party:
    from sqlalchemy import select
    p = session.execute(select(Party).where(Party.abbrev == abbrev)).scalar_one_or_none()
    if p is None:
        p = Party(abbrev=abbrev, name=name)
        session.add(p)
        session.flush()
    return p


def _seed_scored_eo(session, *, signer_name: str, party_abbrev: str, realized: str) -> int:
    """Create + score a curated-fact EO attributed to a party-bearing signer; return eu_id."""
    from sqlalchemy import func, select

    src = _get_or_create_source(session)
    party = _get_or_create_party(session, party_abbrev, party_abbrev)
    signer = Official(full_name=signer_name)
    session.add(signer)
    session.flush()
    session.add(OfficeTerm(official_id=signer.id, party_id=party.id))
    n = session.execute(select(func.count()).select_from(Action)).scalar_one()
    action = Action(type="eo", title=f"EO under review #{n}", action_date=date(2018, 1, 1),
                    source_id=src.id, source_url="https://fr/eo", native_identifier=f"EO-{n}")
    session.add(action)
    session.flush()
    session.add(ExecutiveOrder(action_id=action.id, eo_number=str(13000 + n),
                               signing_official_id=signer.id))
    metric = session.execute(select(Metric).where(Metric.code == "legal_survival")).scalar_one_or_none()
    if metric is None:
        metric = Metric(code="legal_survival", name="Legal survival index", unit="index",
                        direction_good="up", source_id=src.id,
                        native_series_id="CURATED:court_survival", domain="Law")
        session.add(metric)
        session.flush()
    obj = Objective(action_id=action.id, source_id=src.id, source_url="https://court",
                    objective_level="executive", text="Survive judicial review.")
    session.add(obj)
    session.flush()
    eu = EvaluationUnit(action_id=action.id, objective_id=obj.id, metric_id=metric.id,
                        lag_window_months=0, sign_goal=1, status="pending", evaluation_mode="target",
                        target_value=D("100"), realized_value=D(realized), directly_attributable=True,
                        alignment=D("0.95"))
    session.add(eu)
    session.flush()
    out = _rescore_target_eu(session, eu, action, metric)
    assert out.status == "scored"
    return eu.id


def test_full_stack_composes(session) -> None:
    d_eu = _seed_scored_eo(session, signer_name="Dee President", party_abbrev="D", realized="64")
    r_eu = _seed_scored_eo(session, signer_name="Arr President", party_abbrev="R", realized="80")

    # 1) Coverage sees both scored EUs.
    cov = presentation.build_coverage(session)
    assert cov["scored"] == 2
    assert cov["total_evaluation_units"] == 2
    assert "eo" in cov["by_action_type"]
    assert "public_safety" in cov["by_category"]
    assert cov["by_category"]["public_safety"].get("scored") == 2

    # 2) Official roll-ups: each signer has a composite (attribution-weighted).
    officials = presentation.list_officials(session, scored_only=True)
    assert len(officials) == 2
    assert all(o["composite"] is not None for o in officials)

    # 3) Party-symmetry monitor: both parties present with a scored EU each.
    report = party_symmetry_report(session)
    by = {p.abbrev: p for p in report.parties}
    assert by["D"].scored_eus == 1 and by["R"].scored_eus == 1
    assert by["D"].mean_composite is not None and by["R"].mean_composite is not None
    # Different realized values => different composites (R delivered more survival).
    assert by["R"].mean_composite > by["D"].mean_composite

    # 4) Reproducibility self-audit: every published score reproduces bit-for-bit.
    audit = verify_all_reproducible(session)
    assert audit.total == 2 and audit.all_reproduced is True

    # 5) Scorecards render coherently for each EU.
    for eu_id in (d_eu, r_eu):
        card = presentation.build_scorecard(session, eu_id)
        assert card is not None
        assert card["score"]["composite"] is not None
        assert card["narrative"]
        assert len(card["components"]) >= 1
        assert any(a["role"] == "signer" for a in card["attribution"])
        # Objective category is derived from the legal-survival metric domain ("Law").
        assert card["action"]["category"] == "public_safety"
        assert card["action"]["category_label"] == "Public safety"
        assert "descriptive_context" in card

    # 6) Category taxonomy composes: catalog counts + per-official breakdown + list_units.
    cats = presentation.build_categories(session)
    by_key = {c["key"]: c for c in cats["categories"]}
    assert by_key["public_safety"]["total_actions"] == 2
    assert by_key["public_safety"]["scored_actions"] == 2

    units = presentation.list_units(session)
    assert units and all(u["category"] == "public_safety" for u in units)

    off = presentation.list_officials(session, scored_only=True)[0]
    detail = presentation.build_official(session, off["id"])
    assert "by_category" in detail
    assert any(b["category"] == "public_safety" for b in detail["by_category"])
    assert "public_safety" in (off["categories"])
    # Voter-first fields: activity summary, most-active category, and per-action dates.
    assert "activity" in detail and detail["activity"]["count"] >= 1
    assert detail["most_active_category"] == "Public safety"
    assert all("date" in a for a in detail["actions"])

    # Lightweight directory index powers the typeahead/A-Z browse.
    idx = presentation.officials_index(session)
    assert idx and all({"id", "name", "position", "scored_actions", "total_actions"} <= set(o) for o in idx)

    # Category filter narrows / widens the officials list correctly.
    assert presentation.list_officials(session, category="public_safety", scored_only=True)
    assert presentation.list_officials(session, category="health", scored_only=True) == []


def test_activity_layer_sponsored_and_cosponsored(session) -> None:
    """The unscored activity/record layer: a member who only sponsored/cosponsored bills
    still appears in the directory and their record, separate from any scored composite."""
    from degreezeor.core.models import Bill, BillCosponsor
    from degreezeor.core.reference import ensure_us_federal

    src = _get_or_create_source(session)
    jur = ensure_us_federal(session)

    sponsor = Official(full_name="Jane Sponsor", bioguide_id="X000001")
    backer = Official(full_name="John Backer", bioguide_id="X000002")
    session.add_all([sponsor, backer])
    session.flush()

    action = Action(type="bill", title="A jobs bill", action_date=date(2024, 3, 1),
                    jurisdiction_id=jur.id, source_id=src.id, source_url="https://congress/hr1",
                    native_identifier="bill/118/hr/1", domain="Economics and Public Finance")
    session.add(action)
    session.flush()
    session.add(Bill(action_id=action.id, congress=118, bill_number="HR1",
                     sponsor_official_id=sponsor.id, status="introduced"))
    session.add(BillCosponsor(action_id=action.id, official_id=backer.id))
    session.flush()

    idx = {o["name"]: o for o in presentation.officials_index(session)}
    assert idx["Jane Sponsor"]["sponsored"] == 1
    assert idx["John Backer"]["cosponsored"] == 1
    assert "jobs_economy" in idx["John Backer"]["categories"]

    rec = presentation.build_official(session, backer.id)["record"]
    assert rec["cosponsored_total"] == 1
    assert any(c["category"] == "jobs_economy" for c in rec["cosponsored_by_category"])

    srec = presentation.build_official(session, sponsor.id)["record"]
    assert srec["sponsored_total"] == 1


def test_voting_record_surface(session) -> None:
    """A member's recorded roll-call votes surface by topic + position, feed the activity
    timeline, and never become a score. Attribution-only votes (roll_call NULL) are excluded."""
    from degreezeor.core.models import Official, Vote, VotePosition

    member = Official(full_name="Vee Voter", bioguide_id="V000001")
    session.add(member)
    session.flush()

    v1 = Vote(chamber="house", question="https://clerk.house.gov/evs/2025/roll010.xml",
              vote_date=date(2025, 2, 1), result="Passed", congress=119, roll_call=10,
              bill_number="HR1", category="jobs_economy", yea=220, nay=210)
    v2 = Vote(chamber="house", question="https://clerk.house.gov/evs/2025/roll011.xml",
              vote_date=date(2025, 3, 1), result="Failed", congress=119, roll_call=11,
              bill_number="HR2", category="health", yea=200, nay=230)
    # Attribution-only vote (no roll_call) must NOT count toward the voting record.
    v3 = Vote(chamber="house", question="legacy-passage-url", vote_date=date(2019, 1, 1))
    session.add_all([v1, v2, v3])
    session.flush()
    session.add_all([
        VotePosition(vote_id=v1.id, official_id=member.id, position="yea"),
        VotePosition(vote_id=v2.id, official_id=member.id, position="nay"),
        VotePosition(vote_id=v3.id, official_id=member.id, position="yea"),
    ])
    session.flush()

    card = presentation.build_official(session, member.id)
    votes = card["votes"]
    assert votes["total"] == 2  # v3 excluded (roll_call is NULL)
    assert votes["by_position"] == {"yea": 1, "nay": 1}
    cats = {c["category"]: c for c in votes["by_category"]}
    assert cats["jobs_economy"]["yea"] == 1 and cats["health"]["nay"] == 1
    # Votes feed the activity timeline (recent years, not only scored actions).
    years = {d["year"] for d in card["activity"]["by_year"]}
    assert {2025} <= years
