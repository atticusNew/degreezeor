"""Provenance helpers: git SHA and data-snapshot identity for reproducible runs."""

from __future__ import annotations

import subprocess

from degreezeor.core.hashing import hash_payload


def current_git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def data_snapshot_id(content_hashes: list[str]) -> str:
    """Stable identity of the exact input bytes used by a score run."""
    return hash_payload(sorted(set(content_hashes)))
