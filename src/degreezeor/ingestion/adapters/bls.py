"""BLS adapter (Tier 1 — official labor/price outcome series).

Uses the BLS Public Data API v2 (keyless for low volume; set ``DZ_BLS_API_KEY`` to
raise limits). Returns raw JSON for a series over a year range for landing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from degreezeor.config import settings
from degreezeor.core.hashing import sha256_hex
from degreezeor.core.interfaces import SOURCE_ADAPTERS, RawFetch, SourceAdapter
from degreezeor.ingestion.http import client

V2 = "https://api.bls.gov/publicAPI/v2/timeseries/data"


class BlsAdapter(SourceAdapter):
    name = "BLS"
    tier = 1
    base_url = V2
    license = "Public domain (U.S. Government work)"

    def fetch(self, native_identifier: str, **params: Any) -> RawFetch:
        """``native_identifier`` is a BLS series id, e.g. 'LNS14000000' (unemployment rate)."""
        start_year = int(params["start_year"])
        end_year = int(params["end_year"])
        url = f"{V2}/{native_identifier}"
        q: dict[str, str] = {"startyear": str(start_year), "endyear": str(end_year)}
        if settings.bls_api_key:
            q["registrationkey"] = settings.bls_api_key
        content = client.get_bytes(url, params=q)
        # Validate the API actually returned data (BLS returns 200 + REQUEST_NOT_PROCESSED on errors).
        doc = json.loads(content)
        if doc.get("status") != "REQUEST_SUCCEEDED":
            raise RuntimeError(f"BLS request failed for {native_identifier}: {doc.get('message')}")
        public_url = f"{url}?startyear={start_year}&endyear={end_year}"
        return RawFetch(
            source_name=self.name,
            tier=self.tier,
            source_url=public_url,
            native_identifier=native_identifier,
            content=content,
            content_hash=sha256_hex(content),
            retrieved_at=datetime.now(UTC),
        )


bls_adapter = SOURCE_ADAPTERS.register(BlsAdapter())
