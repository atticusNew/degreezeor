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


def _test_database_url() -> str:
    """A DEDICATED live-test database, derived from the configured DSN, so running
    live tests NEVER drops the developer's demo/working database."""
    from degreezeor.config import settings

    url = settings.database_url
    base, _, db = url.rpartition("/")
    return f"{base}/{db}_livetest"


def _ensure_test_db(url: str) -> None:
    from sqlalchemy import create_engine, text

    base, _, dbname = url.rpartition("/")
    admin = create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        exists = c.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": dbname}
        ).scalar()
        if not exists:
            c.execute(text(f'CREATE DATABASE "{dbname}"'))
    admin.dispose()


def _fresh_session():
    from sqlalchemy.orm import sessionmaker

    from degreezeor.core.models import Base

    url = _test_database_url()
    _ensure_test_db(url)
    from sqlalchemy import create_engine

    engine = create_engine(url, pool_pre_ping=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_arra_scores_and_is_reproducible() -> None:
    from sqlalchemy import select

    from degreezeor.core import audit
    from degreezeor.core.models import AttributionWeight, Vote
    from degreezeor.pipeline import score_law

    s1 = _fresh_session()
    try:
        r1 = score_law(s1, 111, 5)
        s1.commit()
        ok, broken = audit.verify_chain(s1)
        assert ok and broken is None
        # BOTH chambers' final-passage roll-calls are ingested (House + Senate),
        # and winning-side voters in each chamber receive decisive-vote attribution.
        chambers = set(s1.execute(select(Vote.chamber).where(Vote.action_id == r1.action_id)).scalars())
        assert {"house", "senate"} <= chambers
        decisive = s1.execute(
            select(AttributionWeight).where(
                AttributionWeight.eu_id == r1.eu_id, AttributionWeight.role == "decisive_vote"
            )
        ).scalars().all()
        # ARRA: 246 House + 60 Senate winning-side votes.
        assert len(decisive) == 246 + 60
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


def test_dispute_reproducible_rerun_upholds_score() -> None:
    import os

    from degreezeor.disputes import file_dispute, resolve_dispute
    from degreezeor.pipeline import STATE_POLICIES, score_state_policy

    s = _fresh_session()
    try:
        r = score_state_policy(s, STATE_POLICIES["KS-HB2117"])
        s.commit()
        assert r.status == "scored"
        d = file_dispute(s, eu_id=r.eu_id, filer="watchdog@example.org",
                         claim="The synthetic control donor pool is biased.")
        s.commit()
        # Independent, deterministic re-run (donors replayed from cache).
        os.environ["DZ_HTTP_CACHE"] = "1"
        try:
            res = resolve_dispute(s, dispute_id=d.id)
            s.commit()
        finally:
            os.environ.pop("DZ_HTTP_CACHE", None)
        # The score reproduces exactly => the challenge is resolved "upheld", not edited.
        assert res.reproduced is True
        assert res.status == "resolved_upheld"
    finally:
        s.close()


def test_noncovid_delivery_integrity_guards() -> None:
    """IIJA (stable, commensurable) scores; Ukraine supplemental (window-unstable) is
    auto-rejected — the platform refuses fragile spending data rather than publish it."""
    import os

    from degreezeor.pipeline import TARGET_SPECS, score_target

    os.environ["DZ_HTTP_CACHE"] = "1"
    s = _fresh_session()
    try:
        iija = score_target(s, TARGET_SPECS["IIJA-DELIVERY"])
        ukr = score_target(s, TARGET_SPECS["UKRAINE-2022-DELIVERY"])
        s.commit()
        assert iija.status == "scored"  # stable + commensurable
        assert ukr.status.startswith("non_scoreable")  # window-unstable => rejected
    finally:
        s.close()
        os.environ.pop("DZ_HTTP_CACHE", None)


def test_cares_delivery_credits_passage_voters_and_reproduces() -> None:
    """A delivery-scored law credits the legislators who PASSED it (both chambers), and
    the run — including that vote-based attribution — reproduces bit-for-bit."""
    import os

    from sqlalchemy import select

    from degreezeor.core.models import AttributionWeight, ScoreRun, Vote
    from degreezeor.pipeline import TARGET_SPECS, rescore_eu, score_target

    os.environ["DZ_HTTP_CACHE"] = "1"
    s = _fresh_session()
    try:
        r = score_target(s, TARGET_SPECS["CARES-DELIVERY"])
        s.commit()
        assert r.status == "scored"  # directly-attributable delivery clears the gate
        chambers = set(s.execute(select(Vote.chamber).where(Vote.action_id == r.action_id)).scalars())
        assert {"house", "senate"} <= chambers  # CARES: House 419-6 + Senate 96-0
        decisive = s.execute(
            select(AttributionWeight).where(
                AttributionWeight.eu_id == r.eu_id, AttributionWeight.role == "decisive_vote"
            )
        ).scalars().all()
        assert len(decisive) > 400  # hundreds of passing legislators now connected
        run = s.execute(
            select(ScoreRun).where(ScoreRun.eu_id == r.eu_id).order_by(ScoreRun.id.desc()).limit(1)
        ).scalar_one()
        assert run.reproducible_hash == rescore_eu(s, r.eu_id).reproducible_hash
    finally:
        s.close()
        os.environ.pop("DZ_HTTP_CACHE", None)


def test_budget_execution_scores_and_reproduces() -> None:
    from sqlalchemy import select

    from degreezeor.core.models import ScoreRun
    from degreezeor.pipeline import rescore_eu, score_budget_execution

    s = _fresh_session()
    try:
        r = score_budget_execution(s, "036", "Department of Veterans Affairs", 2024, "obligated")
        s.commit()
        assert r.status == "scored"  # execution rate is verifiable + directly attributable
        run = s.execute(
            select(ScoreRun).where(ScoreRun.eu_id == r.eu_id).order_by(ScoreRun.id.desc()).limit(1)
        ).scalar_one()
        assert run.reproducible_hash == rescore_eu(s, r.eu_id).reproducible_hash
    finally:
        s.close()


def test_regulation_ingests_scores_and_reproduces() -> None:
    """A final agency rule (Federal Register) ingests as Action(type='regulation'),
    is attributed to the administration on its effective date (executive authority), and
    reproduces — including the president re-derived from the action date on re-run."""
    from sqlalchemy import select

    from degreezeor.core.models import Action, AttributionWeight, ScoreRun
    from degreezeor.pipeline import rescore_eu, score_regulation

    s = _fresh_session()
    try:
        # DOL overtime exemptions final rule (2024), effective 2024-07-01.
        r = score_regulation(s, "2024-08038")
        s.commit()
        action = s.get(Action, r.action_id)
        assert action.type == "regulation"
        assert action.native_identifier == "REG2024-08038"
        # Attributed to the administration via the regulation signer channel.
        signer = s.execute(
            select(AttributionWeight).where(
                AttributionWeight.eu_id == r.eu_id, AttributionWeight.role == "signer"
            )
        ).scalar_one_or_none()
        if r.score_run_id is not None:  # scored or gated (has a run)
            assert signer is not None
            run = s.execute(
                select(ScoreRun).where(ScoreRun.eu_id == r.eu_id).order_by(ScoreRun.id.desc()).limit(1)
            ).scalar_one()
            # Reproduces: the president is re-derived from the action date, no subtype row.
            assert run.reproducible_hash == rescore_eu(s, r.eu_id).reproducible_hash
    finally:
        s.close()


def test_executive_order_ingests_and_scores() -> None:
    from degreezeor.core.models import Action, AttributionWeight, ExecutiveOrder
    from degreezeor.pipeline import score_executive_order

    s = _fresh_session()
    try:
        # EO 14026 — "Increasing the Minimum Wage for Federal Contractors" (Biden, 2021).
        r = score_executive_order(s, "2021-09263")
        s.commit()
        action = s.get(Action, r.action_id)
        eo = s.get(ExecutiveOrder, r.action_id)
        assert action.type == "eo"
        assert eo.eo_number == "14026"
        # If it scored, the signing president carries high executive authority.
        if r.status == "scored":
            signer = s.query(AttributionWeight).filter_by(eu_id=r.eu_id, role="signer").one()
            assert float(signer.attribution) >= 0.5
    finally:
        s.close()
