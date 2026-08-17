"""Senate roll-call vote adapter (Tier 0, keyless).

Fetches and parses official Senate roll-call XML (senate.gov/legislative/LIS/...).
Unlike the House (whose ``name-id`` is the Bioguide ID), Senate member records key on
``lis_member_id`` (e.g. ``S213``), so resolving a senator to our Bioguide-keyed Official
records requires the lis↔bioguide crosswalk (see ``congress_legislators``).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from degreezeor.core.hashing import sha256_hex
from degreezeor.core.interfaces import SOURCE_ADAPTERS, RawFetch, SourceAdapter
from degreezeor.ingestion.http import client


@dataclass(frozen=True)
class SenateMemberVote:
    lis_member_id: str
    last_name: str
    first_name: str
    party: str | None
    state: str | None
    position: str  # yea | nay | present | nv


@dataclass(frozen=True)
class SenateVote:
    question: str
    result: str
    document: str  # e.g. "H.R. 1"
    congress: int | None
    session: int | None
    vote_number: int | None
    yea: int
    nay: int
    present: int
    not_voting: int
    positions: list[SenateMemberVote]
    vote_date: date | None = None

    @property
    def margin(self) -> int:
        return abs(self.yea - self.nay)

    @property
    def passed(self) -> bool:
        return self.yea > self.nay


_POS = {"yea": "yea", "guilty": "yea", "nay": "nay", "not guilty": "nay",
        "present": "present", "present, giving live pair": "present",
        "not voting": "nv", "": "nv"}


def _int_or_none(s: str) -> int | None:
    s = (s or "").strip()
    return int(s) if s.isdigit() else None


def _parse_senate_date(raw: str) -> date | None:
    """Parse the Senate ``vote_date`` (e.g. 'January 9, 2025,  02:54 PM') into a date."""
    parts = [p.strip() for p in (raw or "").split(",")]
    if len(parts) < 2:
        return None
    try:
        return datetime.strptime(f"{parts[0]} {parts[1]}", "%B %d %Y").date()
    except (ValueError, TypeError):
        return None


def parse_senate_vote(xml_bytes: bytes) -> SenateVote:
    root = ET.fromstring(xml_bytes)

    def _txt(path: str) -> str:
        e = root.find(path)
        return (e.text or "").strip() if e is not None else ""

    positions: list[SenateMemberVote] = []
    counts = {"yea": 0, "nay": 0, "present": 0, "nv": 0}
    for m in root.findall(".//members/member"):
        cast = (m.findtext("vote_cast") or "").strip()
        norm = _POS.get(cast.lower(), "nv")
        counts[norm] += 1
        positions.append(SenateMemberVote(
            lis_member_id=(m.findtext("lis_member_id") or "").strip(),
            last_name=(m.findtext("last_name") or "").strip(),
            first_name=(m.findtext("first_name") or "").strip(),
            party=(m.findtext("party") or "").strip() or None,
            state=(m.findtext("state") or "").strip() or None,
            position=norm,
        ))
    # Prefer the official tallied counts when present; fall back to our tally.
    yea = _int_or_none(_txt(".//count/yeas")) or counts["yea"]
    nay = _int_or_none(_txt(".//count/nays")) or counts["nay"]
    present = _int_or_none(_txt(".//count/present")) or counts["present"]
    return SenateVote(
        question=_txt(".//vote_question_text") or _txt(".//question"),
        result=_txt(".//vote_result"),
        document=_txt(".//document_name") or _txt(".//document_title"),
        congress=_int_or_none(_txt(".//congress")),
        session=_int_or_none(_txt(".//session")),
        vote_number=_int_or_none(_txt(".//vote_number")),
        yea=yea, nay=nay, present=present, not_voting=counts["nv"],
        positions=positions,
        vote_date=_parse_senate_date(_txt(".//vote_date")),
    )


class SenateRollCallAdapter(SourceAdapter):
    name = "SenateRollCall"
    tier = 0
    base_url = "https://www.senate.gov/legislative/LIS/roll_call_votes"
    license = "Public domain (U.S. Government work)"

    def fetch(self, native_identifier: str, **params: Any) -> RawFetch:
        """``native_identifier`` is the full Senate roll-call XML URL."""
        content = client.get_bytes(native_identifier)
        return RawFetch(
            source_name=self.name, tier=self.tier, source_url=native_identifier,
            native_identifier=native_identifier, content=content,
            content_hash=sha256_hex(content), retrieved_at=datetime.now(UTC),
        )


senate_rollcall_adapter = SOURCE_ADAPTERS.register(SenateRollCallAdapter())
