"""Deterministic content hashing.

Canonical JSON (sorted keys, no insignificant whitespace, stable Decimal/str
coercion) ensures the same logical content always yields the same sha256 — the
basis for content-addressed snapshots, pre-registration commits, score-run
reproducibility hashes, and the audit hash chain.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any


def _default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        # Normalize so 0.10 and 0.1 hash identically.
        return format(obj.normalize(), "f")
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(f"Unhashable type for canonical JSON: {type(obj)!r}")


def canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_default,
    )


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def hash_payload(payload: Any) -> str:
    """sha256 of the canonical JSON encoding of ``payload``."""
    return sha256_hex(canonical_json(payload))
