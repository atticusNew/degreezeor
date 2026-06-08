"""CDC adapter (Tier 1 — official health-outcome statistics).

Uses the keyless CDC Socrata open-data API (data.cdc.gov). A metric's
``native_series_id`` encodes the dataset + the exact column to read and any fixed
filters, so one generic adapter serves many CDC series:

    CDC|<resource_id>|<year_field>|<value_field>|<filter_field=value;...>

e.g. life expectancy at birth (national, all races, both sexes):
    CDC|w9j2-ggv5|year|average_life_expectancy|race=All Races;sex=Both Sexes
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from degreezeor.core.hashing import sha256_hex
from degreezeor.core.interfaces import SOURCE_ADAPTERS, RawFetch, SourceAdapter
from degreezeor.ingestion.http import client

API = "https://data.cdc.gov/resource"


def parse_encoding(native_series_id: str) -> tuple[str, str, str, dict[str, str]]:
    """``CDC|resource|year_field|value_field|f1=v1;f2=v2`` -> parts."""
    parts = native_series_id.split("|")
    if len(parts) < 4 or parts[0] != "CDC":
        raise ValueError(f"not a CDC series id: {native_series_id!r}")
    resource, year_field, value_field = parts[1], parts[2], parts[3]
    filters: dict[str, str] = {}
    if len(parts) >= 5 and parts[4]:
        for kv in parts[4].split(";"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                filters[k.strip()] = v.strip()
    return resource, year_field, value_field, filters


class CDCAdapter(SourceAdapter):
    name = "CDC"
    tier = 1
    base_url = API
    license = "Public domain (U.S. Government work)"

    def fetch(self, native_identifier: str, *, start_year: int = 1900,
              end_year: int = 2100, **params: Any) -> RawFetch:
        resource, year_field, value_field, filters = parse_encoding(native_identifier)
        where = [f"{year_field} >= '{start_year}'", f"{year_field} <= '{end_year}'"]
        for k, v in filters.items():
            where.append(f"{k} = '{v}'")
        q = {
            "$select": f"{year_field},{value_field}",
            "$where": " AND ".join(where),
            "$order": year_field,
            "$limit": "50000",
        }
        url = f"{API}/{resource}.json"
        content = client.get_bytes(url, params=q)
        # A stable public URL for the source trail (Socrata accepts these query params).
        from urllib.parse import urlencode
        public_url = f"{url}?{urlencode(q)}"
        return RawFetch(
            source_name=self.name, tier=self.tier, source_url=public_url,
            native_identifier=native_identifier, content=content,
            content_hash=sha256_hex(content), retrieved_at=datetime.now(UTC),
        )

    @staticmethod
    def parse_series(content: bytes, native_series_id: str) -> list[tuple[str, str]]:
        """Return sorted [(year, value)] for the encoded value field, skipping nulls."""
        _resource, year_field, value_field, _filters = parse_encoding(native_series_id)
        rows = json.loads(content)
        out: list[tuple[str, str]] = []
        for r in rows:
            year, val = r.get(year_field), r.get(value_field)
            if year is None or val is None:
                continue
            out.append((str(year)[:4], str(val)))
        out.sort(key=lambda t: t[0])
        return out


cdc_adapter = SOURCE_ADAPTERS.register(CDCAdapter())
