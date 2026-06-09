"""Loaders: parse landed raw official data into normalized rows.

Entity resolution is by stable official identifiers (Bioguide IDs, Public Law
numbers). Party is captured for transparency in the ``parties`` table but is never
consulted by scoring code (party-blindness is enforced by tests).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal

from dateutil import parser as dtparse
from sqlalchemy import select
from sqlalchemy.orm import Session

from degreezeor.categories import classify_executive_domain
from degreezeor.core.hashing import sha256_hex
from degreezeor.core.models import (
    Action,
    Bill,
    DataSource,
    ExecutiveOrder,
    Law,
    Metric,
    Objective,
    Observation,
    OfficeTerm,
    Official,
    Party,
)
from degreezeor.core.reference import ensure_us_federal, president_on
from degreezeor.ingestion.adapters.bls import bls_adapter
from degreezeor.ingestion.adapters.congress import congress_adapter
from degreezeor.ingestion.adapters.federalregister import federal_register_adapter
from degreezeor.ingestion.landing import ensure_source, land

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", text or "")).strip()


def _ensure_official(session: Session, bioguide: str, full_name: str) -> Official:
    o = session.execute(select(Official).where(Official.bioguide_id == bioguide)).scalar_one_or_none()
    if o is None:
        o = Official(bioguide_id=bioguide, full_name=full_name)
        session.add(o)
        session.flush()
    return o


def _ensure_party(session: Session, abbrev: str) -> Party:
    names = {"D": "Democratic", "R": "Republican", "I": "Independent"}
    p = session.execute(select(Party).where(Party.abbrev == abbrev)).scalar_one_or_none()
    if p is None:
        p = Party(abbrev=abbrev, name=names.get(abbrev, abbrev))
        session.add(p)
        session.flush()
    return p


def load_law(session: Session, congress: int, law_number: int, law_type: str = "pub") -> Action:
    """Ingest one enacted law (Congress.gov) into Action/Law/Bill + sponsor + objectives."""
    fetch = congress_adapter.fetch_law(congress, law_number, law_type)
    land(session, fetch)
    doc = json.loads(fetch.content)
    bill = doc["bill"]

    src = session.execute(
        select(DataSource).where(DataSource.name == congress_adapter.name)
    ).scalar_one()
    jur = ensure_us_federal(session)

    pl_number = next(
        (pl["number"] for pl in bill.get("laws", []) if pl.get("type") == "Public Law"),
        f"{congress}-{law_number}",
    )
    enacted = bill.get("latestAction", {}).get("actionDate")
    enacted_date: date | None = dtparse.parse(enacted).date() if enacted else None
    domain = (bill.get("policyArea") or {}).get("name")

    # Idempotency: keyed on Public Law number.
    existing = session.execute(
        select(Action).join(Law, Law.action_id == Action.id).where(Law.public_law_number == pl_number)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    action = Action(
        type="law",
        title=bill.get("title", ""),
        action_date=enacted_date,
        jurisdiction_id=jur.id,
        source_id=src.id,
        source_url=fetch.source_url,
        native_identifier=f"PL{pl_number}",
        content_hash=fetch.content_hash,
        domain=domain,
        implemented=True,  # an enacted law is implemented (effects may lag)
    )
    session.add(action)
    session.flush()

    # Sponsor (entity-resolved by Bioguide); party stored for transparency only.
    signer = president_on(session, enacted_date) if enacted_date else None
    sponsor_official_id = None
    sponsors = bill.get("sponsors") or []
    if sponsors:
        sp = sponsors[0]
        sponsor = _ensure_official(session, sp["bioguideId"], sp.get("fullName", ""))
        sponsor_official_id = sponsor.id
        if sp.get("party"):
            party = _ensure_party(session, sp["party"])
            if not session.execute(
                select(OfficeTerm).where(
                    OfficeTerm.official_id == sponsor.id, OfficeTerm.party_id == party.id
                )
            ).scalar_one_or_none():
                session.add(OfficeTerm(official_id=sponsor.id, party_id=party.id))

    btype = (bill.get("type") or "").lower()
    bnum = bill.get("number")
    session.add(
        Bill(
            action_id=action.id,
            congress=congress,
            bill_number=f"{btype}{bnum}" if bnum else None,
            sponsor_official_id=sponsor_official_id,
            status="enacted",
            became_law_action_id=action.id,
        )
    )
    session.add(
        Law(
            action_id=action.id,
            public_law_number=pl_number,
            enacted_date=enacted_date,
            signed_by_official_id=signer.id if signer else None,
        )
    )

    # Objective Tier 0 (statutory short title) — always present.
    session.add(
        Objective(
            action_id=action.id,
            text=bill.get("title", ""),
            source_id=src.id,
            source_url=fetch.source_url,
            objective_level="statutory",
        )
    )

    # Objective Tier "agency/CRS" — official CRS summary (best available, introduced version).
    if btype and bnum:
        sfetch = congress_adapter.fetch_bill_summaries(congress, btype, int(bnum))
        land(session, sfetch)
        sdoc = json.loads(sfetch.content)
        summaries = sdoc.get("summaries") or []
        if summaries:
            text = _strip_html(summaries[0].get("text", ""))
            if text:
                session.add(
                    Objective(
                        action_id=action.id,
                        text=text,
                        source_id=src.id,
                        source_url=sfetch.source_url,
                        objective_level="agency",
                    )
                )
    return action


def load_executive_order(session: Session, document_number: str) -> Action:
    """Ingest one executive order (Federal Register) into Action(type='eo') + signer."""
    fetch = federal_register_adapter.fetch(document_number)
    land(session, fetch)
    doc = json.loads(fetch.content)

    src = session.execute(
        select(DataSource).where(DataSource.name == federal_register_adapter.name)
    ).scalar_one()
    jur = ensure_us_federal(session)

    eo_number = doc.get("executive_order_number")
    native_id = f"EO{eo_number}" if eo_number else f"FR{document_number}"
    signing = doc.get("signing_date") or doc.get("publication_date")
    signing_date: date | None = dtparse.parse(signing).date() if signing else None

    existing = session.execute(
        select(Action).where(Action.native_identifier == native_id, Action.type == "eo")
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    signer = president_on(session, signing_date) if signing_date else None
    title = doc.get("title", "")
    abstract = _strip_html(doc.get("abstract") or "")
    # EOs carry no policy area; classify a topic from the title+abstract so the record groups
    # by category (deterministic keyword table). Falls back to a generic governance domain.
    eo_domain = classify_executive_domain(f"{title}. {abstract}") or "Government Operations and Politics"

    # Link to the Federal Register's official permalink (/d/<doc>), which always resolves;
    # the bare /documents/<doc> form is not guaranteed for every document and can 404.
    public_url = doc.get("html_url") or f"https://www.federalregister.gov/d/{document_number}"

    action = Action(
        type="eo",
        title=title,
        action_date=signing_date,
        jurisdiction_id=jur.id,
        source_id=src.id,
        source_url=public_url,
        native_identifier=native_id,
        content_hash=fetch.content_hash,
        domain=eo_domain,
        implemented=True,
    )
    session.add(action)
    session.flush()
    session.add(
        ExecutiveOrder(
            action_id=action.id,
            eo_number=str(eo_number) if eo_number else None,
            signing_official_id=signer.id if signer else None,
            fr_doc_number=document_number,
        )
    )
    # Objective from the EO's own title (+ abstract when present) — its stated purpose.
    objective_text = f"{title}. {abstract}".strip(". ").strip() or title
    session.add(
        Objective(
            action_id=action.id,
            text=objective_text,
            source_id=src.id,
            source_url=fetch.source_url,
            objective_level="executive",
        )
    )
    return action


_PASSAGE_KEYWORDS = ("passage", "passed", "concur", "agreed to", "adoption")


def load_regulation(session: Session, document_number: str) -> Action:
    """Ingest one final agency rule (Federal Register) into Action(type='regulation').

    Unlike an EO (a unilateral presidential instrument), a regulation is issued by an
    agency under delegated executive authority; we attribute it to the administration in
    office on its effective date (derived at score/rescore time from the action date, like
    budget execution), so no dedicated subtype row is required."""
    fetch = federal_register_adapter.fetch(document_number)
    land(session, fetch)
    doc = json.loads(fetch.content)

    src = session.execute(
        select(DataSource).where(DataSource.name == federal_register_adapter.name)
    ).scalar_one()
    jur = ensure_us_federal(session)

    native_id = f"REG{document_number}"
    existing = session.execute(
        select(Action).where(Action.native_identifier == native_id, Action.type == "regulation")
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    eff = doc.get("effective_on") or doc.get("publication_date")
    eff_date: date | None = dtparse.parse(eff).date() if eff else None
    title = doc.get("title", "")
    abstract = _strip_html(doc.get("abstract") or "")

    action = Action(
        type="regulation", title=title, action_date=eff_date, jurisdiction_id=jur.id,
        source_id=src.id, source_url=fetch.source_url, native_identifier=native_id,
        content_hash=fetch.content_hash, domain="Economics and Public Finance", implemented=True,
    )
    session.add(action)
    session.flush()
    objective_text = f"{title}. {abstract}".strip(". ").strip() or title
    session.add(Objective(action_id=action.id, text=objective_text, source_id=src.id,
                          source_url=fetch.source_url, objective_level="agency"))
    return action


def load_house_final_passage_vote(
    session: Session, action: Action, congress: int, bill_type: str, bill_number: int
):
    """Ingest the final-passage House roll-call vote for a law: Vote + VotePosition rows
    (full member record) + ensures Officials. Returns the parsed HouseVote (or None)."""
    from degreezeor.core.models import Vote, VotePosition
    from degreezeor.ingestion.adapters.house_clerk import house_clerk_adapter, parse_house_vote

    afetch = congress_adapter.fetch_bill_actions(congress, bill_type, bill_number)
    land(session, afetch)
    actions = json.loads(afetch.content).get("actions", [])

    # Collect House recorded votes whose action text indicates final passage; latest wins.
    candidates = []
    for act in actions:
        text = (act.get("text") or "").lower()
        if not any(k in text for k in _PASSAGE_KEYWORDS):
            continue
        for rv in act.get("recordedVotes") or []:
            if rv.get("chamber") == "House" and rv.get("url"):
                candidates.append((rv.get("date") or "", rv["url"]))
    if not candidates:
        return None
    candidates.sort()
    vote_url = candidates[-1][1]

    # Idempotency: skip if we already stored this roll-call URL.
    existing = session.execute(select(Vote).where(Vote.question == vote_url)).scalar_one_or_none()
    if existing is not None:
        return None

    vfetch = house_clerk_adapter.fetch(vote_url)
    land(session, vfetch)
    hv = parse_house_vote(vfetch.content)

    vote = Vote(
        action_id=action.id, chamber="house", question=vote_url,  # store URL for idempotency + trail
        vote_date=action.action_date, yea=hv.yea, nay=hv.nay, present=hv.present,
        not_voting=hv.not_voting, result=hv.result,
    )
    session.add(vote)
    session.flush()
    winning_position = "yea" if hv.passed else "nay"
    winning_official_ids: list[int] = []
    for mv in hv.positions:
        if not mv.bioguide_id:
            continue
        official = _ensure_official(session, mv.bioguide_id, mv.name)
        if mv.party:
            party = _ensure_party(session, mv.party)
            if not session.execute(
                select(OfficeTerm).where(
                    OfficeTerm.official_id == official.id, OfficeTerm.party_id == party.id
                )
            ).scalar_one_or_none():
                session.add(OfficeTerm(official_id=official.id, party_id=party.id))
        session.add(VotePosition(vote_id=vote.id, official_id=official.id, position=mv.position))
        if mv.position == winning_position:
            winning_official_ids.append(official.id)
    session.flush()
    return hv, winning_official_ids


def load_senate_final_passage_vote(
    session: Session, action: Action, congress: int, bill_type: str, bill_number: int
):
    """Ingest the final-passage Senate roll-call vote for a law: Vote(chamber='senate') +
    VotePosition rows + ensures Officials, resolving each senator's ``lis_member_id`` to a
    Bioguide ID via the crosswalk. Returns (SenateVote, winning_official_ids) or None.

    Senators who cannot be resolved to a Bioguide ID are skipped (logged), never guessed."""
    from degreezeor.core.models import Vote, VotePosition
    from degreezeor.ingestion.adapters.congress_legislators import congress_legislators_adapter
    from degreezeor.ingestion.adapters.senate import parse_senate_vote, senate_rollcall_adapter

    afetch = congress_adapter.fetch_bill_actions(congress, bill_type, bill_number)
    land(session, afetch)
    actions = json.loads(afetch.content).get("actions", [])

    candidates = []
    for act in actions:
        text = (act.get("text") or "").lower()
        if not any(k in text for k in _PASSAGE_KEYWORDS):
            continue
        for rv in act.get("recordedVotes") or []:
            if rv.get("chamber") == "Senate" and rv.get("url"):
                candidates.append((rv.get("date") or "", rv["url"]))
    if not candidates:
        return None
    candidates.sort()
    vote_url = candidates[-1][1]

    if session.execute(select(Vote).where(Vote.question == vote_url)).scalar_one_or_none() is not None:
        return None  # idempotent: already stored this roll-call

    vfetch = senate_rollcall_adapter.fetch(vote_url)
    land(session, vfetch)
    sv = parse_senate_vote(vfetch.content)

    lis_map = congress_legislators_adapter.lis_to_bioguide()
    vote = Vote(
        action_id=action.id, chamber="senate", question=vote_url,
        vote_date=action.action_date, yea=sv.yea, nay=sv.nay, present=sv.present,
        not_voting=sv.not_voting, result=sv.result,
    )
    session.add(vote)
    session.flush()
    winning_position = "yea" if sv.passed else "nay"
    winning_official_ids: list[int] = []
    for mv in sv.positions:
        bioguide = lis_map.get(mv.lis_member_id)
        if not bioguide:
            log.warning("unresolved senator lis_member_id=%s (%s)", mv.lis_member_id, mv.last_name)
            continue
        full_name = f"{mv.first_name} {mv.last_name}".strip()
        official = _ensure_official(session, bioguide, full_name)
        if mv.party:
            party = _ensure_party(session, mv.party)
            if not session.execute(
                select(OfficeTerm).where(
                    OfficeTerm.official_id == official.id, OfficeTerm.party_id == party.id
                )
            ).scalar_one_or_none():
                session.add(OfficeTerm(official_id=official.id, party_id=party.id))
        session.add(VotePosition(vote_id=vote.id, official_id=official.id, position=mv.position))
        if mv.position == winning_position:
            winning_official_ids.append(official.id)
    session.flush()
    return sv, winning_official_ids


def _insert_observation(session: Session, metric: Metric, jur_id: int, source_id: int,
                        period_iso: str, value: str, source_url: str, retrieved_at) -> bool:
    """Insert one observation if absent. Returns True if inserted."""
    exists = session.execute(
        select(Observation).where(
            Observation.metric_id == metric.id,
            Observation.jurisdiction_id == jur_id,
            Observation.period == period_iso,
        )
    ).scalar_one_or_none()
    if exists:
        return False
    session.add(Observation(
        metric_id=metric.id, jurisdiction_id=jur_id, period=period_iso,
        value=Decimal(str(value)), source_id=source_id, source_url=source_url,
        retrieved_at=retrieved_at,
        content_hash=sha256_hex(f"{metric.native_series_id}:{period_iso}:{value}"),
    ))
    return True


def _load_bls_observations(session: Session, metric: Metric, start_year: int, end_year: int) -> int:
    fetch = bls_adapter.fetch(metric.native_series_id, start_year=start_year, end_year=end_year)
    land(session, fetch)
    series = json.loads(fetch.content)["Results"]["series"][0]
    jur = ensure_us_federal(session)
    src_id = session.execute(select(DataSource.id).where(DataSource.name == bls_adapter.name)).scalar_one()
    retrieved = datetime.fromisoformat(fetch.retrieved_at.isoformat())
    count = 0
    for pt in series["data"]:
        period = pt["period"]  # M01..M12 (monthly); skip quarterly/annual aggregates
        if not period.startswith("M"):
            continue
        period_iso = f"{pt['year']}-{int(period[1:]):02d}-01"
        if _insert_observation(session, metric, jur.id, src_id, period_iso, pt["value"],
                               fetch.source_url, retrieved):
            count += 1
    session.flush()
    return count


def _load_annual_observations(session: Session, metric: Metric, start_year: int, end_year: int,
                              adapter, ensure_src) -> int:
    """Generic loader for annual single-value series (CDC / Census / EIA). The adapter
    exposes ``fetch(native_id, start_year, end_year)`` + ``parse_series(content, native_id)
    -> [(year, value)]``; we anchor each annual point to Jan 1 of its year."""
    fetch = adapter.fetch(metric.native_series_id, start_year=start_year, end_year=end_year)
    land(session, fetch)
    jur = ensure_us_federal(session)
    src_id = ensure_src(session).id
    retrieved = datetime.fromisoformat(fetch.retrieved_at.isoformat())
    count = 0
    for year, value in adapter.parse_series(fetch.content, metric.native_series_id):
        if _insert_observation(session, metric, jur.id, src_id, f"{year}-01-01", value,
                               fetch.source_url, retrieved):
            count += 1
    session.flush()
    return count


def load_observations(
    session: Session, metric: Metric, start_year: int, end_year: int
) -> int:
    """Ingest a metric's official outcome series into observations (pluggable by source).
    Dispatches on the metric's native series id prefix (CDC / Census / EIA Socrata-style
    annual series, else BLS monthly). Returns the count loaded."""
    nsid = metric.native_series_id
    if nsid.startswith("CDC|"):
        from degreezeor.ingestion.adapters.cdc import cdc_adapter
        return _load_annual_observations(session, metric, start_year, end_year,
                                         cdc_adapter, ensure_cdc_source)
    if nsid.startswith("CENSUS|"):
        from degreezeor.ingestion.adapters.census import census_adapter
        return _load_annual_observations(session, metric, start_year, end_year,
                                         census_adapter, ensure_census_source)
    if nsid.startswith("EIA|"):
        from degreezeor.ingestion.adapters.eia import eia_adapter
        return _load_annual_observations(session, metric, start_year, end_year,
                                         eia_adapter, ensure_eia_source)
    return _load_bls_observations(session, metric, start_year, end_year)


def _name_needs_enrichment(full_name: str | None) -> bool:
    """A vote-derived name that should be replaced with a full Congress.gov name:
    a single token ("Adams"), or a last-name + tag like "Bishop (GA)" / "DeFazio [D-OR-4]"."""
    if not full_name:
        return False
    return len(full_name.split()) == 1 or "(" in full_name or "[" in full_name


def enrich_official_names(session: Session, limit: int | None = None) -> int:
    """Replace last-name-only (vote-derived) official names with full names from
    Congress.gov /member/{bioguide}. Idempotent; returns the number updated."""
    candidates = [
        o for o in session.execute(
            select(Official).where(Official.bioguide_id.is_not(None))
        ).scalars().all()
        if _name_needs_enrichment(o.full_name)
    ]
    updated = 0
    for o in candidates:
        if limit is not None and updated >= limit:
            break
        try:
            fetch = congress_adapter.fetch_member(o.bioguide_id)
            land(session, fetch)
            m = json.loads(fetch.content).get("member", {})
            full = (m.get("directOrderName") or "").strip()
            if full:
                o.full_name = full
                updated += 1
        except Exception:  # noqa: BLE001 - name enrichment is best-effort
            continue
    session.flush()
    return updated


def ensure_bls_source(session: Session) -> DataSource:
    return ensure_source(
        session, name=bls_adapter.name, tier=bls_adapter.tier, base_url=bls_adapter.base_url
    )


def ensure_cdc_source(session: Session) -> DataSource:
    from degreezeor.ingestion.adapters.cdc import cdc_adapter
    return ensure_source(
        session, name=cdc_adapter.name, tier=cdc_adapter.tier, base_url=cdc_adapter.base_url
    )


def ensure_census_source(session: Session) -> DataSource:
    from degreezeor.ingestion.adapters.census import census_adapter
    return ensure_source(
        session, name=census_adapter.name, tier=census_adapter.tier, base_url=census_adapter.base_url
    )


def ensure_eia_source(session: Session) -> DataSource:
    from degreezeor.ingestion.adapters.eia import eia_adapter
    return ensure_source(
        session, name=eia_adapter.name, tier=eia_adapter.tier, base_url=eia_adapter.base_url
    )
