"""CourtListener adapter (Tier 0/1 — official court records, via the free CourtListener API).

INTEGRITY NOTE: this adapter fetches case METADATA only (case name, court, citation,
date, docket URL) for *provenance*. It does NOT infer legal dispositions by parsing
opinion text — disposition (upheld / partially struck / struck / pending) is curated
from the unambiguous public record and source-linked, so no fragile NLP enters the score.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from degreezeor.config import settings
from degreezeor.core.hashing import sha256_hex
from degreezeor.core.interfaces import SOURCE_ADAPTERS, RawFetch, SourceAdapter
from degreezeor.ingestion.http import client

API = "https://www.courtlistener.com/api/rest/v4"


class CourtListenerAdapter(SourceAdapter):
    name = "CourtListener"
    tier = 1  # official court records (via Free Law Project)
    base_url = API
    license = "CourtListener / Free Law Project"

    def fetch(self, native_identifier: str, **params: Any) -> RawFetch:
        """``native_identifier`` is a search query (e.g. 'Trump v. Hawaii 13780').

        Returns the top case-metadata match for provenance.
        """
        headers = {}
        token = getattr(settings, "courtlistener_token", "") or ""
        if token:
            headers["Authorization"] = f"Token {token}"
        url = f"{API}/search/"
        content = client.get_bytes(url, params={"q": native_identifier, "type": "o"}, headers=headers)
        return RawFetch(
            source_name=self.name, tier=self.tier,
            source_url=f"{API}/search/?q={native_identifier}&type=o",
            native_identifier=native_identifier, content=content,
            content_hash=sha256_hex(content), retrieved_at=datetime.now(UTC),
        )

    @staticmethod
    def top_case(content: bytes) -> dict[str, Any] | None:
        results = json.loads(content).get("results", [])
        if not results:
            return None
        r = results[0]
        url = r.get("absolute_url") or ""
        return {
            "case_name": r.get("caseName"),
            "court": r.get("court"),
            "citation": (r.get("citation") or [None])[0] if isinstance(r.get("citation"), list)
            else r.get("citation"),
            "date_filed": r.get("dateFiled"),
            "url": f"https://www.courtlistener.com{url}" if url.startswith("/") else url,
        }


courtlistener_adapter = SOURCE_ADAPTERS.register(CourtListenerAdapter())
