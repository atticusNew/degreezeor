"""Generic official-URL adapter (Tier 0).

For action records that lack a dedicated API (e.g. a state legislature's official
bill page), this adapter fetches the official URL and lands the exact bytes with a
content hash, giving the action the same provenance guarantees as API-sourced ones.
A dedicated state-legislature adapter (OpenStates / per-state) replaces this later
behind the same SourceAdapter interface.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from degreezeor.core.hashing import sha256_hex
from degreezeor.core.interfaces import SOURCE_ADAPTERS, RawFetch, SourceAdapter
from degreezeor.ingestion.http import client


class GenericUrlAdapter(SourceAdapter):
    name = "OfficialURL"
    tier = 0
    base_url = ""
    license = "Official government source"

    def fetch(self, native_identifier: str, **params: Any) -> RawFetch:
        """``native_identifier`` is the official URL itself; ``params['label']`` optional."""
        url = native_identifier
        content = client.get_bytes(url)
        return RawFetch(
            source_name=self.name,
            tier=self.tier,
            source_url=url,
            native_identifier=params.get("label", url),
            content=content,
            content_hash=sha256_hex(content),
            retrieved_at=datetime.now(UTC),
        )


generic_url_adapter = SOURCE_ADAPTERS.register(GenericUrlAdapter())
