"""Build public scorecards from stored quantities only (PLAN.md §14).

Every field traces to a row in the database that itself carries a source URL +
content hash. The "why" narrative and "what would change the score" hints are
generated mechanically from those quantities — no editorializing, no labels.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
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


def _latest_run_map(session: Session, eu_ids: set[int]) -> dict[int, ScoreRun]:
    """Bulk: latest ScoreRun per evaluation unit in ~1 query (avoids per-EU N+1)."""
    if not eu_ids:
        return {}
    subq = (
        select(ScoreRun.eu_id, func.max(ScoreRun.id).label("mx"))
        .where(ScoreRun.eu_id.in_(eu_ids))
        .group_by(ScoreRun.eu_id)
        .subquery()
    )
    runs = session.execute(
        select(ScoreRun).join(subq, ScoreRun.id == subq.c.mx)
    ).scalars().all()
    return {r.eu_id: r for r in runs}


def _scores_by_run(session: Session, run_ids: list[int]) -> dict[int, EUScore]:
    """Bulk: EUScore keyed by score_run_id in 1 query."""
    if not run_ids:
        return {}
    rows = session.execute(select(EUScore).where(EUScore.score_run_id.in_(run_ids))).scalars().all()
    return {s.score_run_id: s for s in rows}


def _narrative(action: Action, metric: Metric | None, outcome: OutcomeResult | None,
               eu: EvaluationUnit, score: EUScore | None) -> str:
    if metric is None or outcome is None:
        return (
            f"This action ({action.title!r}) could not be scored on outcomes "
            f"because: {eu.non_scoreable_reason or 'no operational metric / outcome'}. "
            "Absence of evidence is reported as such — not as a low score."
        )
    gated = score.gated if score else True
    if eu.evaluation_mode == "target":
        # Promise-keeping framing (the baseline value here is the committed TARGET).
        target = float(outcome.baseline_pooled)
        observed = float(outcome.observed)
        pct = (observed / target * 100) if target else 0.0
        attrib = ("directly attributable to the action (its own funds/output)"
                  if eu.directly_attributable else "NOT directly attributable (economy-wide)")
        base = (
            f"This is a TARGET-RELATIVE (promise-keeping) score: did the policy deliver its own "
            f"committed number? Its target for '{metric.name}' was {target:,.0f} {metric.unit}; "
            f"official data shows {observed:,.0f} {metric.unit} delivered (~{pct:.0f}%). "
            f"The realized series is {attrib}."
        )
        if gated:
            base += (" Confidence is below the publish threshold (the realized series isn't "
                     "directly attributable), so no composite is issued — insufficient evidence.")
        else:
            base += (f" Because the realized series is the action's own output, confidence cleared "
                     f"the gate; composite {_num(score.composite) if score else None}/100.")
        return base
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
    # Audit-complete: include every source URL the run recorded as a numeric input
    # (e.g. donor-state series for synthetic control / DiD).
    if run is not None and run.input_source_urls:
        with contextlib.suppress(ValueError, TypeError):
            relevant_urls.update(json.loads(run.input_source_urls))
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
            "evaluation_mode": eu.evaluation_mode,
            "target_value": _num(eu.target_value),
            "directly_attributable": eu.directly_attributable,
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


def _official_contributions(session: Session, official_id: int):
    """Gather this official's (non-residual) attributable EUs + their latest score."""
    from degreezeor.scoring.rollup import ActionContribution

    rows = session.execute(
        select(AttributionWeight).where(
            AttributionWeight.official_id == official_id,
            AttributionWeight.is_residual.is_(False),
        )
    ).scalars().all()
    eu_ids = {aw.eu_id for aw in rows}
    # Bulk-load EUs, actions, latest runs + scores (avoids per-action N+1).
    eu_map = {e.id: e for e in session.execute(
        select(EvaluationUnit).where(EvaluationUnit.id.in_(eu_ids))).scalars()} if eu_ids else {}
    action_ids = {e.action_id for e in eu_map.values()}
    action_map = {a.id: a for a in session.execute(
        select(Action).where(Action.id.in_(action_ids))).scalars()} if action_ids else {}
    run_map = _latest_run_map(session, eu_ids)
    score_map = _scores_by_run(session, [r.id for r in run_map.values()])
    contributions = []
    details = []
    seen: set[int] = set()
    for aw in rows:
        if aw.eu_id in seen:
            continue
        seen.add(aw.eu_id)
        eu = eu_map.get(aw.eu_id)
        action = action_map.get(eu.action_id) if eu else None
        run = run_map.get(aw.eu_id)
        score = score_map.get(run.id) if run else None
        composite = score.composite if score else None
        gated = bool(score.gated) if score else True
        contributions.append(ActionContribution(
            eu_id=aw.eu_id, attribution=aw.attribution,
            composite=composite, confidence=score.confidence if score else None, gated=gated,
        ))
        details.append({
            "eu_id": aw.eu_id,
            "action_title": action.title if action else None,
            "action_type": action.type if action else None,
            "role": aw.role,
            "attribution": _num(aw.attribution),
            "status": eu.status if eu else None,
            "composite": _num(composite),
            "confidence": _num(score.confidence) if score else None,
        })
    return contributions, details


