"""Live end-to-end test against real official APIs + Postgres.

Skipped unless ``DZ_RUN_LIVE=1`` (needs network + a running Postgres at
DZ_DATABASE_URL). Proves the full path ingests real data, scores, and produces a
bit-reproducible hash across two independent runs.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("DZ_RUN_LIVE") != "1", reason="live test (set DZ_RUN_LIVE=1)"
)


def _fresh_session():
    from degreezeor.core.db import SessionLocal, engine
    from degreezeor.core.models import Base

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return SessionLocal()


def test_arra_scores_and_is_reproducible() -> None:
    from degreezeor.core import audit
    from degreezeor.pipeline import score_law

    s1 = _fresh_session()
    try:
        r1 = score_law(s1, 111, 5)
        s1.commit()
        ok, broken = audit.verify_chain(s1)
        assert ok and broken is None
    finally:
        s1.close()

    s2 = _fresh_session()
    try:
        r2 = score_law(s2, 111, 5)
        s2.commit()
    finally:
        s2.close()

    assert r1.reproducible_hash is not None
    assert r1.reproducible_hash == r2.reproducible_hash
    # ARRA on a naive single-series baseline must NOT be over-claimed.
    assert r1.status == "insufficient_evidence"


def test_state_policy_synthetic_control_clears_gate_and_is_reproducible() -> None:
    from degreezeor.pipeline import STATE_POLICIES, score_state_policy

    s1 = _fresh_session()
    try:
        r1 = score_state_policy(s1, STATE_POLICIES["KS-HB2117"])
        s1.commit()
    finally:
        s1.close()

    s2 = _fresh_session()
    try:
        r2 = score_state_policy(s2, STATE_POLICIES["KS-HB2117"])
        s2.commit()
    finally:
        s2.close()

    # Synthetic control on real BLS state data is well-identified here, so the gate
    # is cleared and a composite is produced — and the run is bit-reproducible.
    assert r1.status == "scored"
    assert r1.reproducible_hash == r2.reproducible_hash
