"""Build public scorecards from stored quantities only (PLAN.md §14).

Every field traces to a row in the database that itself carries a source URL +
content hash. The "why" narrative and "what would change the score" hints are
generated mechanically from those quantities — no editorializing, no labels.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from degreezeor.config import settings
from degreezeor.core.models import (
    Action,
    AttributionWeight,
    Baseline,
    ConfidenceInterval,
    EUScore,
    EvaluationUnit,
    Law,
    MethodologyVersion,
    Metric,
    Objective,
    Official,
    OutcomeResult,
    RawLanding,
    ScoreComponent,
    ScoreRun,
)


def _num(x: Any) -> float | None:
    return float(x) if x is not None else None


def _latest_run(session: Session, eu_id: int) -> ScoreRun | None:
    return session.execute(
        select(ScoreRun).where(ScoreRun.eu_id == eu_id).order_by(ScoreRun.id.desc()).limit(1)
    ).scalar_one_or_none()


def _narrative(action: Action, metric: Metric | None, outcome: OutcomeResult | None,
               eu: EvaluationUnit, score: EUScore | None) -> str:
    if metric is None or outcome is None:
        return (
            f"This action ({action.title!r}) could not be scored on outcomes "
            f"because: {eu.non_scoreable_reason or 'no operational metric / outcome'}. "
            "Absence of evidence is reported as such — not as a low score."
        )
    direction = "toward" if float(outcome.delta) * (eu.sign_goal or 1) >= 0 else "away from"
    gated = score.gated if score else True
    goal_phrase = "reduce" if (eu.sign_goal or 1) < 0 else "increase"
    base = (
        f"This law's stated objective maps to '{metric.name}' (goal: {goal_phrase} it). "
        f"At {outcome and 'the evaluation point'}, the metric was {_num(outcome.observed)} {metric.unit}; "
        f"the counterfactual baseline (what would likely have happened anyway) was "
        f"{_num(outcome.baseline_pooled)} {metric.unit}. The baseline-adjusted change was "
        f"{_num(outcome.delta)} {metric.unit} — i.e. movement {direction} the stated goal "
        f"(standardized effect z={_num(outcome.z)})."
    )
    if gated:
        base += (
            " However, confidence is below the publish threshold, so NO composite verdict is "
            "issued (insufficient evidence). Most likely causes: a single-series baseline cannot "
            "separate the policy from concurrent macro shocks, and/or baseline methods disagree."
        )
    else:
        base += (
            f" Confidence cleared the publish threshold, so a composite of "
            f"{_num(score.composite) if score else None}/100 is shown (confidence-scaled, factual "
            "components only; value-laden lenses are off by default)."
        )
    return base


def _what_would_change(outcome: OutcomeResult | None, score: EUScore | None) -> list[str]:
    hints: list[str] = []
    if outcome is None:
        hints.append("Linking an official outcome metric to the stated objective would enable scoring.")
        return hints
    if float(outcome.model_dependence) > 0.3:
        hints.append(
            "A stronger baseline (difference-in-differences or synthetic control) that reduces "
            "model dependence would raise confidence."
        )
    if score and score.gated:
        hints.append(
            "A macro-shock-adjusted baseline and a longer outcome window would likely move this "
            "out of 'insufficient evidence'."
        )
    hints.append("More precise attribution (e.g. decisive-vote pivotality data) would narrow the attribution band.")
    return hints


def build_scorecard(session: Session, eu_id: int) -> dict[str, Any] | None:
    eu = session.get(EvaluationUnit, eu_id)
    if eu is None:
        return None
    action = session.get(Action, eu.action_id)
    metric = session.get(Metric, eu.metric_id) if eu.metric_id else None
    objective = session.get(Objective, eu.objective_id) if eu.objective_id else None
    law = session.get(Law, action.id)
    outcome = session.execute(
        select(OutcomeResult).where(OutcomeResult.eu_id == eu_id)
    ).scalar_one_or_none()
    baselines = session.execute(select(Baseline).where(Baseline.eu_id == eu_id)).scalars().all()
    attributions = session.execute(
        select(AttributionWeight).where(AttributionWeight.eu_id == eu_id)
    ).scalars().all()
    run = _latest_run(session, eu_id)
    components = (
        session.execute(select(ScoreComponent).where(ScoreComponent.score_run_id == run.id)).scalars().all()
        if run else []
    )
    score = (
        session.execute(select(EUScore).where(EUScore.score_run_id == run.id)).scalar_one_or_none()
        if run else None
    )
    cis = (
        session.execute(select(ConfidenceInterval).where(ConfidenceInterval.score_run_id == run.id)).scalars().all()
        if run else []
    )
    mv = session.get(MethodologyVersion, run.methodology_version_id) if run else None

    def official_name(oid: int | None) -> str | None:
        if oid is None:
            return None
        o = session.get(Official, oid)
        return o.full_name if o else None

    # Source trail filtered to THIS evaluation unit: the action record, the objective
    # source, and the outcome series — each an immutable landing with url + content hash.
    relevant_urls = {action.source_url}
    if objective is not None:
        relevant_urls.add(objective.source_url)
    relevant_series = {metric.native_series_id} if metric is not None else set()
    enacted_year = law.enacted_date.year if law and law.enacted_date else None

    def _series_window_covers(url: str) -> bool:
        # Keep only the outcome-series snapshot whose year window brackets the
        # evaluation period, so one EU's trail never shows another EU's window.
        if enacted_year is None:
            return True
        m = re.search(r"startyear=(\d{4}).*endyear=(\d{4})", url)
        if not m:
            return True
        return int(m.group(1)) <= enacted_year <= int(m.group(2))

    landings = []
    for land in session.execute(select(RawLanding).order_by(RawLanding.id.asc())).scalars().all():
        if land.source_url in relevant_urls or (
            land.native_identifier in relevant_series and _series_window_covers(land.source_url)
        ):
            landings.append(land)

    return {
        "evaluation_unit": {
            "id": eu.id,
            "status": eu.status,
            "non_scoreable_reason": eu.non_scoreable_reason,
            "lag_window_months": eu.lag_window_months,
            "sign_goal": eu.sign_goal,
            "prereg_hash": eu.prereg_hash,
            "prereg_at": eu.prereg_at.isoformat() if eu.prereg_at else None,
        },
        "action": {
            "id": action.id,
            "type": action.type,
            "title": action.title,
            "domain": action.domain,
            "public_law_number": law.public_law_number if law else None,
            "enacted_date": law.enacted_date.isoformat() if law and law.enacted_date else None,
            "source_url": action.source_url,
            "content_hash": action.content_hash,
        },
        "objective": None if objective is None else {
            "text": objective.text,
            "level": objective.objective_level,
            "source_url": objective.source_url,
        },
        "metric": None if metric is None else {
            "code": metric.code, "name": metric.name, "unit": metric.unit,
            "direction_good": metric.direction_good, "native_series_id": metric.native_series_id,
        },
        "outcome": None if outcome is None else {
            "observed": _num(outcome.observed),
            "baseline_pooled": _num(outcome.baseline_pooled),
            "delta": _num(outcome.delta),
            "z": _num(outcome.z),
            "model_dependence": _num(outcome.model_dependence),
            "ci_low": _num(outcome.ci_low),
            "ci_high": _num(outcome.ci_high),
        },
        "baselines": [
            {"method": b.method, "baseline_value": _num(b.baseline_value),
             "ci_low": _num(b.ci_low), "ci_high": _num(b.ci_high), "spec": b.spec_json}
            for b in baselines
        ],
        "attribution": [
            {"official_id": a.official_id, "official_name": official_name(a.official_id),
             "role": a.role, "authority": _num(a.authority), "pivotality": _num(a.pivotality),
             "attribution": _num(a.attribution), "ci_low": _num(a.attr_ci_low),
             "ci_high": _num(a.attr_ci_high), "is_residual": a.is_residual}
            for a in attributions
        ],
        "components": [
            {"name": c.component, "value": _num(c.value), "ci_low": _num(c.ci_low),
             "ci_high": _num(c.ci_high), "is_value_laden": c.is_value_laden}
            for c in components
        ],
        "score": None if score is None else {
            "confidence": _num(score.confidence),
            "composite": _num(score.composite),
            "gated": score.gated,
            "coverage": _num(score.coverage),
            "publish_threshold": float(settings.confidence_publish_threshold),
        },
        "confidence_intervals": [
            {"quantity": ci.quantity, "ci_low": _num(ci.ci_low), "ci_high": _num(ci.ci_high), "method": ci.method}
            for ci in cis
        ],
        "run": None if run is None else {
            "id": run.id, "data_snapshot_id": run.data_snapshot_id,
            "code_git_sha": run.code_git_sha, "seed": run.seed,
            "reproducible_hash": run.reproducible_hash,
            "methodology_version": mv.semver if mv else None,
        },
        "source_trail": [
            {"source_url": land.source_url, "native_identifier": land.native_identifier,
             "content_hash": land.content_hash, "retrieved_at": land.retrieved_at.isoformat(),
             "byte_size": land.byte_size}
            for land in landings
        ],
        "narrative": _narrative(action, metric, outcome, eu, score),
        "what_would_change_the_score": _what_would_change(outcome, score),
    }


def list_units(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        select(EvaluationUnit, Action).join(Action, Action.id == EvaluationUnit.action_id)
    ).all()
    out = []
    for eu, action in rows:
        run = _latest_run(session, eu.id)
        score = (
            session.execute(select(EUScore).where(EUScore.score_run_id == run.id)).scalar_one_or_none()
            if run else None
        )
        out.append({
            "id": eu.id,
            "title": action.title,
            "public_law": (session.get(Law, action.id).public_law_number if session.get(Law, action.id) else None),
            "status": eu.status,
            "composite": _num(score.composite) if score else None,
            "confidence": _num(score.confidence) if score else None,
        })
    return out
