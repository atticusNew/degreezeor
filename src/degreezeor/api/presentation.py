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

from degreezeor.categories import category_for, category_label, category_sort_key
from degreezeor.config import settings
from degreezeor.core.models import (
    Action,
    AttributionWeight,
    Baseline,
    Bill,
    ConfidenceInterval,
    DataSource,
    EUScore,
    EvaluationUnit,
    ExecutiveOrder,
    Jurisdiction,
    Law,
    MethodologyVersion,
    Metric,
    Objective,
    OfficeTerm,
    Official,
    OutcomeResult,
    Party,
    RawLanding,
    ScoreComponent,
    ScoreRun,
    Vote,
    VotePosition,
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

    # Descriptive peer-group context (symmetric, non-ranking): how this scored result
    # sits next to the typical scored result for its action type and its category.
    cat_key = category_for(action.domain, action.type, metric.domain if metric else None)
    descriptive_context: list[str] = []
    if score is not None and not score.gated and score.composite is not None:
        ref = _scored_reference(session)
        comp = float(score.composite)
        type_labels = {"law": "laws", "eo": "executive orders", "regulation": "regulations",
                       "budget": "budget execution"}
        ctx_type = _describe_relative(comp, ref["by_type"].get(action.type),
                                      type_labels.get(action.type, action.type + "s"))
        ctx_cat = _describe_relative(comp, ref["by_category"].get(cat_key),
                                     f"the {category_label(cat_key).lower()} category")
        descriptive_context = [c for c in (ctx_type, ctx_cat) if c]

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
            "category": category_for(action.domain, action.type, metric.domain if metric else None),
            "category_label": category_label(
                category_for(action.domain, action.type, metric.domain if metric else None)),
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
        "descriptive_context": descriptive_context,
        "what_would_change_the_score": _what_would_change(outcome, score),
    }


def _positions_for(session: Session, official_ids: set[int]) -> dict[int, str | None]:
    """Derive each official's office from source-linked facts only (never from party).

    President: in the presidential reference, or signer of a federal law / executive order.
    Governor: signer of a state-jurisdiction law.
    Senator / Representative: chamber of the roll-calls they were recorded in.
    Returns None when the office cannot be determined objectively (shown as just the name)."""
    if not official_ids:
        return {}
    from degreezeor.core.reference import PRESIDENTS

    pres_bio = {b for _, b, _, _, _ in PRESIDENTS}
    bio = dict(session.execute(
        select(Official.id, Official.bioguide_id).where(Official.id.in_(official_ids))
    ).all())
    eo_signers = set(session.execute(
        select(ExecutiveOrder.signing_official_id)
        .where(ExecutiveOrder.signing_official_id.in_(official_ids))
    ).scalars())
    fed_law_signers: set[int] = set()
    state_law_signers: set[int] = set()
    for oid, jtype in session.execute(
        select(Law.signed_by_official_id, Jurisdiction.type)
        .join(Action, Action.id == Law.action_id)
        .join(Jurisdiction, Jurisdiction.id == Action.jurisdiction_id, isouter=True)
        .where(Law.signed_by_official_id.in_(official_ids))
    ).all():
        (state_law_signers if jtype == "state" else fed_law_signers).add(oid)
    chambers: dict[int, set[str]] = defaultdict(set)
    for oid, ch in session.execute(
        select(VotePosition.official_id, Vote.chamber)
        .join(Vote, Vote.id == VotePosition.vote_id)
        .where(VotePosition.official_id.in_(official_ids))
    ).all():
        chambers[oid].add((ch or "").lower())
    # Chamber also inferable from the bills a member sponsored (HR* = House, S* = Senate).
    for oid, bn in session.execute(
        select(Bill.sponsor_official_id, Bill.bill_number)
        .where(Bill.sponsor_official_id.in_(official_ids), Bill.bill_number.is_not(None))
    ).all():
        chambers[oid].add("house" if (bn or "").upper().startswith("H") else "senate")

    out: dict[int, str | None] = {}
    for oid in official_ids:
        if bio.get(oid) in pres_bio or oid in eo_signers or oid in fed_law_signers:
            out[oid] = "President"
        elif oid in state_law_signers:
            out[oid] = "Governor"
        elif "senate" in chambers.get(oid, set()):
            out[oid] = "Senator"
        elif "house" in chambers.get(oid, set()):
            out[oid] = "Representative"
        else:
            out[oid] = None
    return out


