"""Loaders: parse landed raw official data into normalized rows.

Entity resolution is by stable official identifiers (Bioguide IDs, Public Law
numbers). Party is captured for transparency in the ``parties`` table but is never
consulted by scoring code (party-blindness is enforced by tests).
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal

from dateutil import parser as dtparse
from sqlalchemy import select
from sqlalchemy.orm import Session

from degreezeor.core.hashing import sha256_hex
from degreezeor.core.models import (
    Action,
    Bill,
    DataSource,
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
from degreezeor.ingestion.landing import ensure_source, land

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


def load_observations(
    session: Session, metric: Metric, start_year: int, end_year: int
) -> int:
    """Ingest a metric's official series (BLS) into observations. Returns count loaded."""
    fetch = bls_adapter.fetch(metric.native_series_id, start_year=start_year, end_year=end_year)
    land(session, fetch)
    doc = json.loads(fetch.content)
    series = doc["Results"]["series"][0]
    jur = ensure_us_federal(session)
    count = 0
    for pt in series["data"]:
        period = pt["period"]  # M01..M12 (monthly) or Q01.. / A01 (annual)
        if not period.startswith("M"):
            continue
        month = int(period[1:])
        period_iso = f"{pt['year']}-{month:02d}-01"
        exists = session.execute(
            select(Observation).where(
                Observation.metric_id == metric.id,
                Observation.jurisdiction_id == jur.id,
                Observation.period == period_iso,
            )
        ).scalar_one_or_none()
        if exists:
            continue
        session.add(
            Observation(
                metric_id=metric.id,
                jurisdiction_id=jur.id,
                period=period_iso,
                value=Decimal(str(pt["value"])),
                source_id=session.execute(
                    select(DataSource.id).where(DataSource.name == bls_adapter.name)
                ).scalar_one(),
                source_url=fetch.source_url,
                retrieved_at=datetime.fromisoformat(fetch.retrieved_at.isoformat()),
                content_hash=sha256_hex(f"{metric.native_series_id}:{period_iso}:{pt['value']}"),
            )
        )
        count += 1
    session.flush()
    return count


def ensure_bls_source(session: Session) -> DataSource:
    return ensure_source(
        session, name=bls_adapter.name, tier=bls_adapter.tier, base_url=bls_adapter.base_url
    )