def build_official(session: Session, official_id: int) -> dict[str, Any] | None:
    from degreezeor.scoring.rollup import rollup

    official = session.get(Official, official_id)
    if official is None:
        return None
    contributions, details = _official_contributions(session, official_id)
    r = rollup(contributions)
    return {
        "official": {"id": official.id, "name": official.full_name, "bioguide_id": official.bioguide_id},
        "rollup": {
            "total_actions": r.total_actions,
            "scored_actions": r.scored_actions,
            "coverage": _num(r.coverage),
            "composite": _num(r.composite),  # None => insufficient evidence
            "confidence": _num(r.confidence),
            "note": (
                "Attribution-weighted mean composite over this official's SCORED actions, "
                "shown only with coverage. None => no action cleared the confidence gate "
                "(insufficient evidence), never a low score."
            ),
        },
        "actions": details,
    }


def list_officials(
    session: Session, q: str | None = None, scored_only: bool = False
) -> list[dict[str, Any]]:
    """Attribution-weighted roll-up for every official. Bulk-loaded (a handful of queries
    total) so it scales to the full House+Senate roster without per-official N+1 latency."""
    from degreezeor.scoring.rollup import ActionContribution, rollup

    # 1 query: all non-residual attribution edges.
    aw_rows = session.execute(
        select(AttributionWeight).where(
            AttributionWeight.official_id.is_not(None),
            AttributionWeight.is_residual.is_(False),
        )
    ).scalars().all()
    eu_ids = {aw.eu_id for aw in aw_rows}
    run_map = _latest_run_map(session, eu_ids)                       # ~1 query
    score_map = _scores_by_run(session, [r.id for r in run_map.values()])  # 1 query

    by_off: dict[int, list[AttributionWeight]] = defaultdict(list)
    for aw in aw_rows:
        by_off[aw.official_id].append(aw)
    # 1 query: official names.
    name_map = {
        o.id: o.full_name
        for o in session.execute(select(Official).where(Official.id.in_(by_off.keys()))).scalars()
    }

    out = []
    for oid, aws in by_off.items():
        contributions = []
        seen: set[int] = set()
        for aw in aws:
            if aw.eu_id in seen:
                continue
            seen.add(aw.eu_id)
            run = run_map.get(aw.eu_id)
            score = score_map.get(run.id) if run else None
            contributions.append(ActionContribution(
                eu_id=aw.eu_id, attribution=aw.attribution,
                composite=score.composite if score else None,
                confidence=score.confidence if score else None,
                gated=bool(score.gated) if score else True,
            ))
        r = rollup(contributions)
        out.append({
            "id": oid,
            "name": name_map.get(oid),
            "total_actions": r.total_actions,
            "scored_actions": r.scored_actions,
            "coverage": _num(r.coverage),
            "composite": _num(r.composite),
            "confidence": _num(r.confidence),
        })
    if q:
        ql = q.lower()
        out = [o for o in out if ql in (o["name"] or "").lower()]
    if scored_only:
        out = [o for o in out if o["scored_actions"] > 0]
    out.sort(key=lambda x: (-(x["scored_actions"]), x["name"] or ""))
    return out


def build_coverage(session: Session) -> dict[str, Any]:
    """Platform-wide coverage (PLAN.md §16 transparency / anti-cherry-picking).

    Shows the WHOLE denominator: how many actions were considered, and what fraction are
    scoreable vs. honestly 'insufficient evidence' / non-scoreable — so the scored subset
    can never be mistaken for a complete or cherry-picked record."""
    from sqlalchemy import func

    rows = session.execute(
        select(EvaluationUnit.status, func.count()).group_by(EvaluationUnit.status)
    ).all()
    by_status = {s: n for s, n in rows}
    total = sum(by_status.values())
    scored = by_status.get("scored", 0)
    insufficient = by_status.get("insufficient_evidence", 0)
    non_scoreable = total - scored - insufficient

    # By action type (join EU -> action).
    type_rows = session.execute(
        select(Action.type, EvaluationUnit.status, func.count())
        .join(EvaluationUnit, EvaluationUnit.action_id == Action.id)
        .group_by(Action.type, EvaluationUnit.status)
    ).all()
    by_type: dict[str, dict[str, int]] = {}
    for atype, status, n in type_rows:
        by_type.setdefault(atype, {})[status] = n

    return {
        "total_evaluation_units": total,
        "scored": scored,
        "insufficient_evidence": insufficient,
        "non_scoreable": non_scoreable,
        "scored_share": round(scored / total, 4) if total else 0.0,
        "by_status": by_status,
        "by_action_type": by_type,
        "note": (
            "Complete visibility: every action considered is shown, including those we "
            "could not score. 'Insufficient evidence' is honest abstention, never a low score; "
            "the scored subset is NOT a complete or representative record of any official."
        ),
    }


def list_units(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        select(EvaluationUnit, Action).join(Action, Action.id == EvaluationUnit.action_id)
    ).all()
    eu_ids = {eu.id for eu, _ in rows}
    run_map = _latest_run_map(session, eu_ids)
    score_map = _scores_by_run(session, [r.id for r in run_map.values()])
    # Bulk-load public-law numbers (1 query) instead of two session.get per row.
    law_ids = {action.id for _, action in rows}
    law_map = {law.action_id: law.public_law_number
               for law in session.execute(select(Law).where(Law.action_id.in_(law_ids))).scalars()} if law_ids else {}
    out = []
    for eu, action in rows:
        run = run_map.get(eu.id)
        score = score_map.get(run.id) if run else None
        out.append({
            "id": eu.id,
            "title": action.title,
            "public_law": law_map.get(action.id),
            "status": eu.status,
            "composite": _num(score.composite) if score else None,
            "confidence": _num(score.confidence) if score else None,
        })
    return out