def official_activity(session: Session, official_id: int) -> dict[str, Any]:
    """The record of what an official ACTED ON: bills they sponsored, grouped by topic
    category (unscored). Distinct from the scored composite; this is breadth, not effect."""
    rows = session.execute(
        select(Action.title, Action.domain, Action.action_date, Action.source_url, Bill.bill_number)
        .join(Bill, Bill.action_id == Action.id)
        .where(Bill.sponsor_official_id == official_id, Action.type == "bill")
    ).all()
    by_cat: dict[str, int] = defaultdict(int)
    items = []
    for title, domain, adate, url, bn in rows:
        cat = category_for(domain, "bill")
        by_cat[cat] += 1
        items.append({
            "title": title, "date": adate.isoformat() if adate else None,
            "category": cat, "category_label": category_label(cat),
            "bill_number": bn, "source_url": url,
        })
    items.sort(key=lambda x: x["date"] or "", reverse=True)
    cats = sorted(
        ({"category": k, "category_label": category_label(k), "count": v} for k, v in by_cat.items()),
        key=lambda c: (-c["count"], category_sort_key(c["category"])),
    )
    return {"sponsored_total": len(rows), "by_category": cats, "recent": items[:10]}


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
    law_date_map = {law.action_id: law.enacted_date for law in session.execute(
        select(Law).where(Law.action_id.in_(action_ids))).scalars()} if action_ids else {}
    metric_ids = {e.metric_id for e in eu_map.values() if e.metric_id}
    metric_domain = {m.id: m.domain for m in session.execute(
        select(Metric).where(Metric.id.in_(metric_ids))).scalars()} if metric_ids else {}
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
        cat = category_for(
            action.domain if action else None,
            action.type if action else None,
            metric_domain.get(eu.metric_id) if eu else None,
        )
        adate = (law_date_map.get(action.id) if action else None) or (action.action_date if action else None)
        details.append({
            "eu_id": aw.eu_id,
            "action_title": action.title if action else None,
            "action_type": action.type if action else None,
            "category": cat,
            "category_label": category_label(cat),
            "date": adate.isoformat() if adate else None,
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

    # Per-category breakdown: group the official's contributions by objective category
    # and roll each group up the same way (composite + coverage). Descriptive only.
    eu_cat = {d["eu_id"]: d["category"] for d in details}
    by_cat: dict[str, list] = defaultdict(list)
    for c in contributions:
        by_cat[eu_cat.get(c.eu_id, "other")].append(c)
    categories = []
    for key, items in by_cat.items():
        cr = rollup(items)
        categories.append({
            "category": key,
            "category_label": category_label(key),
            "total_actions": cr.total_actions,
            "scored_actions": cr.scored_actions,
            "coverage": _num(cr.coverage),
            "composite": _num(cr.composite),
            "confidence": _num(cr.confidence),
        })
    categories.sort(key=lambda x: category_sort_key(x["category"]))

    # Activity summary (when / how often they act) from dated actions. Empirical, neutral.
    years = sorted(int(d["date"][:4]) for d in details if d.get("date"))
    activity = {
        "count": len(details),
        "dated_count": len(years),
        "first_year": years[0] if years else None,
        "last_year": years[-1] if years else None,
    }
    # Most-active category by number of attributable actions (descriptive only).
    most_active = max(categories, key=lambda c: c["total_actions"], default=None)
    most_active_category = most_active["category_label"] if most_active else None
    party = session.execute(
        select(Party.abbrev).join(OfficeTerm, OfficeTerm.party_id == Party.id)
        .where(OfficeTerm.official_id == official_id).order_by(OfficeTerm.id.desc()).limit(1)
    ).scalar_one_or_none()
    position = _positions_for(session, {official_id}).get(official_id)
    return {
        "official": {"id": official.id, "name": official.full_name,
                     "bioguide_id": official.bioguide_id, "party": party,
                     "position": position},
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
        "by_category": categories,
        "activity": activity,
        "record": official_activity(session, official_id),
        "most_active_category": most_active_category,
        "actions": details,
    }


def list_officials(
    session: Session, q: str | None = None, scored_only: bool = False,
    min_involvement: float = 0.0, party: str | None = None, action_type: str | None = None,
    category: str | None = None,
) -> list[dict[str, Any]]:
    """Attribution-weighted roll-up for every official. Bulk-loaded (a handful of queries
    total) so it scales to the full House+Senate roster without per-official N+1 latency.

    ``involvement`` = the official's LARGEST single-action attribution share — so a bill's
    sponsor (meaningful share) is distinguishable from a backbench voter whose only tie is a
    lopsided roll-call (~0.05%). ``min_involvement`` hides negligible ties by default; the UI
    exposes a "show all" toggle. ``party`` filters by caucus abbrev (audit metadata only)."""
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
    # 1 query: action type + objective category per EU (for the type / category filters).
    eu_type: dict[int, str] = {}
    eu_cat: dict[int, str] = {}
    if eu_ids:
        for eid, atype, adomain, mdomain in session.execute(
            select(EvaluationUnit.id, Action.type, Action.domain, Metric.domain)
            .join(Action, Action.id == EvaluationUnit.action_id)
            .join(Metric, Metric.id == EvaluationUnit.metric_id, isouter=True)
            .where(EvaluationUnit.id.in_(eu_ids))
        ).all():
            eu_type[eid] = atype
            eu_cat[eid] = category_for(adomain, atype, mdomain)

    by_off: dict[int, list[AttributionWeight]] = defaultdict(list)
    for aw in aw_rows:
        by_off[aw.official_id].append(aw)
    # 1 query: official names.
    name_map = {
        o.id: o.full_name
        for o in session.execute(select(Official).where(Official.id.in_(by_off.keys()))).scalars()
    }
    # 1 query: party abbrev per official (latest office term; audit metadata only).
    party_map: dict[int, str] = {}
    for off_id, abbrev in session.execute(
        select(OfficeTerm.official_id, Party.abbrev)
        .join(Party, OfficeTerm.party_id == Party.id)
        .where(OfficeTerm.official_id.in_(by_off.keys()))
        .order_by(OfficeTerm.id)
    ).all():
        party_map[off_id] = abbrev  # last (most recent) wins
    # Office per official, derived from source-linked facts (never from party).
    position_map = _positions_for(session, set(by_off.keys()))

    out = []
    for oid, aws in by_off.items():
        contributions = []
        seen: set[int] = set()
        involvement = 0.0
        atypes: set[str] = set()
        cats: set[str] = set()
        for aw in aws:
            involvement = max(involvement, float(aw.attribution))
            if eu_type.get(aw.eu_id):
                atypes.add(eu_type[aw.eu_id])
            if eu_cat.get(aw.eu_id):
                cats.add(eu_cat[aw.eu_id])
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
            "party": party_map.get(oid),
            "position": position_map.get(oid),
            "involvement": round(involvement, 4),
            "total_actions": r.total_actions,
            "scored_actions": r.scored_actions,
            "coverage": _num(r.coverage),
            "composite": _num(r.composite),
            "confidence": _num(r.confidence),
            "categories": sorted(cats, key=category_sort_key),
            "_action_types": sorted(atypes),
        })
    if q:
        ql = q.lower()
        out = [o for o in out if ql in (o["name"] or "").lower()]
    if party:
        out = [o for o in out if (o["party"] or "").lower() == party.lower()]
    if action_type:
        out = [o for o in out if action_type in o["_action_types"]]
    if category:
        out = [o for o in out if category in o["categories"]]
    for o in out:
        o.pop("_action_types", None)  # internal filter field; not part of the public shape
    if scored_only:
        out = [o for o in out if o["scored_actions"] > 0]
    if min_involvement > 0:
        out = [o for o in out if o["involvement"] >= min_involvement]
    # Scored first, then by causal involvement (sponsors above incidental voters), then name.
    out.sort(key=lambda x: (-(x["scored_actions"]), -x["involvement"], x["name"] or ""))
    return out


