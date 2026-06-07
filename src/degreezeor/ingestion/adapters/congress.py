"""Congress.gov adapter (Tier 0 — the primary record of legislative actions).

Uses the official api.congress.gov v3 API. The shared ``DEMO_KEY`` works for the
MVP slice; set ``DZ_CONGRESS_API_KEY`` for higher rate limits. Raw JSON is returned
for landing; parsing into Action/Law/Bill rows happens in the loader.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from degreezeor.config import settings
from degreezeor.core.hashing import sha256_hex
from degreezeor.core.interfaces import SOURCE_ADAPTERS, RawFetch, SourceAdapter
from degreezeor.ingestion.http import client

API_ROOT = "https://api.congress.gov/v3"


class CongressGovAdapter(SourceAdapter):
    name = "Congress.gov"
    tier = 0
    base_url = API_ROOT
    license = "Public domain (U.S. Government work)"

    def _get(self, path: str, native_identifier: str, params: dict | None = None) -> RawFetch:
        params = dict(params or {})
        params["api_key"] = settings.congress_api_key
        params.setdefault("format", "json")
        url = f"{API_ROOT}/{path}"
        content = client.get_bytes(url, params=params)
        # Public URL recorded WITHOUT the api_key (so the trail is shareable/auditable).
        public_url = f"{url}?format=json"
        return RawFetch(
            source_name=self.name,
            tier=self.tier,
            source_url=public_url,
            native_identifier=native_identifier,
            content=content,
            content_hash=sha256_hex(content),
            retrieved_at=datetime.now(UTC),
        )

    def fetch(self, native_identifier: str, **params: Any) -> RawFetch:
        """``native_identifier`` is the API path, e.g. 'law/111/5' or 'bill/111/hr/1'."""
        return self._get(native_identifier, native_identifier, params)

    # Convenience typed fetchers -------------------------------------------------
    def fetch_law(self, congress: int, law_number: int, law_type: str = "pub") -> RawFetch:
        return self._get(
            f"law/{congress}/{law_type}/{law_number}",
            f"law/{congress}/{law_type}/{law_number}",
        )

    def fetch_bill(self, congress: int, bill_type: str, bill_number: int) -> RawFetch:
        return self._get(
            f"bill/{congress}/{bill_type}/{bill_number}",
            f"bill/{congress}/{bill_type}/{bill_number}",
        )

    def fetch_bill_summaries(self, congress: int, bill_type: str, bill_number: int) -> RawFetch:
        return self._get(
            f"bill/{congress}/{bill_type}/{bill_number}/summaries",
            f"bill/{congress}/{bill_type}/{bill_number}/summaries",
        )

    def fetch_law_list(self, congress: int, limit: int = 250, offset: int = 0) -> RawFetch:
        return self._get(f"law/{congress}", f"law/{congress}",
                         params={"limit": limit, "offset": offset})

    def fetch_bill_actions(self, congress: int, bill_type: str, bill_number: int) -> RawFetch:
        return self._get(
            f"bill/{congress}/{bill_type}/{bill_number}/actions",
            f"bill/{congress}/{bill_type}/{bill_number}/actions",
            params={"limit": 250},
        )


congress_adapter = SOURCE_ADAPTERS.register(CongressGovAdapter())
