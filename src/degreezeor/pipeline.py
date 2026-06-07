"""End-to-end scoring pipeline for one enacted law (PLAN.md §12).

Order is significant for neutrality:
  ingest law + objective  ->  select metric (party-masked, objective-only)
  ->  PRE-REGISTER (hash to audit)  ->  ingest outcome series  ->  compute outcome
  ->  attribution  ->  confidence + gate  ->  pinned, reproducible score run.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.orm import Session

from degreezeor.config import settings
from degreezeor.core import audit
from degreezeor.core.hashing import canonical_json, hash_payload
from degreezeor.core.interfaces import AttributionContext
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
    Observation,
    Official,
    OutcomeResult,
    ScoreComponent,
    ScoreRun,
)
from degreezeor.core.numeric import D, q_score
from degreezeor.ingestion.adapters.bls import bls_adapter
from degreezeor.ingestion.adapters.generic import generic_url_adapter
from degreezeor.ingestion.landing import land
from degreezeor.ingestion.loader import (
    ensure_bls_source,
    load_executive_order,
    load_law,
    load_observations,
)
from degreezeor.provenance import current_git_sha, data_snapshot_id
from degreezeor.scoring.attribution import build_attribution
from degreezeor.scoring.baseline import split_series  # noqa: F401  (ensures registration import)
from degreezeor.scoring.confidence import best_design, compute_confidence
from degreezeor.scoring.objective import (
    ensure_metric,
    mask_party_and_name,
    select_metrics,
    sign_goal_for,
)
from degreezeor.scoring.outcome import compute_outcome, s_outcome_from_z
from degreezeor.scoring.prereg import preregister
from degreezeor.scoring.score import assemble_score


def state_employment_series_id(fips: str) -> str:
    """BLS state total-nonfarm-employment series (SA): SMS + FIPS(2) + 15-char suffix."""
    return f"SMS{fips}000000000000001"


@dataclass
class StatePolicySpec:
    key: str  # e.g. "KS-HB2117"
    title: str
    state_fips: str
    state_name: str
    donor_fips: list[str]
    source_url: str  # official state legislature URL (Tier-0 provenance)
    objective_text: str
    enacted_year: int
    enacted_month: int
    lag_window_months: int = 24
    signer_name: str | None = None
    sponsor_name: str | None = None


# Documented demo state policies (public record). The objective text states the
# policy's OWN goal verbatim-in-spirit; the system then measures against it neutrally.
STATE_POLICIES: dict[str, StatePolicySpec] = {
    "KS-HB2117": StatePolicySpec(
        key="KS-HB2117",
        title="Kansas 2012 income tax cuts (HB 2117)",
        state_fips="20",
        state_name="Kansas",
        # Regional comparison pool of states that did not enact comparable 2012 cuts.
        donor_fips=["31", "29", "40", "19", "05", "08", "27", "46"],  # NE MO OK IA AR CO MN SD
        source_url="http://www.kslegislature.org/li_2012/b2011_12/measures/hb2117/",
        objective_text=(
            "Reduce individual income tax rates and exempt certain business income in order to "
            "grow the Kansas economy and create jobs and employment."
        ),
        enacted_year=2012,
        enacted_month=5,
        # 48-month horizon: a structural income-tax cut's job-creation claim is
        # appropriately evaluated over a multi-year window, declared at pre-registration.
        lag_window_months=48,
        signer_name="Sam Brownback",
    ),
}


@dataclass
class ScoreOutcome:
    action_id: int
    eu_id: int
    status: str
    score_run_id: int | None
    reproducible_hash: str | None


def _obs_window(enacted: date, lag_months: int) -> tuple[str, str]:
    """Inclusive ISO bounds for an EU's outcome series, so EUs that share a metric
    (e.g. two laws both scored on nonfarm employment) never pollute each other's
    observation set — which keeps every score run deterministic and reproducible."""
    start = f"{enacted.year - 3}-01-01"
    end = f"{enacted.year + (lag_months // 12) + 2}-12-31"
    return start, end


def _windowed_observations(session: Session, metric_id: int, enacted: date, lag_months: int):
    start, end = _obs_window(enacted, lag_months)
    rows = session.execute(
        select(Observation.period, Observation.value).where(
            Observation.metric_id == metric_id,
            Observation.period >= start,
            Observation.period <= end,
        ).order_by(Observation.period)
    ).all()
    return [(p, v) for p, v in rows]


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
    """Sustained achievement of the stated goal: fraction of post-evaluation periods
    on the GOAL-WARD side of the baseline. Goal-directional, so a persistent move
    *away* from the objective correctly scores LOW durability (not high)."""
    later = [(p, D(v)) for p, v in observations if p > eval_period]
    if not later:
        return None
    toward = sum(1 for _p, v in later if D(sign_goal) * (v - baseline_pooled) > 0)
    frac = D(toward) / D(len(later))
    return q_score(frac * D(100))


def _finalize(
    session: Session,
    eu: EvaluationUnit,
    action: Action,
    comp,
    attributions,
    *,
    alignment: object,
    observations: list[tuple[str, object]],
    metric: Metric,
    sign_goal: int,
    extra_source_urls: list[str] | None = None,
) -> ScoreOutcome:
    """Shared scoring tail: confidence → components → assemble → pinned reproducible run.

    Used by every scoring pipeline (federal laws, state policies, …) so the formula,
    gate, persistence, and reproducibility hash are identical across action types.
    """
    eu.alignment = q_score(D(alignment))  # persist for faithful re-runs (disputes)

    # OutcomeResult / Baseline / AttributionWeight describe the EU's CURRENT result and
    # are keyed by eu_id; a re-run (e.g. dispute resolution) replaces them. Run-level
    # history is preserved via versioned ScoreRun + components + reproducible_hash.
    for model in (OutcomeResult, Baseline, AttributionWeight):
        session.execute(sa_delete(model).where(model.eu_id == eu.id))
    session.flush()

    residual = next((a.attribution for a in attributions if a.is_residual), D(0))
    human_widths = [D(a.attr_ci_high) - D(a.attr_ci_low) for a in attributions if not a.is_residual]

    best_method = best_design([e.method for e in comp.per_method])
    conf = compute_confidence(
        best_method=best_method,
        ci_low=comp.ci_low, ci_high=comp.ci_high,
        model_dependence=comp.model_dependence,
        data_tier=1, data_completeness=D("1.0"),
        attribution_widths=human_widths,
    )

    delta_toward_goal = D(sign_goal) * D(comp.delta)
    durability = _durability(observations, comp.eval_period, D(comp.baseline_pooled), sign_goal, delta_toward_goal)
    s_outcome = s_outcome_from_z(comp.z)
    s_evidence = q_score(D(conf.c_design) * D(100))
    s_attribution = q_score((D(1) - D(residual)) * D(100))
    s_alignment = q_score(D(alignment) * D(100))
    s_dataquality = q_score(D(conf.c_data) * D(100))

    assembled = assemble_score(
        s_outcome=s_outcome, s_evidence=s_evidence, s_attribution=s_attribution,
        s_alignment=s_alignment, s_dataquality=s_dataquality, s_durability=durability,
        confidence=conf.confidence,
    )

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
    # Snapshot identity = the exact numeric inputs that determine the score (treated +
    # donor series, fingerprinted by compute_outcome) plus the metric spec. Independent
    # of volatile provenance bytes (e.g. dynamic HTML), so re-runs are bit-reproducible.
    snapshot = data_snapshot_id(
        [comp.input_hash, metric.native_series_id, str(sign_goal), str(eu.lag_window_months)]
    )

    input_urls = sorted({action.source_url, *(extra_source_urls or [])})
    run = ScoreRun(
        eu_id=eu.id, methodology_version_id=mv.id, data_snapshot_id=snapshot,
        code_git_sha=current_git_sha(), seed=settings.deterministic_seed,
        input_source_urls=canonical_json(input_urls),
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

    observations = _windowed_observations(session, metric.id, enacted, lag)
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
    return _finalize(
        session, eu, action, comp, attributions,
        alignment=D(primary.alignment), observations=observations, metric=metric, sign_goal=sign_goal,
    )


def score_executive_order(session: Session, document_number: str) -> ScoreOutcome:
    """Ingest + score one executive order (Federal Register) end-to-end.

    Same neutral machinery as laws; attribution gives the signing president high
    executive authority (EOs are unilateral). Most EOs will be non-scoreable or
    insufficient-evidence (narrow / diffuse objectives) — reported honestly.
    """
    ensure_bls_source(session)
    action = load_executive_order(session, document_number)

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
    eu = EvaluationUnit(action_id=action.id, objective_id=obj.id, metric_id=metric.id,
                        lag_window_months=lag, sign_goal=sign_goal, status="pending")
    session.add(eu)
    session.flush()

    preregister(
        session, eu, action_native_id=action.native_identifier, metric_code=primary.spec.code,
        objective_level=obj.objective_level, sign_goal=sign_goal, lag_window_months=lag,
        masked_objective=mask_party_and_name(obj.text)[:280],
    )

    enacted = action.action_date
    load_observations(session, metric, enacted.year - 3, enacted.year + (lag // 12) + 2)
    observations = _windowed_observations(session, metric.id, enacted, lag)
    event_period = f"{enacted.year}-{enacted.month:02d}-01"

    comp = compute_outcome(observations, event_period=event_period, lag_window_months=lag,
                           sign_goal=sign_goal, seed=settings.deterministic_seed)
    if comp is None:
        eu.status = "insufficient_evidence"
        eu.non_scoreable_reason = "Insufficient outcome observations around the evaluation window."
        session.flush()
        return ScoreOutcome(action.id, eu.id, eu.status, None, None)

    eo = session.get(ExecutiveOrder, action.id)
    actx = AttributionContext(
        eu_id=eu.id, action_type=action.type,
        sponsor_official_id=None,
        signer_official_id=eo.signing_official_id if eo else None,
        vote_margin=None, member_on_winning_side=None,
    )
    attributions = build_attribution(actx)
    return _finalize(
        session, eu, action, comp, attributions,
        alignment=D(primary.alignment), observations=observations, metric=metric, sign_goal=sign_goal,
    )


def _eu_donor_observations(action: Action) -> tuple[dict[str, list[tuple[str, object]]], list[str]]:
    """Reconstruct a state policy's donor series (cache-first, no new network)."""
    donor_observations: dict[str, list[tuple[str, object]]] = {}
    donor_source_urls: list[str] = []
    spec = STATE_POLICIES.get(action.native_identifier or "")
    if spec is None or not spec.donor_fips:
        return donor_observations, donor_source_urls
    prev = os.environ.get("DZ_HTTP_CACHE")
    os.environ["DZ_HTTP_CACHE"] = "1"
    try:
        sy = spec.enacted_year - 3
        ey = spec.enacted_year + (spec.lag_window_months // 12) + 2
        for dfips in spec.donor_fips:
            dfetch = bls_adapter.fetch(state_employment_series_id(dfips), start_year=sy, end_year=ey)
            donor_source_urls.append(dfetch.source_url)
            dseries = json.loads(dfetch.content)["Results"]["series"][0]
            donor_observations[dfips] = [
                (f"{pt['year']}-{int(pt['period'][1:]):02d}-01", pt["value"])
                for pt in dseries["data"] if pt["period"].startswith("M")
            ]
    finally:
        if prev is None:
            os.environ.pop("DZ_HTTP_CACHE", None)
        else:
            os.environ["DZ_HTTP_CACHE"] = prev
    return donor_observations, donor_source_urls


def eu_sensitivity(session: Session, eu_id: int):
    """Lag-window sensitivity analysis for an EU (PLAN.md §9.10), from stored data."""
    from degreezeor.scoring.sensitivity import DEFAULT_LAGS, analyze_lag_sensitivity

    eu = session.get(EvaluationUnit, eu_id)
    if eu is None or eu.metric_id is None or eu.sign_goal is None:
        return None
    action = session.get(Action, eu.action_id)
    enacted = action.action_date
    # Window wide enough to cover the longest probed horizon (uses whatever is stored/cached).
    observations = _windowed_observations(session, eu.metric_id, enacted, max(DEFAULT_LAGS))
    if len(observations) < 7:
        return None
    donors, _ = _eu_donor_observations(action)
    event_period = f"{enacted.year}-{enacted.month:02d}-01"
    return analyze_lag_sensitivity(
        observations, event_period=event_period, registered_lag=eu.lag_window_months,
        sign_goal=eu.sign_goal, seed=settings.deterministic_seed,
        donor_observations=donors or None,
    )


def rescore_eu(session: Session, eu_id: int) -> ScoreOutcome:
    """Deterministically RE-RUN an existing evaluation unit from stored inputs.

    Reads the EU's persisted objective/metric/observations/alignment and rebuilds the
    attribution context from the action — refetching outcome series ONLY from the URL
    replay cache (no new network calls). Donor series for state comparison designs are
    reconstructed (cache-first) from the policy spec. Produces a fresh, pinned ScoreRun;
    a faithful re-run yields the SAME reproducible_hash. This is the engine behind the
    dispute/appeal process: anyone can trigger an independent, reproducible re-run.
    """
    eu = session.get(EvaluationUnit, eu_id)
    if eu is None or eu.metric_id is None or eu.objective_id is None:
        raise ValueError(f"EU {eu_id} is not in a re-scoreable state")
    action = session.get(Action, eu.action_id)
    metric = session.get(Metric, eu.metric_id)

    enacted = action.action_date
    lag = eu.lag_window_months
    observations = _windowed_observations(session, metric.id, enacted, lag)
    event_period = f"{enacted.year}-{enacted.month:02d}-01"

    # Reconstruct donor series (cache-first) for state comparison-design policies.
    donor_observations, donor_source_urls = _eu_donor_observations(action)

    comp = compute_outcome(
        observations, event_period=event_period, lag_window_months=lag,
        sign_goal=eu.sign_goal, seed=settings.deterministic_seed,
        donor_observations=donor_observations or None,
    )
    if comp is None:
        raise ValueError(f"EU {eu_id} has insufficient stored observations to re-run")

    bill = session.get(Bill, action.id)
    law = session.get(Law, action.id)
    eo = session.get(ExecutiveOrder, action.id)
    signer = (law.signed_by_official_id if law else None) or (eo.signing_official_id if eo else None)
    actx = AttributionContext(
        eu_id=eu.id, action_type=action.type,
        sponsor_official_id=bill.sponsor_official_id if bill else None,
        signer_official_id=signer, vote_margin=None, member_on_winning_side=None,
    )
    attributions = build_attribution(actx)
    return _finalize(
        session, eu, action, comp, attributions,
        alignment=eu.alignment, observations=observations, metric=metric, sign_goal=eu.sign_goal,
        extra_source_urls=donor_source_urls,
    )


def _ensure_named_official(session: Session, name: str) -> Official:
    o = session.execute(select(Official).where(Official.full_name == name)).scalar_one_or_none()
    if o is None:
        o = Official(full_name=name)
        session.add(o)
        session.flush()
    return o


def _ensure_state_jurisdiction(session: Session, fips: str, name: str) -> Jurisdiction:
    j = session.execute(
        select(Jurisdiction).where(Jurisdiction.type == "state", Jurisdiction.fips == fips)
    ).scalar_one_or_none()
    if j is None:
        j = Jurisdiction(type="state", name=name, fips=fips)
        session.add(j)
        session.flush()
    return j


def score_state_policy(session: Session, spec: StatePolicySpec) -> ScoreOutcome:
    """Score a state policy via comparison-design baselines (synthetic control / DiD)
    on real BLS state employment data, with treated-vs-donor structure.

    This is the path that can legitimately clear the confidence gate and produce a
    composite, because a donor pool addresses the confounding a single series cannot.
    """
    ensure_bls_source(session)
    sign_goal = 1  # "create jobs" => higher employment is toward the stated goal

    # --- Tier-0 action provenance: fetch the official state source URL ---
    fetch = generic_url_adapter.fetch(spec.source_url, label=spec.key)
    land(session, fetch)
    jur = _ensure_state_jurisdiction(session, spec.state_fips, spec.state_name)

    existing = session.execute(
        select(Action).where(Action.native_identifier == spec.key, Action.type == "law")
    ).scalar_one_or_none()
    if existing is None:
        action = Action(
            type="law", title=spec.title, action_date=date(spec.enacted_year, spec.enacted_month, 1),
            jurisdiction_id=jur.id, source_id=fetch_source_id(session), source_url=spec.source_url,
            native_identifier=spec.key, content_hash=fetch.content_hash,
            domain="Economics and Public Finance", implemented=True,
        )
        session.add(action)
        session.flush()
    else:
        action = existing

    signer = _ensure_named_official(session, spec.signer_name) if spec.signer_name else None
    sponsor = _ensure_named_official(session, spec.sponsor_name) if spec.sponsor_name else None
    session.add(Law(action_id=action.id, public_law_number=spec.key,
                    enacted_date=action.action_date, signed_by_official_id=signer.id if signer else None))
    if sponsor:
        session.add(Bill(action_id=action.id, sponsor_official_id=sponsor.id, status="enacted",
                         became_law_action_id=action.id))

    obj = Objective(action_id=action.id, text=spec.objective_text, source_id=fetch_source_id(session),
                    source_url=spec.source_url, objective_level="statutory")
    session.add(obj)

    # Treated-state employment metric.
    metric = session.execute(
        select(Metric).where(Metric.code == f"state_nonfarm_employment_{spec.state_fips}")
    ).scalar_one_or_none()
    if metric is None:
        metric = Metric(
            code=f"state_nonfarm_employment_{spec.state_fips}",
            name=f"{spec.state_name} Total Nonfarm Employment (SA)",
            unit="thousands of jobs", direction_good="up",
            source_id=session.execute(select(DataSource.id).where(DataSource.name == "BLS")).scalar_one(),
            native_series_id=state_employment_series_id(spec.state_fips),
            domain="Economics and Public Finance",
        )
        session.add(metric)
        session.flush()

    eu = EvaluationUnit(action_id=action.id, objective_id=obj.id, metric_id=metric.id,
                        lag_window_months=spec.lag_window_months, sign_goal=sign_goal, status="pending")
    session.add(eu)
    session.flush()

    preregister(
        session, eu, action_native_id=spec.key, metric_code=metric.code,
        objective_level="statutory", sign_goal=sign_goal,
        lag_window_months=spec.lag_window_months, masked_objective=mask_party_and_name(spec.objective_text)[:280],
    )

    start_year = spec.enacted_year - 3
    end_year = spec.enacted_year + (spec.lag_window_months // 12) + 2
    load_observations(session, metric, start_year, end_year)
    observations = _windowed_observations(
        session, metric.id, date(spec.enacted_year, spec.enacted_month, 1), spec.lag_window_months
    )
    event_period = f"{spec.enacted_year}-{spec.enacted_month:02d}-01"

    # Donor (control) states: land for provenance + build in-memory series for the design.
    donor_observations: dict[str, list[tuple[str, object]]] = {}
    donor_source_urls: list[str] = []
    for dfips in spec.donor_fips:
        dfetch = bls_adapter.fetch(state_employment_series_id(dfips), start_year=start_year, end_year=end_year)
        land(session, dfetch)
        donor_source_urls.append(dfetch.source_url)
        dseries = json.loads(dfetch.content)["Results"]["series"][0]
        donor_observations[dfips] = [
            (f"{pt['year']}-{int(pt['period'][1:]):02d}-01", pt["value"])
            for pt in dseries["data"] if pt["period"].startswith("M")
        ]

    comp = compute_outcome(
        observations, event_period=event_period, lag_window_months=spec.lag_window_months,
        sign_goal=sign_goal, seed=settings.deterministic_seed, donor_observations=donor_observations,
    )
    if comp is None:
        eu.status = "insufficient_evidence"
        eu.non_scoreable_reason = "Insufficient outcome observations around the evaluation window."
        session.flush()
        return ScoreOutcome(action.id, eu.id, eu.status, None, None)

    actx = AttributionContext(
        eu_id=eu.id, action_type=action.type,
        sponsor_official_id=sponsor.id if sponsor else None,
        signer_official_id=signer.id if signer else None,
        vote_margin=None, member_on_winning_side=None,
    )
    attributions = build_attribution(actx)
    return _finalize(
        session, eu, action, comp, attributions,
        alignment=D("0.90"), observations=observations, metric=metric, sign_goal=sign_goal,
        extra_source_urls=donor_source_urls,
    )


def fetch_source_id(session: Session) -> int:
    return session.execute(
        select(DataSource.id).where(DataSource.name == generic_url_adapter.name)
    ).scalar_one()
