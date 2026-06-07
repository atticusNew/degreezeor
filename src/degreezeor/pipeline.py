"""End-to-end scoring pipeline for one enacted law (PLAN.md §12).

Order is significant for neutrality:
  ingest law + objective  ->  select metric (party-masked, objective-only)
  ->  PRE-REGISTER (hash to audit)  ->  ingest outcome series  ->  compute outcome
  ->  attribution  ->  confidence + gate  ->  pinned, reproducible score run.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from degreezeor.config import settings
from degreezeor.core import audit
from degreezeor.core.hashing import canonical_json, hash_payload
from degreezeor.core.interfaces import AttributionContext
from degreezeor.core.models import (
    AttributionWeight,
    Baseline,
    Bill,
    ConfidenceInterval,
    EUScore,
    EvaluationUnit,
    Law,
    MethodologyVersion,
    Objective,
    Observation,
    OutcomeResult,
    ScoreComponent,
    ScoreRun,
)
from degreezeor.core.numeric import D, q_score
from degreezeor.ingestion.loader import ensure_bls_source, load_law, load_observations
from degreezeor.provenance import current_git_sha, data_snapshot_id
from degreezeor.scoring.attribution import build_attribution
from degreezeor.scoring.baseline import split_series  # noqa: F401  (ensures registration import)
from degreezeor.scoring.confidence import compute_confidence
from degreezeor.scoring.objective import (
    ensure_metric,
    mask_party_and_name,
    select_metrics,
    sign_goal_for,
)
from degreezeor.scoring.outcome import compute_outcome, s_outcome_from_z
from degreezeor.scoring.prereg import preregister
from degreezeor.scoring.score import assemble_score


@dataclass
class ScoreOutcome:
    action_id: int
    eu_id: int
    status: str
    score_run_id: int | None
    reproducible_hash: str | None


def _ensure_methodology(session: Session) -> MethodologyVersion:
    mv = session.execute(
        select(MethodologyVersion).where(MethodologyVersion.semver == settings.methodology_version)
    ).scalar_one_or_none()
    if mv is None:
        mv = MethodologyVersion(
            semver=settings.methodology_version,
            git_sha=current_git_sha(),
            description="MVP slice: pretrend/flat baseline ensemble; sponsor+signer attribution.",
        )
        session.add(mv)
        session.flush()
    return mv


def _objective_for_matching(session: Session, action_id: int) -> Objective | None:
    objs = session.execute(
        select(Objective).where(Objective.action_id == action_id)
    ).scalars().all()
    # Prefer the richer agency/CRS summary; fall back to statutory short title.
    by_level = {o.objective_level: o for o in objs}
    return by_level.get("agency") or by_level.get("statutory") or (objs[0] if objs else None)


def _durability(observations: list[tuple[str, object]], eval_period: str, baseline_pooled: D, sign_goal: int, delta_toward_goal: D) -> object | None:
    later = [(p, D(v)) for p, v in observations if p > eval_period]
    if not later:
        return None
    good = D(1) if delta_toward_goal >= 0 else D(-1)
    same = 0
    for _p, v in later:
        side = D(sign_goal) * (v - baseline_pooled)
        if (side >= 0) == (good >= 0):
            same += 1
    frac = D(same) / D(len(later))
    return q_score(frac * D(100))


def score_law(session: Session, congress: int, law_number: int, law_type: str = "pub") -> ScoreOutcome:
    ensure_bls_source(session)
    action = load_law(session, congress, law_number, law_type)

    obj = _objective_for_matching(session, action.id)
    if obj is None:
        eu = EvaluationUnit(action_id=action.id, status="non_scoreable_no_objective",
                            non_scoreable_reason="No stated objective found.")
        session.add(eu)
        session.flush()
        return ScoreOutcome(action.id, eu.id, eu.status, None, None)

    primary, _side = select_metrics(obj.text, action.domain)
    if primary is None:
        eu = EvaluationUnit(action_id=action.id, objective_id=obj.id, status="non_scoreable_no_metric",
                            non_scoreable_reason="No official metric operationalizes the stated objective.")
        session.add(eu)
        session.flush()
        return ScoreOutcome(action.id, eu.id, eu.status, None, None)

    metric = ensure_metric(session, primary.spec)
    sign_goal = sign_goal_for(primary.spec)
    lag = primary.spec.default_lag_months

    eu = EvaluationUnit(
        action_id=action.id, objective_id=obj.id, metric_id=metric.id,
        lag_window_months=lag, sign_goal=sign_goal, status="pending",
    )
    session.add(eu)
    session.flush()

    # --- PRE-REGISTER before any outcome data is consulted ---
    preregister(
        session, eu,
        action_native_id=action.native_identifier,
        metric_code=primary.spec.code,
        objective_level=obj.objective_level,
        sign_goal=sign_goal,
        lag_window_months=lag,
        masked_objective=mask_party_and_name(obj.text)[:280],
    )

    # --- Now ingest outcome series ---
    enacted = action.action_date
    start_year = enacted.year - 3
    end_year = enacted.year + (lag // 12) + 2
    load_observations(session, metric, start_year, end_year)

    rows = session.execute(
        select(Observation.period, Observation.value).where(Observation.metric_id == metric.id)
        .order_by(Observation.period)
    ).all()
    observations = [(p, v) for p, v in rows]
    event_period = f"{enacted.year}-{enacted.month:02d}-01"

    comp = compute_outcome(
        observations, event_period=event_period, lag_window_months=lag,
        sign_goal=sign_goal, seed=settings.deterministic_seed,
    )
    if comp is None:
        eu.status = "insufficient_evidence"
        eu.non_scoreable_reason = "Insufficient outcome observations around the evaluation window."
        session.flush()
        return ScoreOutcome(action.id, eu.id, eu.status, None, None)

    # --- Attribution ---
    bill = session.get(Bill, action.id)
    law = session.get(Law, action.id)
    actx = AttributionContext(
        eu_id=eu.id,
        action_type=action.type,
        sponsor_official_id=bill.sponsor_official_id if bill else None,
        signer_official_id=law.signed_by_official_id if law else None,
        vote_margin=None,
        member_on_winning_side=None,
    )
    attributions = build_attribution(actx)
    residual = next((a.attribution for a in attributions if a.is_residual), D(0))
    human_widths = [D(a.attr_ci_high) - D(a.attr_ci_low) for a in attributions if not a.is_residual]

    # --- Confidence ---
    best_method = "pretrend_projection" if any(
        e.method == "pretrend_projection" for e in comp.per_method
    ) else comp.per_method[0].method
    conf = compute_confidence(
        best_method=best_method,
        ci_low=comp.ci_low, ci_high=comp.ci_high,
        model_dependence=comp.model_dependence,
        data_tier=1, data_completeness=D("1.0"),
        attribution_widths=human_widths,
    )

    # --- Components ---
    delta_toward_goal = D(sign_goal) * D(comp.delta)
    durability = _durability(observations, comp.eval_period, D(comp.baseline_pooled), sign_goal, delta_toward_goal)
    s_outcome = s_outcome_from_z(comp.z)
    s_evidence = q_score(D(conf.c_design) * D(100))
    s_attribution = q_score((D(1) - D(residual)) * D(100))
    s_alignment = q_score(D(primary.alignment) * D(100))
    s_dataquality = q_score(D(conf.c_data) * D(100))

    assembled = assemble_score(
        s_outcome=s_outcome, s_evidence=s_evidence, s_attribution=s_attribution,
        s_alignment=s_alignment, s_dataquality=s_dataquality, s_durability=durability,
        confidence=conf.confidence,
    )

    # --- Persist outputs ---
    session.add(OutcomeResult(
        eu_id=eu.id, observed=q_score(comp.observed), baseline_pooled=q_score(comp.baseline_pooled),
        delta=q_score(comp.delta), z=q_score(comp.z), model_dependence=q_score(comp.model_dependence),
        ci_low=q_score(comp.ci_low), ci_high=q_score(comp.ci_high),
    ))
    for e in comp.per_method:
        session.add(Baseline(
            eu_id=eu.id, method=e.method, spec_json=canonical_json(e.spec),
            baseline_value=q_score(e.baseline_value),
            ci_low=q_score(e.ci_low) if e.ci_low is not None else None,
            ci_high=q_score(e.ci_high) if e.ci_high is not None else None,
        ))
    for a in attributions:
        session.add(AttributionWeight(
            eu_id=eu.id, official_id=a.official_id, role=a.role,
            authority=q_score(a.authority), pivotality=q_score(a.pivotality),
            attribution=q_score(a.attribution), attr_ci_low=q_score(a.attr_ci_low),
            attr_ci_high=q_score(a.attr_ci_high), is_residual=a.is_residual,
        ))

    mv = _ensure_methodology(session)
    obs_hashes = session.execute(
        select(Observation.content_hash).where(Observation.metric_id == metric.id)
    ).scalars().all()
    snapshot = data_snapshot_id([action.content_hash or ""] + list(obs_hashes))

    run = ScoreRun(
        eu_id=eu.id, methodology_version_id=mv.id, data_snapshot_id=snapshot,
        code_git_sha=current_git_sha(), seed=settings.deterministic_seed,
    )
    session.add(run)
    session.flush()

    for c in assembled.components:
        session.add(ScoreComponent(
            score_run_id=run.id, component=c.name, value=q_score(c.value),
            ci_low=q_score(c.ci_low) if c.ci_low is not None else None,
            ci_high=q_score(c.ci_high) if c.ci_high is not None else None,
            is_value_laden=c.is_value_laden,
        ))
    session.add(ConfidenceInterval(
        score_run_id=run.id, quantity="outcome_delta",
        ci_low=q_score(comp.ci_low), ci_high=q_score(comp.ci_high), method="bootstrap_2000",
    ))
    session.add(EUScore(
        score_run_id=run.id, confidence=q_score(assembled.confidence),
        composite=q_score(assembled.composite) if assembled.composite is not None else None,
        gated=assembled.gated, coverage=D("1.0"),
    ))

    # Reproducible hash: stable canonical digest of all run outputs + pins.
    repro_payload = {
        "data_snapshot_id": snapshot,
        "methodology_version": settings.methodology_version,
        "seed": settings.deterministic_seed,
        "confidence": str(q_score(assembled.confidence)),
        "composite": str(q_score(assembled.composite)) if assembled.composite is not None else None,
        "gated": assembled.gated,
        "components": {c.name: str(q_score(c.value)) for c in assembled.components},
        "outcome": {
            "observed": str(q_score(comp.observed)),
            "baseline_pooled": str(q_score(comp.baseline_pooled)),
            "delta": str(q_score(comp.delta)),
            "z": str(q_score(comp.z)),
        },
        "attribution": [
            {"official_id": a.official_id, "role": a.role, "attribution": str(q_score(a.attribution))}
            for a in attributions
        ],
    }
    run.reproducible_hash = hash_payload(repro_payload)

    eu.status = "insufficient_evidence" if assembled.gated else "scored"
    if assembled.gated:
        eu.non_scoreable_reason = "Confidence below publish threshold; outcome not distinguishable enough."
    session.flush()

    audit.append(session, event_type="SCORE", payload={
        "eu_id": eu.id, "score_run_id": run.id, "reproducible_hash": run.reproducible_hash,
        "gated": assembled.gated,
    })
    return ScoreOutcome(action.id, eu.id, eu.status, run.id, run.reproducible_hash)
