"""USAspending adapter (Tier 1 — authoritative federal spending).

Keyless. Exposes realized award spending tagged to a specific law via its Disaster
Emergency Fund Code (DEFC) — i.e. the law's OWN money, which makes it a *directly
attributable* realized series for target-relative scoring ("did the policy deliver
the funds it committed?").
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from degreezeor.core.hashing import sha256_hex
from degreezeor.core.interfaces import SOURCE_ADAPTERS, RawFetch, SourceAdapter
from degreezeor.ingestion.http import client

API = "https://api.usaspending.gov/api/v2"


class UsaSpendingAdapter(SourceAdapter):
    name = "USAspending"
    tier = 1
    base_url = API
    license = "Public domain (U.S. Government work)"

    def fetch(self, native_identifier: str, **params: Any) -> RawFetch:
        """``native_identifier`` is a DEFC (e.g. 'V' for ARP, 'N' for CARES).

        Returns the disaster award-amount totals (obligation, outlay, award_count).
        """
        defc = native_identifier
        url = f"{API}/disaster/award/amount/"
        body = {"filter": {"def_codes": [defc]}}
        content = client.post_json(url, body)
        public_url = f"{url}?def_codes={defc}"
        return RawFetch(
            source_name=self.name, tier=self.tier, source_url=public_url,
            native_identifier=f"DEFC:{defc}", content=content,
            content_hash=sha256_hex(content), retrieved_at=datetime.now(UTC),
        )

    @staticmethod
    def parse_amounts(content: bytes) -> dict[str, float]:
        d = json.loads(content)
        return {
            "obligation": float(d.get("obligation") or 0.0),
            "outlay": float(d.get("outlay") or 0.0),
            "award_count": int(d.get("award_count") or 0),
        }


usaspending_adapter = SOURCE_ADAPTERS.register(UsaSpendingAdapter())
