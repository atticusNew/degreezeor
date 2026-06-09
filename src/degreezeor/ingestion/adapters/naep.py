"""NAEP adapter (Tier 1 — official educational-outcome statistics).

Uses the keyless NAEP Data Service (nationsreportcard.gov), the U.S. Department of
Education's authoritative assessment. A metric's ``native_series_id`` encodes the
assessment + jurisdiction, so one generic adapter serves reading/math at any grade:

    NAEP|<subject>|<grade>|<subscale>|<jurisdiction>

e.g. Grade-4 reading mean scale score for Mississippi:
    NAEP|reading|4|RRPCM|MS

The Data Service reports state means only in NAEP administration years (roughly
biennial), so this is an annual series with gaps; the comparison design handles the
spacing via its pre-window length.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from degreezeor.core.hashing import sha256_hex
from degreezeor.core.interfaces import SOURCE_ADAPTERS, RawFetch, SourceAdapter
from degreezeor.ingestion.http import client

API = "https://www.nationsreportcard.gov/Dataservice/GetAdhocData.aspx"
# Main NAEP assessment years for reading/math at grades 4 and 8.
_NAEP_YEARS = [1992, 1994, 1998, 2000, 2002, 2003, 2005, 2007, 2009, 2011, 2013,
               2015, 2017, 2019, 2022, 2024]


def parse_encoding(native_series_id: str) -> tuple[str, str, str, str]:
    """``NAEP|subject|grade|subscale|jurisdiction`` -> (subject, grade, subscale, juris)."""
    parts = native_series_id.split("|")
    if len(parts) != 5 or parts[0] != "NAEP":
        raise ValueError(f"not a NAEP series id: {native_series_id!r}")
    return parts[1], parts[2], parts[3], parts[4]


class NAEPAdapter(SourceAdapter):
    name = "NAEP"
    tier = 1
    base_url = API
    license = "Public domain (U.S. Government work)"

    def fetch(self, native_identifier: str, *, start_year: int = 1990,
              end_year: int = 2100, **params: Any) -> RawFetch:
        subject, grade, subscale, juris = parse_encoding(native_identifier)
        years = [y for y in _NAEP_YEARS if start_year <= y <= end_year]
        q = {
            "type": "data", "subject": subject, "grade": grade, "subscale": subscale,
            "variable": "TOTAL", "jurisdiction": juris, "stattype": "MN:MN",
            "Year": ",".join(str(y) for y in years),
        }
        content = client.get_bytes(API, params=q)
        from urllib.parse import urlencode
        public_url = f"{API}?{urlencode(q)}"
        return RawFetch(
            source_name=self.name, tier=self.tier, source_url=public_url,
            native_identifier=native_identifier, content=content,
            content_hash=sha256_hex(content), retrieved_at=datetime.now(UTC),
        )

    @staticmethod
    def parse_series(content: bytes, native_series_id: str) -> list[tuple[str, str]]:
        """Return sorted [(year, mean_scale_score)], skipping suppressed/flagged points."""
        doc = json.loads(content)
        out: list[tuple[str, str]] = []
        for r in doc.get("result", []):
            year, val = r.get("year"), r.get("value")
            flag = r.get("errorFlag")
            if year is None or val is None:
                continue
            # errorFlag 0 (or "0") means a valid, unsuppressed estimate.
            if flag not in (0, "0", None):
                continue
            out.append((str(year)[:4], str(val)))
        out.sort(key=lambda t: t[0])
        return out


naep_adapter = SOURCE_ADAPTERS.register(NAEPAdapter())
