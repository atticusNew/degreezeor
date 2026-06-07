"""Pre-registration / methodology lock (PLAN.md §9, keystone bias control).

The metric, baseline candidates, lag window, and sign_goal are derived from the
objective text and committed (hashed) to the audit chain BEFORE any outcome data
is consulted. This makes outcome-driven metric/baseline cherry-picking detectable:
the prereg hash is fixed and timestamped ahead of the outcome computation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from degreezeor.core import audit
from degreezeor.core.hashing import hash_payload
from degreezeor.core.interfaces import BASELINE_METHODS
from degreezeor.core.models import EvaluationUnit


def preregister(
    session: Session,
    eu: EvaluationUnit,
    *,
    action_native_id: str | None,
    metric_code: str,
    objective_level: str,
    sign_goal: int,
    lag_window_months: int,
    masked_objective: str,
) -> str:
    spec: dict[str, Any] = {
        "action": action_native_id,
        "objective_level": objective_level,
        "metric_code": metric_code,
        "sign_goal": sign_goal,
        "lag_window_months": lag_window_months,
        "baseline_candidates": sorted(m.name for m in BASELINE_METHODS.all()),
        "masked_objective": masked_objective,
    }
    prereg_hash = hash_payload(spec)
    eu.prereg_hash = prereg_hash
    eu.prereg_at = datetime.now(UTC)
    audit.append(session, event_type="PREREG", payload={"eu_id": eu.id, "hash": prereg_hash, "spec": spec})
    session.flush()
    return prereg_hash
