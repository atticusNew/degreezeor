"""Federal Register adapter (Tier 0 — the primary record of executive actions).

Uses the official, keyless federalregister.gov API. Provides executive orders
(and other presidential documents / rules) with their official identifiers,
signing date, signing president, and abstract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from degreezeor.core.hashing import sha256_hex
from degreezeor.core.interfaces import SOURCE_ADAPTERS, RawFetch, SourceAdapter
from degreezeor.ingestion.http import client

API = "https://www.federalregister.gov/api/v1"


class FederalRegisterAdapter(SourceAdapter):
    name = "FederalRegister"
    tier = 0
    base_url = API
    license = "Public domain (U.S. Government work)"

    def fetch(self, native_identifier: str, **params: Any) -> RawFetch:
        """``native_identifier`` is the FR document number, e.g. '2021-09263'.

        The individual-document endpoint returns the full field set (title, abstract,
        executive_order_number, signing_date, president, citation, ...) by default.
        """
        url = f"{API}/documents/{native_identifier}.json"
        content = client.get_bytes(url)
        return RawFetch(
            source_name=self.name,
            tier=self.tier,
            source_url=f"https://www.federalregister.gov/documents/{native_identifier}",
            native_identifier=native_identifier,
            content=content,
            content_hash=sha256_hex(content),
            retrieved_at=datetime.now(UTC),
        )

    def find_executive_order(self, eo_number: int) -> str | None:
        """Resolve an EO number to its FR document number (Tier-0 lookup)."""
        import json

        url = f"{API}/documents.json"
        params = {
            "conditions[type][]": "PRESDOCU",
            "conditions[presidential_document_type][]": "executive_order",
            "conditions[term]": str(eo_number),
            "per_page": "20",
        }
        content = client.get_bytes(url, params=params)
        results = json.loads(content).get("results", [])
        for r in results:
            if str(r.get("executive_order_number")) == str(eo_number):
                return r.get("document_number")
        return results[0].get("document_number") if results else None


federal_register_adapter = SOURCE_ADAPTERS.register(FederalRegisterAdapter())
