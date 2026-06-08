"""Census adapter (Tier 1 — official socioeconomic statistics).

Uses the Census Bureau data API (key required: ``DZ_CENSUS_API_KEY``). A metric's
``native_series_id`` encodes the timeseries dataset path + the variable to read:

    CENSUS|<dataset_path>|<variable>

e.g. national poverty rate (SAIPE):  CENSUS|timeseries/poverty/saipe|SAEPOVRTALL_PT
     median household income (SAIPE): CENSUS|timeseries/poverty/saipe|SAEMHI_PT
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from degreezeor.config import settings
from degreezeor.core.hashing import sha256_hex
from degreezeor.core.interfaces import SOURCE_ADAPTERS, RawFetch, SourceAdapter
from degreezeor.ingestion.http import client

API = "https://api.census.gov/data"


def parse_encoding(native_series_id: str) -> tuple[str, str]:
    parts = native_series_id.split("|")
    if len(parts) != 3 or parts[0] != "CENSUS":
        raise ValueError(f"not a Census series id: {native_series_id!r}")
    return parts[1], parts[2]  # (dataset_path, variable)


class CensusAdapter(SourceAdapter):
    name = "Census"
    tier = 1
    base_url = API
    license = "Public domain (U.S. Government work)"

    def fetch(self, native_identifier: str, *, start_year: int = 1990,
              end_year: int = 2100, **params: Any) -> RawFetch:
        path, variable = parse_encoding(native_identifier)
        # Pre-encode with quote_plus (spaces -> '+') so the Census "time=from+X+to+Y"
        # range filter is accepted exactly as the API expects.
        query = urlencode({
            "get": variable, "for": "us:*",
            "time": f"from {start_year} to {end_year}",
        })
        public_url = f"{API}/{path}?{query}"  # key-less URL for the audit trail
        content = client.get_bytes(f"{public_url}&key={settings.census_api_key}")
        return RawFetch(
            source_name=self.name, tier=self.tier, source_url=public_url,
            native_identifier=native_identifier, content=content,
            content_hash=sha256_hex(content), retrieved_at=datetime.now(UTC),
        )

    @staticmethod
    def parse_series(content: bytes, native_series_id: str) -> list[tuple[str, str]]:
        _path, variable = parse_encoding(native_series_id)
        rows = json.loads(content)
        if not rows:
            return []
        header = rows[0]
        vi, ti = header.index(variable), header.index("time")
        out: list[tuple[str, str]] = []
        for r in rows[1:]:
            year, val = r[ti], r[vi]
            if year is None or val is None:
                continue
            out.append((str(year)[:4], str(val)))
        out.sort(key=lambda t: t[0])
        return out


census_adapter = SOURCE_ADAPTERS.register(CensusAdapter())
