"""Platform-wide reproducibility self-audit (PLAN.md §9.9 / §16).

Proves the operational reproducibility guarantee: every published score can be
independently re-derived from its stored inputs + pinned methodology and yields the
SAME ``reproducible_hash`` bit-for-bit. A mismatch (non-determinism / tampering) is a
hard failure; the audit itself is read-only (re-runs happen in rolled-back savepoints).

Uses a curated-fact target EU (the court-survival pattern: ``realized_value`` is stored
directly), so the re-run is fully deterministic and offline — no network, no cache.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select

from degreezeor.core.models import (
    Action,
    DataSource,
    EvaluationUnit,
    ExecutiveOrder,
    Metric,
    Objective,
    Official,
    ScoreRun,
)
from degreezeor.core.numeric import D
from degreezeor.pipeline import _rescore_target_eu, verify_all_reproducible


def _build_scored_curated_eu(session, *, realized: str = "64") -> tuple[int, str]:
    """Create + score one curated-fact target EU; return (eu_id, reproducible_hash)."""
    src = session.execute(select(DataSource).where(DataSource.name == "Curated")).scalar_one_or_none()
    if src is None:
        src = DataSource(name="Curated", tier=2, base_url="https://example.gov")
        session.add(src)
        session.flush()
    signer = Official(full_name="Issuing President")
    session.add(signer)
    session.flush()
    n = session.execute(select(func.count()).select_from(Action)).scalar_one()
    action = Action(type="eo", title="Executive Order Under Review", action_date=date(2017, 3, 6),
                    source_id=src.id, source_url="https://federalregister.gov/eo",
                    native_identifier=f"EO-TEST-{n}")
    session.add(action)
    session.flush()
    session.add(ExecutiveOrder(action_id=action.id, eo_number=str(9000 + n), signing_official_id=signer.id))
    metric = session.execute(select(Metric).where(Metric.code == "legal_survival")).scalar_one_or_none()
    if metric is None:
        metric = Metric(code="legal_survival", name="Legal survival index", unit="index",
                        direction_good="up", source_id=src.id, native_series_id="CURATED:court_survival",
                        domain="Law")
        session.add(metric)
        session.flush()
    obj = Objective(action_id=action.id, source_id=src.id, source_url="https://courtlistener/case",
                    objective_level="executive", text="Survive judicial review.")
    session.add(obj)
    session.flush()
    eu = EvaluationUnit(action_id=action.id, objective_id=obj.id, metric_id=metric.id,
                        lag_window_months=0, sign_goal=1, status="pending", evaluation_mode="target",
                        target_value=D("100"), realized_value=D(realized), directly_attributable=True,
                        alignment=D("0.95"))
    session.add(eu)
    session.flush()
    out = _rescore_target_eu(session, eu, action, metric)
    assert out.status == "scored"
    return eu.id, out.reproducible_hash


def test_published_score_reproduces_bit_for_bit(session) -> None:
    eu_id, repro_hash = _build_scored_curated_eu(session)
    a = verify_all_reproducible(session)
    assert a.total == 1
    assert a.reproduced == 1
    assert a.mismatched == 0
    assert a.errored == 0
    assert a.all_reproduced is True
    c = a.checks[0]
    assert c.eu_id == eu_id
    assert c.status == "reproduced"
    assert c.stored_hash == c.recomputed_hash == repro_hash


def test_self_audit_is_read_only(session) -> None:
    """The audit must not mutate the DB — savepoints are rolled back, so no new runs."""
    _build_scored_curated_eu(session)
    before = session.execute(select(func.count()).select_from(ScoreRun)).scalar_one()
    verify_all_reproducible(session)
    after = session.execute(select(func.count()).select_from(ScoreRun)).scalar_one()
    assert before == after == 1


def test_tampered_hash_is_flagged_as_mismatch(session) -> None:
    """If a stored hash no longer matches its re-derivation (tampering / non-determinism),
    the audit flags it as a mismatch and fails."""
    eu_id, _ = _build_scored_curated_eu(session)
    run = session.execute(
        select(ScoreRun).where(ScoreRun.eu_id == eu_id).order_by(ScoreRun.id.desc()).limit(1)
    ).scalar_one()
    run.reproducible_hash = "dead" * 16  # 64 hex chars, but wrong
    session.flush()

    a = verify_all_reproducible(session)
    assert a.mismatched == 1
    assert a.reproduced == 0
    assert a.all_reproduced is False
    assert a.checks[0].status == "mismatch"
    assert a.checks[0].recomputed_hash != a.checks[0].stored_hash


def test_multiple_scores_all_audited(session) -> None:
    _build_scored_curated_eu(session, realized="64")
    _build_scored_curated_eu(session, realized="0")
    _build_scored_curated_eu(session, realized="100")
    a = verify_all_reproducible(session)
    assert a.total == 3
    assert a.reproduced == 3
    assert a.all_reproduced is True
