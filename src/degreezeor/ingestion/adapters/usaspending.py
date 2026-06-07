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

    def fetch_toptier_agencies(self) -> bytes:
        return client.get_bytes(f"{API}/references/toptier_agencies/")

    def fetch_agency_budget(self, toptier_code: str) -> RawFetch:
        """Agency budgetary resources by fiscal year: agency_budgetary_resources (the
        appropriation/authority available), agency_total_obligated, agency_total_outlayed.
        These are authoritative account-level figures (obligated/outlayed <= resources by
        construction), so execution rates are stable + commensurable — unlike DEFC award
        aggregates."""
        url = f"{API}/agency/{toptier_code}/budgetary_resources/"
        content = client.get_bytes(url)
        return RawFetch(
            source_name=self.name, tier=self.tier, source_url=url,
            native_identifier=f"AGENCYBUDGET:{toptier_code}", content=content,
            content_hash=sha256_hex(content), retrieved_at=datetime.now(UTC),
        )

    @staticmethod
    def parse_agency_budget(content: bytes, fiscal_year: int) -> dict[str, float] | None:
        for y in json.loads(content).get("agency_data_by_year", []):
            if y.get("fiscal_year") == fiscal_year:
                return {
                    "resources": float(y.get("agency_budgetary_resources") or 0.0),
                    "obligated": float(y.get("agency_total_obligated") or 0.0),
                    "outlayed": float(y.get("agency_total_outlayed") or 0.0),
                }
        return None

    def fetch_general_obligations(self, defc: str, start_year: int, end_year: int) -> RawFetch:
        """Total award OBLIGATIONS for any DEFC (incl. non-COVID) via spending_by_geography.

        Outlays are not available for non-COVID DEFCs, so delivery for those laws is
        measured as obligations vs the law's (curated, source-linked) appropriation.
        """
        body = {
            "scope": "place_of_performance", "geo_layer": "country",
            "filters": {"def_codes": [defc], "time_period": [
                {"start_date": f"{start_year}-10-01", "end_date": f"{end_year}-09-30"}]},
        }
        content = client.post_json(f"{API}/search/spending_by_geography/", body)
        return RawFetch(
            source_name=self.name, tier=self.tier,
            source_url=f"{API}/search/spending_by_geography/?def_codes={defc}",
            native_identifier=f"DEFCGEN:{defc}", content=content,
            content_hash=sha256_hex(content), retrieved_at=datetime.now(UTC),
        )

    @staticmethod
    def parse_general_obligation(content: bytes) -> float:
        res = json.loads(content).get("results", [])
        return float(res[0]["aggregated_amount"]) if res else 0.0

    @staticmethod
    def parse_amounts(content: bytes) -> dict[str, float]:
        d = json.loads(content)
        return {
            "obligation": float(d.get("obligation") or 0.0),
            "outlay": float(d.get("outlay") or 0.0),
            "award_count": int(d.get("award_count") or 0),
        }

    def def_codes(self) -> list[dict[str, Any]]:
        """Return DEFCs that map to exactly ONE public law (cleanly attributable).

        Each: {code, congress, law_number, title}. Multi-law DEFCs are skipped — their
        spending can't be attributed to a single law without overstatement.
        """
        import re

        content = client.get_bytes(f"{API}/references/def_codes/")
        out: list[dict[str, Any]] = []
        for c in json.loads(content).get("codes", []):
            laws = sorted(set(re.findall(r"P\.?L\.?\s*(\d+)-(\d+)", c.get("public_law") or "")))
            if len(laws) != 1:
                continue
            congress, num = laws[0]
            out.append({"code": c["code"], "congress": int(congress),
                        "law_number": int(num), "title": (c.get("title") or "").strip()})
        return out


usaspending_adapter = SOURCE_ADAPTERS.register(UsaSpendingAdapter())
