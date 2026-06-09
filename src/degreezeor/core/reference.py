"""Reference data seeds (public record).

Kept intentionally small and explicit. Presidents-by-date lets the signer
attribution channel resolve who signed a law from its enacted date. These are
verifiable public facts; sources are noted inline.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from degreezeor.core.models import Jurisdiction, OfficeTerm, Official, Party

# (full_name, bioguide_id, party_abbrev, start_date, end_date_or_None). Source: archives /
# Federal Register. Party is public record and is stored for transparency only (never read
# by scoring code).
PRESIDENTS: list[tuple[str, str, str, date, date | None]] = [
    ("Barack Obama", "O000167", "D", date(2009, 1, 20), date(2017, 1, 20)),
    ("Donald J. Trump", "T000452p", "R", date(2017, 1, 20), date(2021, 1, 20)),
    ("Joseph R. Biden Jr.", "B000444p", "D", date(2021, 1, 20), date(2025, 1, 20)),
    # Second term: SAME bioguide as the first so both terms aggregate on one record.
    ("Donald J. Trump", "T000452p", "R", date(2025, 1, 20), None),
]

# Current executive officeholders who are NOT in the congressional roster, so their CURRENT
# office (and "in office" status) is shown correctly even though their legislative record was
# built in a prior role (e.g. a Vice President who was previously a Senator). Bioguide -> the
# office to display. Keep this small, explicit, and updatable; it is public record, never read
# by scoring. (The sitting President is already covered by PRESIDENTS with end=None.)
CURRENT_EXECUTIVE: dict[str, str] = {
    "V000137": "Vice President",  # JD Vance (2025– ; previously U.S. Senator, OH)
}


def current_executive_bioguides() -> set[str]:
    """Bioguides currently holding executive office (sitting president + CURRENT_EXECUTIVE)."""
    ids = {bio for _n, bio, _p, _s, end in PRESIDENTS if end is None}
    ids |= set(CURRENT_EXECUTIVE)
    return ids


def ensure_party_term(session: Session, official: Official, abbrev: str) -> None:
    """Attach a party (via an office term) to an official if not already present.
    Party is audit metadata only; scoring never reads it."""
    if not abbrev:
        return
    party = session.execute(select(Party).where(Party.abbrev == abbrev)).scalar_one_or_none()
    if party is None:
        names = {"D": "Democratic", "R": "Republican", "I": "Independent", "ID": "Independent Democrat"}
        party = Party(abbrev=abbrev, name=names.get(abbrev, abbrev))
        session.add(party)
        session.flush()
    exists = session.execute(
        select(OfficeTerm).where(OfficeTerm.official_id == official.id, OfficeTerm.party_id == party.id)
    ).scalar_one_or_none()
    if exists is None:
        session.add(OfficeTerm(official_id=official.id, party_id=party.id))
        session.flush()


def ensure_us_federal(session: Session) -> Jurisdiction:
    j = session.execute(
        select(Jurisdiction).where(Jurisdiction.type == "federal", Jurisdiction.name == "United States")
    ).scalar_one_or_none()
    if j is None:
        j = Jurisdiction(type="federal", name="United States", fips="US")
        session.add(j)
        session.flush()
    return j


def president_on(session: Session, day: date) -> Official | None:
    """Return the Official who was President on ``day`` (creating the row if needed),
    ensuring their party is recorded for transparency."""
    for full_name, bioguide, party, start, end in PRESIDENTS:
        if start <= day and (end is None or day < end):
            official = session.execute(
                select(Official).where(Official.bioguide_id == bioguide)
            ).scalar_one_or_none()
            if official is None:
                official = Official(full_name=full_name, bioguide_id=bioguide)
                session.add(official)
                session.flush()
            ensure_party_term(session, official, party)
            return official
    return None
