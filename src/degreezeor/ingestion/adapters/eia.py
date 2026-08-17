"""EIA adapter (Tier 1 — official energy statistics).

Uses the EIA v2 data API (key required: ``DZ_EIA_API_KEY``). A metric's
``native_series_id`` encodes the API route + the series (``msn``) facet:

    EIA|<route>|<msn>

e.g. total U.S. energy CO2 emissions (annual, million metric tons):
    EIA|total-energy|TETCEUS
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

API = "https://api.eia.gov/v2"


def parse_encoding(native_series_id: str) -> tuple[str, dict[str, str]]:
    """EIA|<route>|<facets>. ``facets`` is either a single ``msn`` value (legacy national
    series, e.g. 'TETCEUS') or a generic ``key=val;key=val`` spec (e.g. state CO2:
    'stateId=CA;sectorId=TT;fuelId=TO'). Returns (route, {facet: value})."""
    parts = native_series_id.split("|")
    if len(parts) != 3 or parts[0] != "EIA":
        raise ValueError(f"not an EIA series id: {native_series_id!r}")
    route, spec = parts[1], parts[2]
    if "=" in spec:
        facets = {}
        for pair in spec.split(";"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                facets[k.strip()] = v.strip()
        return route, facets
    return route, {"msn": spec}  # legacy national series


class EIAAdapter(SourceAdapter):
    name = "EIA"
    tier = 1
    base_url = API
    license = "Public domain (U.S. Government work)"

    def fetch(self, native_identifier: str, *, start_year: int = 1990,
              end_year: int = 2100, **params: Any) -> RawFetch:
        route, facets = parse_encoding(native_identifier)
        q = [
            ("frequency", "annual"), ("data[0]", "value"),
            *[(f"facets[{k}][]", v) for k, v in facets.items()],
            ("start", str(start_year)), ("end", str(end_year)),
            ("sort[0][column]", "period"), ("sort[0][direction]", "asc"),
        ]
        public_url = f"{API}/{route}/data/?{urlencode(q)}"  # key-less URL for the audit trail
        content = client.get_bytes(f"{public_url}&api_key={settings.eia_api_key}")
        return RawFetch(
            source_name=self.name, tier=self.tier, source_url=public_url,
            native_identifier=native_identifier, content=content,
            content_hash=sha256_hex(content), retrieved_at=datetime.now(UTC),
        )

    @staticmethod
    def parse_series(content: bytes, native_series_id: str) -> list[tuple[str, str]]:
        data = json.loads(content).get("response", {}).get("data", [])
        out: list[tuple[str, str]] = []
        for r in data:
            period, val = r.get("period"), r.get("value")
            if period is None or val is None:
                continue
            out.append((str(period)[:4], str(val)))
        out.sort(key=lambda t: t[0])
        return out


eia_adapter = SOURCE_ADAPTERS.register(EIAAdapter())