def officials_index(session: Session) -> list[dict[str, Any]]:
    """Lightweight directory of every attributed official for client-side typeahead /
    A-to-Z browse. Minimal fields (no composite computation) so it loads fast and the
    whole roster can be filtered in the browser. Sorted most-active first."""
    aw_rows = session.execute(
        select(AttributionWeight).where(
            AttributionWeight.official_id.is_not(None),
            AttributionWeight.is_residual.is_(False),
        )
    ).scalars().all()
    eu_ids = {aw.eu_id for aw in aw_rows}
    run_map = _latest_run_map(session, eu_ids)
    score_map = _scores_by_run(session, [r.id for r in run_map.values()])
    eu_cat: dict[int, str] = {}
    if eu_ids:
        for eid, atype, adomain, mdomain in session.execute(
            select(EvaluationUnit.id, Action.type, Action.domain, Metric.domain)
            .join(Action, Action.id == EvaluationUnit.action_id)
            .join(Metric, Metric.id == EvaluationUnit.metric_id, isouter=True)
            .where(EvaluationUnit.id.in_(eu_ids))
        ).all():
            eu_cat[eid] = category_for(adomain, atype, mdomain)
    by_off: dict[int, list[AttributionWeight]] = defaultdict(list)
    for aw in aw_rows:
        by_off[aw.official_id].append(aw)

    # Sponsored-bill record per official (the activity layer), so members who only
    # sponsored bills still appear in the directory and carry their topic categories.
    sponsored: dict[int, int] = defaultdict(int)
    bill_cats: dict[int, set[str]] = defaultdict(set)
    for oid, domain in session.execute(
        select(Bill.sponsor_official_id, Action.domain)
        .join(Action, Action.id == Bill.action_id)
        .where(Bill.sponsor_official_id.is_not(None), Action.type == "bill")
    ).all():
        sponsored[oid] += 1
        bill_cats[oid].add(category_for(domain, "bill"))

    all_ids = set(by_off.keys()) | set(sponsored.keys())
    name_map = {o.id: o.full_name for o in session.execute(
        select(Official).where(Official.id.in_(all_ids))).scalars()}
    position_map = _positions_for(session, all_ids)

    out = []
    for oid in all_ids:
        seen: set[int] = set()
        scored = 0
        involvement = 0.0
        cats: set[str] = set(bill_cats.get(oid, set()))
        for aw in by_off.get(oid, []):
            involvement = max(involvement, float(aw.attribution))
            if eu_cat.get(aw.eu_id):
                cats.add(eu_cat[aw.eu_id])
            if aw.eu_id in seen:
                continue
            seen.add(aw.eu_id)
            run = run_map.get(aw.eu_id)
            score = score_map.get(run.id) if run else None
            if score is not None and not score.gated and score.composite is not None:
                scored += 1
        out.append({
            "id": oid,
            "name": name_map.get(oid),
            "position": position_map.get(oid),
            "total_actions": len(seen),
            "scored_actions": scored,
            "sponsored": sponsored.get(oid, 0),
            "involvement": round(involvement, 4),
            "categories": sorted(cats, key=category_sort_key),
        })
    # Most active first: scored, then bills sponsored, then attributable actions, then name.
    out.sort(key=lambda x: (-x["scored_actions"], -x["sponsored"], -x["total_actions"], x["name"] or ""))
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

    # By objective category (derived in Python from action/metric domain + action type).
    cat_rows = session.execute(
        select(EvaluationUnit.status, Action.type, Action.domain, Metric.domain)
        .join(Action, Action.id == EvaluationUnit.action_id)
        .join(Metric, Metric.id == EvaluationUnit.metric_id, isouter=True)
    ).all()
    by_category: dict[str, dict[str, int]] = {}
    for status, atype, adomain, mdomain in cat_rows:
        cat = category_for(adomain, atype, mdomain)
        bucket = by_category.setdefault(cat, {})
        bucket[status] = bucket.get(status, 0) + 1

    return {
        "total_evaluation_units": total,
        "scored": scored,
        "insufficient_evidence": insufficient,
        "non_scoreable": non_scoreable,
        "scored_share": round(scored / total, 4) if total else 0.0,
        "by_status": by_status,
        "by_action_type": by_type,
        "by_category": by_category,
        "note": (
            "Complete visibility: every action considered is shown, including those we "
            "could not score. 'Insufficient evidence' is honest abstention, never a low score; "
            "the scored subset is NOT a complete or representative record of any official."
        ),
    }


