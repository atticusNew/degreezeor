"""House Clerk roll-call vote adapter (Tier 0, keyless).

Fetches and parses official House roll-call XML (clerk.house.gov/evs/...). The
legislator ``name-id`` IS the Bioguide ID, so member positions resolve cleanly to
our official records. Used to attach decisive-vote attribution to the members who
voted a law through.
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
class MemberVote:
    bioguide_id: str
    name: str
    party: str | None
    state: str | None
    position: str  # yea | nay | present | nv


@dataclass(frozen=True)
class HouseVote:
    question: str
    result: str
    legis_num: str
    description: str
    yea: int
    nay: int
    present: int
    not_voting: int
    positions: list[MemberVote]
    congress: int | None = None
    rollcall_num: int | None = None
    vote_date: date | None = None

    @property
    def margin(self) -> int:
        return abs(self.yea - self.nay)

    @property
    def passed(self) -> bool:
        return self.yea > self.nay


_POS = {"yea": "yea", "aye": "yea", "yes": "yea", "nay": "nay", "no": "nay",
        "present": "present", "not voting": "nv", "": "nv"}

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def _parse_house_date(raw: str) -> date | None:
    """Parse the Clerk's ``action-date`` (e.g. '3-Jan-2025') into a date."""
    parts = (raw or "").strip().split("-")
    if len(parts) != 3:
        return None
    try:
        day = int(parts[0])
        month = _MONTHS.get(parts[1][:3].title())
        year = int(parts[2])
        return date(year, month, day) if month else None
    except (ValueError, TypeError):
        return None


def _int_or_none(s: str) -> int | None:
    s = (s or "").strip()
    return int(s) if s.isdigit() else None


def parse_house_vote(xml_bytes: bytes) -> HouseVote:
    root = ET.fromstring(xml_bytes)
    md = root.find(".//vote-metadata")

    def _txt(tag: str) -> str:
        e = md.find(tag) if md is not None else None
        return (e.text or "").strip() if e is not None else ""

    positions: list[MemberVote] = []
    counts = {"yea": 0, "nay": 0, "present": 0, "nv": 0}
    for rec in root.findall(".//recorded-vote"):
        leg = rec.find("legislator")
        vote = rec.find("vote")
        if leg is None or vote is None:
            continue
        norm = _POS.get((vote.text or "").strip().lower(), "nv")
        counts[norm] += 1
        positions.append(MemberVote(
            bioguide_id=leg.get("name-id", ""),
            name=leg.get("unaccented-name") or (leg.text or "").strip(),
            party=leg.get("party"), state=leg.get("state"), position=norm,
        ))
    return HouseVote(
        question=_txt("vote-question"), result=_txt("vote-result"),
        legis_num=_txt("legis-num"), description=_txt("vote-desc"),
        yea=counts["yea"], nay=counts["nay"], present=counts["present"],
        not_voting=counts["nv"], positions=positions,
        congress=_int_or_none(_txt("congress")),
        rollcall_num=_int_or_none(_txt("rollcall-num")),
        vote_date=_parse_house_date(_txt("action-date")),
    )


class HouseClerkAdapter(SourceAdapter):
    name = "HouseClerk"
    tier = 0
    base_url = "https://clerk.house.gov/evs"
    license = "Public domain (U.S. Government work)"

    def fetch(self, native_identifier: str, **params: Any) -> RawFetch:
        """``native_identifier`` is the full roll-call XML URL."""
        content = client.get_bytes(native_identifier)
        return RawFetch(
            source_name=self.name, tier=self.tier, source_url=native_identifier,
            native_identifier=native_identifier, content=content,
            content_hash=sha256_hex(content), retrieved_at=datetime.now(UTC),
        )


house_clerk_adapter = SOURCE_ADAPTERS.register(HouseClerkAdapter())
