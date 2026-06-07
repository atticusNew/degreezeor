"""Dispute / appeal workflow (PLAN.md §9.8, §16).

Anyone can challenge a score. Resolution is NOT an editorial judgment — it triggers
an independent, deterministic RE-RUN of the evaluation unit and publishes whether the
score changed, with a machine-generated public diff. Every step is appended to the
append-only audit chain (no anonymous edits; full version history).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from degreezeor.core import audit
from degreezeor.core.models import Dispute, EUScore, EvaluationUnit, ScoreComponent, ScoreRun
from degreezeor.pipeline import rescore_eu


def file_dispute(session: Session, *, eu_id: int, filer: str, claim: str) -> Dispute:
    """Record a challenge against an evaluation unit (immutably, via the audit chain)."""
    if session.get(EvaluationUnit, eu_id) is None:
        raise ValueError(f"EU {eu_id} not found")
    dispute = Dispute(eu_id=eu_id, filer=filer, claim=claim, status="open")
    session.add(dispute)
    session.flush()
    audit.append(
        session, event_type="DISPUTE_FILED", actor="user",
        payload={"dispute_id": dispute.id, "eu_id": eu_id, "filer": filer, "claim": claim},
    )
    return dispute


def _run_snapshot(session: Session, run: ScoreRun) -> dict[str, Any]:
    score = session.execute(
        select(EUScore).where(EUScore.score_run_id == run.id)
    ).scalar_one_or_none()
    comps = {
        c.component: str(c.value)
        for c in session.execute(
            select(ScoreComponent).where(ScoreComponent.score_run_id == run.id)
        ).scalars()
    }
    return {
        "reproducible_hash": run.reproducible_hash,
        "composite": str(score.composite) if score and score.composite is not None else None,
        "confidence": str(score.confidence) if score else None,
        "gated": bool(score.gated) if score else None,
        "components": comps,
    }


def _diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for key in ("composite", "confidence", "gated", "reproducible_hash"):
        if before.get(key) != after.get(key):
            changes[key] = {"before": before.get(key), "after": after.get(key)}
    comp_changes = {}
    for name in set(before.get("components", {})) | set(after.get("components", {})):
        b = before.get("components", {}).get(name)
        a = after.get("components", {}).get(name)
        if b != a:
            comp_changes[name] = {"before": b, "after": a}
    if comp_changes:
        changes["components"] = comp_changes
    return changes


@dataclass(frozen=True)
class DisputeResolution:
    dispute_id: int
    status: str
    reproduced: bool
    public_diff: dict[str, Any]


def resolve_dispute(session: Session, *, dispute_id: int, reviewer: str = "system") -> DisputeResolution:
    """Resolve a dispute via an independent reproducible re-run + public diff."""
    dispute = session.get(Dispute, dispute_id)
    if dispute is None:
        raise ValueError(f"dispute {dispute_id} not found")

    prior_run = session.execute(
        select(ScoreRun).where(ScoreRun.eu_id == dispute.eu_id).order_by(ScoreRun.id.desc()).limit(1)
    ).scalar_one_or_none()
    before = _run_snapshot(session, prior_run) if prior_run else {}

    result = rescore_eu(session, dispute.eu_id)
    new_run = session.get(ScoreRun, result.score_run_id)
    after = _run_snapshot(session, new_run)

    reproduced = bool(prior_run) and before.get("reproducible_hash") == after.get("reproducible_hash")
    diff = _diff(before, after)
    if reproduced:
        status = "resolved_upheld"
        public_diff = {"summary": "Score reproduced exactly on independent re-run; original stands.",
                       "changes": {}}
    else:
        status = "resolved_corrected"
        public_diff = {"summary": "Re-run differed from the original; the score has been re-derived.",
                       "changes": diff}

    dispute.status = status
    dispute.resolution_run_id = new_run.id
    import json

    dispute.public_diff = json.dumps(public_diff)
    session.flush()
    audit.append(
        session, event_type="DISPUTE_RESOLVED", actor="reviewer",
        payload={"dispute_id": dispute.id, "eu_id": dispute.eu_id, "reviewer": reviewer,
                 "status": status, "reproduced": reproduced,
                 "old_hash": before.get("reproducible_hash"), "new_hash": after.get("reproducible_hash")},
    )
    return DisputeResolution(dispute_id=dispute.id, status=status, reproduced=reproduced, public_diff=public_diff)