def build_stats(session: Session) -> dict[str, Any]:
    """Headline credibility stats for the landing page: real counts + data freshness."""
    cov = build_coverage(session)
    officials = session.execute(
        select(func.count(func.distinct(AttributionWeight.official_id))).where(
            AttributionWeight.official_id.is_not(None), AttributionWeight.is_residual.is_(False)
        )
    ).scalar() or 0
    sources = session.execute(select(func.count()).select_from(DataSource)).scalar() or 0
    last = session.execute(select(func.max(RawLanding.retrieved_at))).scalar()
    return {
        "actions_considered": cov["total_evaluation_units"],
        "scored": cov["scored"],
        "insufficient_evidence": cov["insufficient_evidence"],
        "non_scoreable": cov["non_scoreable"],
        "officials": officials,
        "sources": sources,
        "last_updated": last.isoformat() if last else None,
        "methodology_version": settings.methodology_version,
    }


def build_sources(session: Session) -> list[dict[str, Any]]:
    """Every data source, with its provenance tier, so users can browse what feeds scores."""
    rows = session.execute(select(DataSource).order_by(DataSource.tier, DataSource.name)).scalars().all()
    tier_label = {0: "Tier 0 (action record)", 1: "Tier 1 (official statistics)",
                  2: "Tier 2 (official analysis)", 3: "Tier 3 (verified mirror)"}
    return [{"name": d.name, "tier": d.tier, "tier_label": tier_label.get(d.tier, f"Tier {d.tier}"),
             "base_url": d.base_url, "license": d.license} for d in rows]


