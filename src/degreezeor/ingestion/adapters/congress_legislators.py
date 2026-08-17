"""lis ↔ bioguide crosswalk (Tier 3 — entity-resolution metadata only).

Senate roll-call XML keys members by ``lis_member_id`` while every other record in the
system keys on the Bioguide ID. The community-maintained ``unitedstates/congress-legislators``
dataset publishes both IDs per legislator, so it is used STRICTLY to bridge those two
identifiers — never as a source of any scored quantity. The authoritative vote record is
the Tier-0 Senate XML; this crosswalk only resolves *which* of our officials cast it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from degreezeor.core.hashing import sha256_hex
from degreezeor.core.interfaces import SOURCE_ADAPTERS, RawFetch, SourceAdapter

_BASE = "https://unitedstates.github.io/congress-legislators"
# Current + historical together cover every senator who has ever cast a recorded vote.
CROSSWALK_URLS = (f"{_BASE}/legislators-current.json", f"{_BASE}/legislators-historical.json")

# Process-level cache: the historical file is ~13 MB, so parse it at most once per run.
_LIS_TO_BIOGUIDE: dict[str, str] | None = None


def build_lis_bioguide_map(contents: list[bytes]) -> dict[str, str]:
    """Build ``{lis_member_id: bioguide_id}`` from raw legislators JSON payloads."""
    mapping: dict[str, str] = {}
    for raw in contents:
        for person in json.loads(raw):
            ids = person.get("id", {})
            lis, bioguide = ids.get("lis"), ids.get("bioguide")
            if lis and bioguide:
                mapping[lis] = bioguide
    return mapping


class CongressLegislatorsAdapter(SourceAdapter):
    name = "CongressLegislators"
    tier = 3  # convenience crosswalk; used only for identifier resolution, never as a datum
    base_url = _BASE
    license = "CC0 (public domain dedication)"

    def fetch(self, native_identifier: str, **params: Any) -> RawFetch:
        """``native_identifier`` is one of the CROSSWALK_URLS."""
        from degreezeor.ingestion.http import client

        content = client.get_bytes(native_identifier)
        return RawFetch(
            source_name=self.name, tier=self.tier, source_url=native_identifier,
            native_identifier=native_identifier, content=content,
            content_hash=sha256_hex(content), retrieved_at=datetime.now(UTC),
        )

    def current_bioguide_ids(self) -> set[str]:
        """The Bioguide IDs of legislators CURRENTLY in office (the `legislators-current`
        roster). Used only to flag 'in office' for display — never read by scoring."""
        from degreezeor.ingestion.http import client

        raw = client.get_bytes(f"{_BASE}/legislators-current.json")
        return {
            p["id"]["bioguide"] for p in json.loads(raw)
            if p.get("id", {}).get("bioguide")
        }

    def lis_to_bioguide(self) -> dict[str, str]:
        """Fetch (cache-first) + build the crosswalk, memoised for the process."""
        global _LIS_TO_BIOGUIDE
        if _LIS_TO_BIOGUIDE is None:
            from degreezeor.ingestion.http import client

            _LIS_TO_BIOGUIDE = build_lis_bioguide_map(
                [client.get_bytes(url) for url in CROSSWALK_URLS]
            )
        return _LIS_TO_BIOGUIDE


congress_legislators_adapter = SOURCE_ADAPTERS.register(CongressLegislatorsAdapter())
