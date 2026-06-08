"""The nightly cron self-validates its audit chain (production hardening).

``refresh_all`` is the cron entrypoint. After its idempotent ingest/score pass it must
confirm the append-only hash chain is still intact and surface the result, so a silent
out-of-band tampering of history can never go unnoticed in production. The heavy,
network-bound scorers are monkeypatched out; we only exercise the self-validation glue.
"""

from __future__ import annotations

import pytest

from degreezeor import pipeline
from degreezeor.core import audit


@pytest.fixture()
def _no_network_scorers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline, "ingest_defc_delivery", lambda s, *a, **k: [])
    monkeypatch.setattr(pipeline, "ingest_budget_execution", lambda s, *a, **k: [])
    monkeypatch.setattr(pipeline, "ingest_state_policies", lambda s, *a, **k: [])
    monkeypatch.setattr(pipeline, "score_court_survival", lambda s, spec: None)
    monkeypatch.setattr(pipeline, "score_target", lambda s, spec: None)
    monkeypatch.setattr(pipeline, "batch_score_laws", lambda s, *a, **k: [])
    monkeypatch.setattr(pipeline, "batch_score_executive_orders", lambda s, *a, **k: [])
    monkeypatch.setattr(pipeline, "batch_score_regulations", lambda s, *a, **k: [])
    monkeypatch.setattr(pipeline, "ingest_member_bills", lambda s, *a, **k: 0)
    monkeypatch.setattr(pipeline, "ingest_house_votes", lambda s, *a, **k: 0)
    monkeypatch.setattr(pipeline, "ingest_senate_votes", lambda s, *a, **k: 0)
    # enrich_official_names is imported lazily inside refresh_all; patch at its source.
    from degreezeor.ingestion import loader
    monkeypatch.setattr(loader, "enrich_official_names", lambda s, *a, **k: 0)


def test_refresh_reports_audit_chain_ok(session, _no_network_scorers) -> None:
    audit.append(session, event_type="TEST", payload={"x": 1})
    audit.append(session, event_type="TEST", payload={"x": 2})
    counts = pipeline.refresh_all(session)
    assert counts["audit_chain_ok"] == 1


def test_refresh_detects_broken_chain(session, _no_network_scorers) -> None:
    from degreezeor.core.models import AuditRecord
    audit.append(session, event_type="TEST", payload={"x": 1})
    audit.append(session, event_type="TEST", payload={"x": 2})
    session.flush()
    # Tamper with a historical record's payload — this must break the hash chain.
    rec = session.query(AuditRecord).order_by(AuditRecord.id.asc()).first()
    rec.payload_json = '{"x": 999}'
    session.flush()
    counts = pipeline.refresh_all(session)
    assert counts["audit_chain_ok"] == 0