def _mean_std(values: list[float]) -> tuple[float, float]:
    n = len(values)
    if n == 0:
        return 0.0, 0.0
    mean = sum(values) / n
    if n < 2:
        return mean, 0.0
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return mean, var ** 0.5


# Minimum scored sample before a descriptive "typical result" comparison is shown.
_CONTEXT_MIN_SAMPLE = 3


def _scored_reference(session: Session) -> dict[str, dict[str, dict[str, Any]]]:
    """Aggregate scored composites grouped by action type and by category, so an
    individual action can be described relative to the typical scored result for its
    peer group. Descriptive only: this never changes any score and is symmetric."""
    units = list_units(session)
    by_type: dict[str, list[float]] = defaultdict(list)
    by_cat: dict[str, list[float]] = defaultdict(list)
    for u in units:
        if u["status"] == "scored" and u["composite"] is not None:
            by_type[u["type"]].append(u["composite"])
            by_cat[u["category"]].append(u["composite"])

    def summarize(groups: dict[str, list[float]]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for key, vals in groups.items():
            mean, std = _mean_std(vals)
            out[key] = {"n": len(vals), "mean": round(mean, 2), "std": round(std, 2)}
        return out

    return {"by_type": summarize(by_type), "by_category": summarize(by_cat)}


def _describe_relative(composite: float, ref: dict[str, Any] | None, group_label: str) -> str | None:
    """Neutral, descriptive sentence placing a composite next to its peer group's mean.

    Uses a half-standard-deviation band so small differences read as "near typical".
    Returns None when the sample is too small to be meaningful (honest abstention)."""
    if not ref or ref["n"] < _CONTEXT_MIN_SAMPLE:
        return None
    mean = ref["mean"]
    band = max(ref["std"] * 0.5, 1.0)
    if composite >= mean + band:
        rel = "above"
    elif composite <= mean - band:
        rel = "below"
    else:
        rel = "near"
    return (
        f"This result ({composite:.1f}) is {rel} the typical scored result for {group_label} "
        f"({mean:.1f} across {ref['n']} scored). This is descriptive context, not a ranking."
    )


def build_categories(session: Session) -> dict[str, Any]:
    """Public category catalog with objective counts per category, for grouping/filtering."""
    from degreezeor.categories import category_catalog

    units = list_units(session)
    counts: dict[str, dict[str, Any]] = {}
    for u in units:
        c = counts.setdefault(u["category"], {"total": 0, "scored": 0, "composites": []})
        c["total"] += 1
        if u["status"] == "scored" and u["composite"] is not None:
            c["scored"] += 1
            c["composites"].append(u["composite"])
    out = []
    for entry in category_catalog():
        c = counts.get(entry["key"], {"total": 0, "scored": 0, "composites": []})
        mean, _ = _mean_std(c["composites"])
        out.append({
            **entry,
            "total_actions": c["total"],
            "scored_actions": c["scored"],
            "mean_composite": round(mean, 2) if c["composites"] else None,
        })
    return {
        "categories": out,
        "note": (
            "Categories are derived deterministically from each action's official subject "
            "domain and the metric it was measured against. They group actions by topic; "
            "they are not a value judgment and play no part in scoring."
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
    # Bulk-load metric domains (1 query) for objective category derivation.
    metric_ids = {eu.metric_id for eu, _ in rows if eu.metric_id}
    metric_domain = {m.id: m.domain
                     for m in session.execute(select(Metric).where(Metric.id.in_(metric_ids))).scalars()} if metric_ids else {}
    out = []
    for eu, action in rows:
        run = run_map.get(eu.id)
        score = score_map.get(run.id) if run else None
        cat = category_for(action.domain, action.type, metric_domain.get(eu.metric_id))
        out.append({
            "id": eu.id,
            "title": action.title,
            "type": action.type,
            "public_law": law_map.get(action.id),
            "status": eu.status,
            "category": cat,
            "category_label": category_label(cat),
            "composite": _num(score.composite) if score else None,
            "confidence": _num(score.confidence) if score else None,
        })
    return out
