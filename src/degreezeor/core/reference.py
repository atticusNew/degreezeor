"""Reference data seeds (public record).

Kept intentionally small and explicit. Presidents-by-date lets the signer
attribution channel resolve who signed a law from its enacted date. These are
verifiable public facts; sources are noted inline.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from degreezeor.core.models import Jurisdiction, Official

# (full_name, bioguide_id, start_date, end_date_or_None)  — source: archives/Federal Register
PRESIDENTS: list[tuple[str, str, date, date | None]] = [
    ("Barack Obama", "O000167", date(2009, 1, 20), date(2017, 1, 20)),
    ("Donald J. Trump", "T000452p", date(2017, 1, 20), date(2021, 1, 20)),
    ("Joseph R. Biden Jr.", "B000444p", date(2021, 1, 20), date(2025, 1, 20)),
]


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
    """Return the Official who was President on ``day`` (creating the row if needed)."""
    for full_name, bioguide, start, end in PRESIDENTS:
        if start <= day and (end is None or day < end):
            official = session.execute(
                select(Official).where(Official.bioguide_id == bioguide)
            ).scalar_one_or_none()
            if official is None:
                official = Official(full_name=full_name, bioguide_id=bioguide)
                session.add(official)
                session.flush()
            return official
    return None
