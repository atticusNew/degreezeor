"""End-to-end scoring pipeline for one enacted law (PLAN.md §12).

Order is significant for neutrality:
  ingest law + objective  ->  select metric (party-masked, objective-only)
  ->  PRE-REGISTER (hash to audit)  ->  ingest outcome series  ->  compute outcome
  ->  attribution  ->  confidence + gate  ->  pinned, reproducible score run.
"""

from __future__ import annotations

import json
import logging
import os
import re
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
    Vote,
    VotePosition,
)
from degreezeor.core.numeric import D, q_score
from degreezeor.ingestion.adapters.bls import bls_adapter
from degreezeor.ingestion.adapters.generic import generic_url_adapter
from degreezeor.ingestion.landing import ensure_source, land
from degreezeor.ingestion.loader import (
    ensure_bls_source,
    load_executive_order,
    load_house_final_passage_vote,
    load_law,
    load_observations,
    load_senate_final_passage_vote,
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
from degreezeor.scoring.sensitivity import analyze_lag_sensitivity

log = logging.getLogger("degreezeor.pipeline")


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
    "NC-2013-TAX": StatePolicySpec(
        key="NC-2013-TAX",
        title="North Carolina 2013 tax reform",
        state_fips="37",
        state_name="North Carolina",
        # Regional comparison pool (Southeast/border states).
        donor_fips=["45", "51", "13", "47", "21", "29", "01"],  # SC VA GA TN KY MO AL
        source_url="https://www.ncleg.gov/Sessions/2013/Bills/House/PDF/H998v7.pdf",
        objective_text=(
            "Lower and simplify income tax rates to grow the North Carolina economy and "
            "create jobs and employment."
        ),
        enacted_year=2013, enacted_month=7, lag_window_months=48,
        signer_name="Pat McCrory",
    ),
}


@dataclass
class TargetSpec:
    """A curated, source-linked, pre-registered numeric target for target-relative
    ('promise-keeping') scoring. The realized series is a law's own DEFC-tagged
    USAspending total (directly attributable)."""

    key: str
    congress: int
    law_number: int
    law_type: str
    objective_text: str
    defc: str  # USAspending Disaster Emergency Fund Code for this law
    realized_kind: str  # 'outlay' | 'obligation'
    target_source_url: str
    # 'disaster' = COVID-relief DEFCs (obligation+outlay via disaster endpoint);
    # 'general'  = any DEFC (obligations via spending_by_geography; outlays unavailable).
    realized_source: str = "disaster"
    # Committed/promised amount. None => use the committed OBLIGATION from USAspending as
    # the target (delivery/execution: "did the law outlay the funds it committed?"). A
    # number => a curated, source-linked target (e.g. a CBO/statutory figure).
    target_value: float | None = None
    sign_goal: int = 1  # +1 = "deliver at least the committed amount"
    directly_attributable: bool = True


# Demo: of the emergency-relief award funding a law COMMITTED, how much has actually
# been delivered (outlayed)? Directly attributable (the law's own DEFC-tagged money).
TARGET_SPECS: dict[str, TargetSpec] = {
    "CARES-DELIVERY": TargetSpec(
        key="CARES-DELIVERY",
        congress=116, law_number=136, law_type="pub",
        objective_text=(
            "Disburse the committed CARES Act emergency-relief award funding to provide "
            "rapid economic relief (delivery of obligated funds)."
        ),
        defc="N",
        realized_kind="outlay",
        # Committed CARES award funding (USAspending DEFC 'N' obligations snapshot).
        target_value=285_400_000_000.0,
        target_source_url="https://api.usaspending.gov/api/v2/disaster/award/amount/?def_codes=N",
    ),
    # Non-COVID delivery (obligations vs the law's headline appropriation). Only laws whose
    # DEFC obligations are cleanly commensurable with a confident appropriation figure are
    # included — DEFCs whose totals don't map to a single appropriation are deliberately
    # omitted to avoid misrepresentation (integrity guardrail).
    "UKRAINE-2022-DELIVERY": TargetSpec(
        key="UKRAINE-2022-DELIVERY",
        congress=117, law_number=128, law_type="pub",
        objective_text=(
            "Obligate the ~$40.1B appropriated by the Additional Ukraine Supplemental "
            "Appropriations Act, 2022 (share of the law's appropriation obligated to date)."
        ),
        defc="6", realized_kind="obligation", realized_source="general",
        target_value=40_100_000_000.0,
        target_source_url="https://www.congress.gov/bill/117th-congress/house-bill/7691",
    ),
    "IIJA-DELIVERY": TargetSpec(
        key="IIJA-DELIVERY",
        congress=117, law_number=58, law_type="pub",
        objective_text=(
            "Obligate the ~$550B in new investment authorized by the Infrastructure Investment "
            "and Jobs Act (share of the headline new-investment commitment obligated to date)."
        ),
        defc="Z", realized_kind="obligation", realized_source="general",
        target_value=550_000_000_000.0,
        target_source_url="https://www.congress.gov/bill/117th-congress/house-bill/3684",
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
    event_period: str,
    donor_observations: dict[str, list[tuple[str, object]]] | None = None,
    extra_source_urls: list[str] | None = None,
    s_outcome_override: object | None = None,
    definitive: bool = False,
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

    # Sensitivity of the result to the evaluation-horizon choice feeds confidence (§9.10):
    # a direction that flips across defensible lags is fragile.
    sens = analyze_lag_sensitivity(
        observations, event_period=event_period, registered_lag=eu.lag_window_months,
        sign_goal=sign_goal, seed=settings.deterministic_seed,
        donor_observations=donor_observations or None,
    )

    best_method = best_design([e.method for e in comp.per_method])
    conf = compute_confidence(
        best_method=best_method,
        ci_low=comp.ci_low, ci_high=comp.ci_high,
        model_dependence=comp.model_dependence,
        data_tier=1, data_completeness=D("1.0"),
        attribution_widths=human_widths,
        sensitivity_sign_stable=sens.sign_stable,
        definitive=definitive,
    )

    delta_toward_goal = D(sign_goal) * D(comp.delta)
    durability = _durability(observations, comp.eval_period, D(comp.baseline_pooled), sign_goal, delta_toward_goal)
    # Target-relative scoring supplies its own achievement-based S_outcome (delivery of
    # the promised number); baseline-relative maps the standardized effect through the CDF.
    s_outcome = s_outcome_override if s_outcome_override is not None else s_outcome_from_z(comp.z)
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
    existing = _existing_outcome(session, action.id)
    if existing is not None:  # idempotent: already scored (safe batch re-runs)
        return existing

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

    # Ingest the final-passage House roll-call vote so the members who passed the law
    # receive pivotality-weighted decisive-vote attribution (and the full member record
    # is stored for transparency). Best-effort: scoring proceeds even if unavailable.
    vote_margin = None
    decisive_ids: list[int] = []
    senate_margin = None
    senate_decisive_ids: list[int] = []
    if bill and bill.congress and bill.bill_number:
        m = re.match(r"([a-z]+)(\d+)", bill.bill_number)
        if m:
            btype, bnum = m.group(1), int(m.group(2))
            try:
                result = load_house_final_passage_vote(session, action, bill.congress, btype, bnum)
                if result is not None:
                    hv, decisive_ids = result
                    vote_margin = hv.margin
            except Exception as exc:  # noqa: BLE001 - vote data is optional, never block scoring
                log.warning("house vote ingestion failed for %s: %s", action.native_identifier, exc)
            try:
                sresult = load_senate_final_passage_vote(session, action, bill.congress, btype, bnum)
                if sresult is not None:
                    sv, senate_decisive_ids = sresult
                    senate_margin = sv.margin
            except Exception as exc:  # noqa: BLE001 - vote data is optional, never block scoring
                log.warning("senate vote ingestion failed for %s: %s", action.native_identifier, exc)

    actx = AttributionContext(
        eu_id=eu.id,
        action_type=action.type,
        sponsor_official_id=bill.sponsor_official_id if bill else None,
        signer_official_id=law.signed_by_official_id if law else None,
        vote_margin=vote_margin,
        member_on_winning_side=bool(decisive_ids),
        decisive_official_ids=decisive_ids,
        senate_vote_margin=senate_margin,
        senate_decisive_official_ids=senate_decisive_ids,
    )
    attributions = build_attribution(actx)
    return _finalize(
        session, eu, action, comp, attributions,
        alignment=D(primary.alignment), observations=observations, metric=metric, sign_goal=sign_goal,
        event_period=event_period,
    )


def score_executive_order(session: Session, document_number: str) -> ScoreOutcome:
    """Ingest + score one executive order (Federal Register) end-to-end.

    Same neutral machinery as laws; attribution gives the signing president high
    executive authority (EOs are unilateral). Most EOs will be non-scoreable or
    insufficient-evidence (narrow / diffuse objectives) — reported honestly.
    """
    ensure_bls_source(session)
    action = load_executive_order(session, document_number)
    existing = _existing_outcome(session, action.id)
    if existing is not None:
        return existing

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
        event_period=event_period,
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


def _rescore_target_eu(session: Session, eu, action, metric) -> ScoreOutcome:
    from degreezeor.ingestion.adapters.usaspending import usaspending_adapter
    from degreezeor.scoring.target_outcome import compute_target_outcome

    # Curated-fact EUs (e.g. court survival) store the realized value directly — re-runs
    # use it as-is (no re-fetch), so they're deterministic by construction.
    if eu.realized_value is not None:
        event_period = f"{action.action_date.year}-{action.action_date.month:02d}-01"
        from degreezeor.scoring.target_outcome import compute_target_outcome
        tc = compute_target_outcome(
            realized=float(eu.realized_value), target=float(eu.target_value), sign_goal=eu.sign_goal,
            directly_attributable=bool(eu.directly_attributable), eval_period=event_period)
        eo = session.get(ExecutiveOrder, action.id)
        law = session.get(Law, action.id)
        bill = session.get(Bill, action.id)
        signer = (law.signed_by_official_id if law else None) or (eo.signing_official_id if eo else None)
        actx = AttributionContext(
            eu_id=eu.id, action_type=action.type,
            sponsor_official_id=bill.sponsor_official_id if bill else None,
            signer_official_id=signer, vote_margin=None, member_on_winning_side=None)
        attributions = build_attribution(actx)
        return _finalize(session, eu, action, tc.outcome, attributions, alignment=eu.alignment,
                         observations=[], metric=metric, sign_goal=eu.sign_goal,
                         event_period=event_period, s_outcome_override=tc.s_outcome, definitive=True)

    # native_series_id: "DEFC:<code>:<kind>" | "DEFCGEN:<code>:obligation" |
    #                    "AGENCYBUDGET:<toptier>:<fy>:<kind>"
    parts = metric.native_series_id.split(":")
    prefix = parts[0]
    prev = os.environ.get("DZ_HTTP_CACHE")
    os.environ["DZ_HTTP_CACHE"] = "1"
    try:
        if prefix == "AGENCYBUDGET":
            _, toptier, fy, realized_kind = parts
            rfetch = usaspending_adapter.fetch_agency_budget(toptier)
            realized = usaspending_adapter.parse_agency_budget(rfetch.content, int(fy))[realized_kind]
        elif prefix == "DEFCGEN":
            defc = parts[1]
            rfetch = usaspending_adapter.fetch_general_obligations(defc, action.action_date.year - 1, 2025)
            realized = usaspending_adapter.parse_general_obligation(rfetch.content)
        else:
            defc, realized_kind = parts[1], parts[2]
            rfetch = usaspending_adapter.fetch(defc)
            realized = usaspending_adapter.parse_amounts(rfetch.content)[realized_kind]
    finally:
        if prev is None:
            os.environ.pop("DZ_HTTP_CACHE", None)
        else:
            os.environ["DZ_HTTP_CACHE"] = prev
    event_period = f"{action.action_date.year}-{action.action_date.month:02d}-01"
    tc = compute_target_outcome(
        realized=realized, target=eu.target_value, sign_goal=eu.sign_goal,
        directly_attributable=bool(eu.directly_attributable), eval_period=event_period,
    )
    bill = session.get(Bill, action.id)
    law = session.get(Law, action.id)
    eo = session.get(ExecutiveOrder, action.id)
    signer = (law.signed_by_official_id if law else None) or (eo.signing_official_id if eo else None)
    if signer is None and action.type == "budget":
        # Budget actions store no Law/EO row; re-derive the executing president from the
        # action date (same as scoring) so the attribution reproduces exactly.
        from degreezeor.core.reference import president_on
        pres = president_on(session, action.action_date)
        signer = pres.id if pres else None
    actx = AttributionContext(
        eu_id=eu.id, action_type=action.type,
        sponsor_official_id=bill.sponsor_official_id if bill else None,
        signer_official_id=signer,
        vote_margin=None, member_on_winning_side=None,
    )
    attributions = build_attribution(actx)
    return _finalize(
        session, eu, action, tc.outcome, attributions,
        alignment=eu.alignment, observations=[], metric=metric, sign_goal=eu.sign_goal,
        event_period=event_period, extra_source_urls=[rfetch.source_url],
        s_outcome_override=tc.s_outcome,
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

    # Target-relative EUs re-run by re-observing the directly-attributable realized
    # series (cache-first) and recomputing against the STORED target — no counterfactual.
    if eu.evaluation_mode == "target":
        return _rescore_target_eu(session, eu, action, metric)

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

    # Reconstruct decisive-vote attribution from STORED roll-call rows (no re-fetch),
    # so the re-run reproduces the original attribution exactly — for BOTH chambers.
    def _stored_decisive(chamber: str) -> tuple[int | None, list[int]]:
        v = session.execute(
            select(Vote).where(Vote.action_id == action.id, Vote.chamber == chamber)
            .order_by(Vote.id.desc()).limit(1)
        ).scalar_one_or_none()
        if v is None:
            return None, []
        winning = "yea" if v.yea >= v.nay else "nay"
        ids = list(session.execute(
            select(VotePosition.official_id).where(
                VotePosition.vote_id == v.id, VotePosition.position == winning
            )
        ).scalars())
        return abs(v.yea - v.nay), ids

    vote_margin, decisive_ids = _stored_decisive("house")
    senate_margin, senate_decisive_ids = _stored_decisive("senate")

    actx = AttributionContext(
        eu_id=eu.id, action_type=action.type,
        sponsor_official_id=bill.sponsor_official_id if bill else None,
        signer_official_id=signer, vote_margin=vote_margin,
        member_on_winning_side=bool(decisive_ids), decisive_official_ids=decisive_ids,
        senate_vote_margin=senate_margin, senate_decisive_official_ids=senate_decisive_ids,
    )
    attributions = build_attribution(actx)
    return _finalize(
        session, eu, action, comp, attributions,
        alignment=eu.alignment, observations=observations, metric=metric, sign_goal=eu.sign_goal,
        event_period=event_period, donor_observations=donor_observations,
        extra_source_urls=donor_source_urls,
    )


def score_target(session: Session, spec: TargetSpec) -> ScoreOutcome:
    """Target-relative ('promise-keeping') scoring: did the law DELIVER its committed
    number, measured by its own directly-attributable USAspending DEFC spending?"""
    from degreezeor.ingestion.adapters.usaspending import usaspending_adapter
    from degreezeor.scoring.target_outcome import compute_target_outcome

    action = load_law(session, spec.congress, spec.law_number, spec.law_type)
    usa_src = ensure_source(
        session, name=usaspending_adapter.name, tier=usaspending_adapter.tier,
        base_url=usaspending_adapter.base_url,
    )

    is_general = spec.realized_source == "general"
    metric_code = (f"obligation_delivery_{spec.defc}" if is_general
                   else f"relief_delivery_{spec.defc}")
    native_series = (f"DEFCGEN:{spec.defc}:obligation" if is_general
                     else f"DEFC:{spec.defc}:{spec.realized_kind}")
    metric = session.execute(
        select(Metric).where(Metric.code == metric_code)
    ).scalar_one_or_none()
    if metric is None:
        metric = Metric(
            code=metric_code,
            name=(f"Realized federal {'obligations' if is_general else spec.realized_kind + 's'}, "
                  f"DEFC {spec.defc} (USAspending)"),
            unit="USD", direction_good="up", source_id=usa_src.id,
            native_series_id=native_series, domain="Economics and Public Finance",
        )
        session.add(metric)
        session.flush()

    existing = _existing_outcome_for_metric(session, action.id, metric.id)
    if existing is not None:  # idempotent: already scored (cron-safe)
        return existing
    obj = Objective(action_id=action.id, text=spec.objective_text, source_id=usa_src.id,
                    source_url=spec.target_source_url, objective_level="operational")
    session.add(obj)
    session.flush()
    target_kind = "curated" if spec.target_value is not None else "committed_obligation"
    eu = EvaluationUnit(
        action_id=action.id, objective_id=obj.id, metric_id=metric.id,
        lag_window_months=0, sign_goal=spec.sign_goal, status="pending",
        evaluation_mode="target", target_value=None,
        directly_attributable=spec.directly_attributable,
    )
    session.add(eu)
    session.flush()

    # Pre-register the RULE (metric, mode, target_kind, goal) BEFORE observing spending.
    # For committed_obligation the target NUMBER is the committed amount observed at fetch,
    # but the evaluation rule is fixed in advance (analogous to a pre-registered baseline).
    preregister(
        session, eu, action_native_id=action.native_identifier, metric_code=metric.code,
        objective_level="operational", sign_goal=spec.sign_goal, lag_window_months=0,
        masked_objective=(f"target_mode target_kind={target_kind} directly_attributable="
                          f"{spec.directly_attributable} :: {spec.objective_text}")[:280],
    )

    # Observe realized (directly-attributable) spending.
    if is_general:
        # Start at the fiscal year containing enactment (FY begins Oct 1 of year-1).
        rfetch = usaspending_adapter.fetch_general_obligations(
            spec.defc, action.action_date.year - 1, 2025)
        land(session, rfetch)
        realized = usaspending_adapter.parse_general_obligation(rfetch.content)
        target_amount = spec.target_value  # curated appropriation (required for 'general')

        # INTEGRITY GUARDS for non-COVID obligation totals (no stable outlay series):
        # (1) window-stability — the total must not change materially with the query window;
        # (2) commensurability — obligations must not exceed the appropriation (else the DEFC
        # total isn't a clean delivery measure). Fragile cases are rejected, not published.
        wide = usaspending_adapter.fetch_general_obligations(spec.defc, action.action_date.year - 5, 2025)
        land(session, wide)
        realized_wide = usaspending_adapter.parse_general_obligation(wide.content)
        denom = max(realized, realized_wide, 1.0)
        if abs(realized - realized_wide) / denom > 0.05:
            eu.status = "non_scoreable_no_metric"
            eu.non_scoreable_reason = (
                "Obligation total is not stable across query windows, so a reliable "
                "delivery share can't be computed (integrity guard)."
            )
            session.flush()
            return ScoreOutcome(action.id, eu.id, eu.status, None, None)
        if target_amount and realized > target_amount * 1.1:
            eu.status = "non_scoreable_no_metric"
            eu.non_scoreable_reason = (
                "DEFC obligations exceed the law's appropriation, so the total isn't "
                "commensurable with a clean delivery measure (integrity guard)."
            )
            session.flush()
            return ScoreOutcome(action.id, eu.id, eu.status, None, None)
    else:
        rfetch = usaspending_adapter.fetch(spec.defc)
        land(session, rfetch)
        amounts = usaspending_adapter.parse_amounts(rfetch.content)
        realized = amounts[spec.realized_kind]
        target_amount = spec.target_value if spec.target_value is not None else amounts["obligation"]
    if not target_amount:
        eu.status = "non_scoreable_no_metric"
        eu.non_scoreable_reason = (
            f"No award-level spending is tracked for DEFC {spec.defc} via the USAspending "
            "disaster endpoint (e.g. non-COVID supplementals), so delivery isn't measurable here."
        )
        session.flush()
        return ScoreOutcome(action.id, eu.id, eu.status, None, None)
    eu.target_value = D(str(target_amount))

    event_period = f"{action.action_date.year}-{action.action_date.month:02d}-01"
    tc = compute_target_outcome(
        realized=realized, target=target_amount, sign_goal=spec.sign_goal,
        directly_attributable=spec.directly_attributable, eval_period=event_period,
    )

    bill = session.get(Bill, action.id)
    law = session.get(Law, action.id)
    actx = AttributionContext(
        eu_id=eu.id, action_type=action.type,
        sponsor_official_id=bill.sponsor_official_id if bill else None,
        signer_official_id=law.signed_by_official_id if law else None,
        vote_margin=None, member_on_winning_side=None,
    )
    attributions = build_attribution(actx)
    return _finalize(
        session, eu, action, tc.outcome, attributions,
        alignment=D("0.95"), observations=[], metric=metric, sign_goal=spec.sign_goal,
        event_period=event_period, extra_source_urls=[rfetch.source_url],
        s_outcome_override=tc.s_outcome,
    )


def ingest_defc_delivery(session: Session, limit: int | None = None) -> list[ScoreOutcome]:
    """#1 — batch verifiable 'delivery' scores: for every law with DEFC-tagged spending,
    score realized USAspending outlays vs the funds it committed (directly attributable)."""
    from degreezeor.ingestion.adapters.usaspending import usaspending_adapter

    results: list[ScoreOutcome] = []
    for entry in usaspending_adapter.def_codes():
        if limit is not None and len(results) >= limit:
            break
        spec = TargetSpec(
            key=f"DEFC-{entry['code']}",
            congress=entry["congress"], law_number=entry["law_number"], law_type="pub",
            objective_text=(
                f"Disburse the funds committed under {entry['title']} "
                f"(DEFC {entry['code']}) — delivery of obligated emergency/supplemental funding."
            ),
            defc=entry["code"], realized_kind="outlay", target_value=None,
            target_source_url=(
                f"https://api.usaspending.gov/api/v2/disaster/award/amount/?def_codes={entry['code']}"
            ),
        )
        try:
            results.append(score_target(session, spec))
        except Exception as exc:  # noqa: BLE001 - skip a DEFC that can't be resolved/scored
            log.warning("DEFC %s delivery scoring failed: %s", entry["code"], exc)
    return results


def _existing_outcome_for_metric(session: Session, action_id: int, metric_id: int) -> ScoreOutcome | None:
    """Idempotency for target/court/state scorers: if this (action, metric) is already
    scored, return its outcome so a nightly cron can re-run safely without duplicates."""
    eu = session.execute(
        select(EvaluationUnit).where(
            EvaluationUnit.action_id == action_id, EvaluationUnit.metric_id == metric_id
        ).order_by(EvaluationUnit.id.desc()).limit(1)
    ).scalar_one_or_none()
    if eu is None:
        return None
    run = session.execute(
        select(ScoreRun).where(ScoreRun.eu_id == eu.id).order_by(ScoreRun.id.desc()).limit(1)
    ).scalar_one_or_none()
    return ScoreOutcome(action_id, eu.id, eu.status,
                        run.id if run else None, run.reproducible_hash if run else None)


def _existing_outcome(session: Session, action_id: int) -> ScoreOutcome | None:
    """If this action already has an evaluation unit, return its outcome (idempotency)."""
    eu = session.execute(
        select(EvaluationUnit).where(EvaluationUnit.action_id == action_id)
        .order_by(EvaluationUnit.id.desc()).limit(1)
    ).scalar_one_or_none()
    if eu is None:
        return None
    run = session.execute(
        select(ScoreRun).where(ScoreRun.eu_id == eu.id).order_by(ScoreRun.id.desc()).limit(1)
    ).scalar_one_or_none()
    return ScoreOutcome(action_id, eu.id, eu.status,
                        run.id if run else None, run.reproducible_hash if run else None)


def batch_score_laws(session: Session, congress: int, limit: int = 25) -> list[ScoreOutcome]:
    """#2 — breadth: ingest + score enacted laws for a congress (bounded by ``limit``).
    Most land as insufficient-evidence / non-scoreable — the honest denominator that makes
    the scored subset interpretable. Idempotent (skips laws already scored)."""
    import json as _json

    from degreezeor.ingestion.adapters.congress import congress_adapter

    results: list[ScoreOutcome] = []
    offset = 0
    while len(results) < limit:
        page = _json.loads(congress_adapter.fetch_law_list(congress, 250, offset).content)
        bills = page.get("bills", [])
        if not bills:
            break
        for b in bills:
            if len(results) >= limit:
                break
            laws = b.get("laws") or []
            if not laws:
                continue
            m = re.match(r"\d+-(\d+)", laws[0].get("number", ""))
            if not m:
                continue
            law_number = int(m.group(1))
            law_type = "pub" if "Public" in (laws[0].get("type") or "") else "priv"
            try:
                results.append(score_law(session, congress, law_number, law_type))
            except Exception as exc:  # noqa: BLE001 - one bad law must not abort the batch
                log.warning("batch law %s-%s failed: %s", congress, law_number, exc)
        offset += 250
    return results


def batch_score_executive_orders(session: Session, limit: int = 25) -> list[ScoreOutcome]:
    """#2 — breadth: ingest + score recent executive orders (Federal Register, keyless)."""
    import json as _json

    from degreezeor.ingestion.adapters.federalregister import federal_register_adapter
    from degreezeor.ingestion.http import client as _client

    url = f"{federal_register_adapter.base_url}/documents.json"
    params = {
        "conditions[type][]": "PRESDOCU",
        "conditions[presidential_document_type][]": "executive_order",
        "order": "newest", "per_page": str(min(limit, 100)),
    }
    content = _client.get_bytes(url, params=params)
    docs = _json.loads(content).get("results", [])
    results: list[ScoreOutcome] = []
    for d in docs[:limit]:
        doc_number = d.get("document_number")
        if not doc_number:
            continue
        try:
            results.append(score_executive_order(session, doc_number))
        except Exception as exc:  # noqa: BLE001
            log.warning("batch EO %s failed: %s", doc_number, exc)
    return results


@dataclass
class CourtSurvivalSpec:
    """A curated, source-linked judicial-review outcome for an executive order.

    The disposition is a curated public FACT (not NLP-inferred); CourtListener supplies
    case metadata for provenance. Survival index: upheld=100, partial=50, struck=0.
    Ambiguous/ongoing cases are marked 'pending' and left non-scoreable.
    """

    key: str
    eo_document_number: str  # Federal Register doc number of the EO
    disposition: str  # upheld | partial | struck | pending
    case_query: str  # CourtListener search query for provenance
    note: str


_SURVIVAL_INDEX = {"upheld": 100.0, "partial": 50.0, "struck": 0.0}

# Curated set — only unambiguous, well-documented final outcomes are scored.
COURT_SURVIVAL_SPECS: dict[str, CourtSurvivalSpec] = {
    "EO13780-TRAVELBAN": CourtSurvivalSpec(
        key="EO13780-TRAVELBAN", eo_document_number="2017-04837", disposition="upheld",
        case_query="Trump v. Hawaii travel ban",
        note="Proclamation 9645 / EO 13780 travel restrictions UPHELD by the Supreme Court "
             "in Trump v. Hawaii (2018).",
    ),
    "EO14042-CONTRACTOR-VAX": CourtSurvivalSpec(
        key="EO14042-CONTRACTOR-VAX", eo_document_number="2021-19924", disposition="struck",
        case_query="Georgia v. Biden federal contractor vaccine mandate",
        note="EO 14042 federal-contractor vaccine mandate was nationally enjoined, never "
             "enforced, and later revoked.",
    ),
}


def score_court_survival(session: Session, spec: CourtSurvivalSpec) -> ScoreOutcome:
    """Court-survival vertical: how much of an executive order survived judicial review?
    Curated disposition (source-linked) -> survival index; attributed to the issuing
    president with a large residual (judicial composition is exogenous)."""
    from degreezeor.ingestion.adapters.courtlistener import courtlistener_adapter

    if spec.disposition == "pending" or spec.disposition not in _SURVIVAL_INDEX:
        # Not final -> honest non-scoreable (don't score ongoing litigation).
        action = load_executive_order(session, spec.eo_document_number)
        eu = EvaluationUnit(action_id=action.id, status="insufficient_evidence",
                            non_scoreable_reason="Litigation not final (disposition pending).",
                            evaluation_mode="target")
        session.add(eu)
        session.flush()
        return ScoreOutcome(action.id, eu.id, eu.status, None, None)

    action = load_executive_order(session, spec.eo_document_number)
    cl_src = ensure_source(session, name=courtlistener_adapter.name, tier=courtlistener_adapter.tier,
                           base_url=courtlistener_adapter.base_url)
    # Provenance: fetch + land the case metadata (NOT used to infer the disposition).
    cfetch = courtlistener_adapter.fetch(spec.case_query)
    land(session, cfetch)
    case = courtlistener_adapter.top_case(cfetch.content) or {}
    case_url = case.get("url") or cfetch.source_url

    survival = _SURVIVAL_INDEX[spec.disposition]
    metric = session.execute(
        select(Metric).where(Metric.code == "legal_survival")
    ).scalar_one_or_none()
    if metric is None:
        metric = Metric(code="legal_survival", name="Legal survival index (judicial review)",
                        unit="index", direction_good="up", source_id=cl_src.id,
                        native_series_id="CURATED:court_survival", domain="Law")
        session.add(metric)
        session.flush()
    existing = _existing_outcome_for_metric(session, action.id, metric.id)
    if existing is not None:  # idempotent (cron-safe)
        return existing
    obj = Objective(action_id=action.id, source_id=cl_src.id, source_url=case_url,
                    objective_level="executive",
                    text=(f"Survive judicial review: {spec.note} (disposition: {spec.disposition}; "
                          f"source: {case.get('case_name') or spec.case_query})."))
    session.add(obj)
    session.flush()
    eu = EvaluationUnit(action_id=action.id, objective_id=obj.id, metric_id=metric.id,
                        lag_window_months=0, sign_goal=1, status="pending",
                        evaluation_mode="target", target_value=D("100"),
                        realized_value=D(str(survival)), directly_attributable=True)
    session.add(eu)
    session.flush()
    preregister(session, eu, action_native_id=action.native_identifier, metric_code=metric.code,
                objective_level="executive", sign_goal=1, lag_window_months=0,
                masked_objective=f"court_survival disposition={spec.disposition}"[:280])

    from degreezeor.scoring.target_outcome import compute_target_outcome
    event_period = f"{action.action_date.year}-{action.action_date.month:02d}-01"
    tc = compute_target_outcome(realized=survival, target=100.0, sign_goal=1,
                                directly_attributable=True, eval_period=event_period)
    eo = session.get(ExecutiveOrder, action.id)
    actx = AttributionContext(eu_id=eu.id, action_type="eo", sponsor_official_id=None,
                              signer_official_id=eo.signing_official_id if eo else None,
                              vote_margin=None, member_on_winning_side=None)
    attributions = build_attribution(actx)
    return _finalize(session, eu, action, tc.outcome, attributions, alignment=D("0.95"),
                     observations=[], metric=metric, sign_goal=1, event_period=event_period,
                     extra_source_urls=[case_url], s_outcome_override=tc.s_outcome, definitive=True)


def score_budget_execution(
    session: Session, toptier_code: str, agency_name: str, fiscal_year: int,
    realized_kind: str = "obligated",
) -> ScoreOutcome:
    """#2 — account-level budget execution: did the agency obligate/outlay the budgetary
    resources available to it in a fiscal year? Stable + commensurable by construction
    (obligated/outlayed <= resources). Directly attributable to the administration (with a
    large residual, since execution is diffuse across the agency and career staff)."""
    from degreezeor.core.reference import ensure_us_federal, president_on
    from degreezeor.ingestion.adapters.usaspending import usaspending_adapter
    from degreezeor.scoring.target_outcome import compute_target_outcome

    usa_src = ensure_source(session, name=usaspending_adapter.name, tier=usaspending_adapter.tier,
                            base_url=usaspending_adapter.base_url)
    jur = ensure_us_federal(session)
    native_id = f"BUDGET:{toptier_code}:{fiscal_year}"
    fy_end = date(fiscal_year, 9, 30)

    bfetch = usaspending_adapter.fetch_agency_budget(toptier_code)
    amounts = usaspending_adapter.parse_agency_budget(bfetch.content, fiscal_year)

    existing = session.execute(
        select(Action).where(Action.native_identifier == native_id, Action.type == "budget")
    ).scalar_one_or_none()
    if existing is not None:
        out = _existing_outcome(session, existing.id)
        if out is not None:
            return out
        action = existing
    else:
        action = Action(
            type="budget", title=f"{agency_name} — FY{fiscal_year} budget execution",
            action_date=fy_end, jurisdiction_id=jur.id, source_id=usa_src.id,
            source_url=bfetch.source_url, native_identifier=native_id,
            content_hash=bfetch.content_hash, domain="Economics and Public Finance",
            implemented=True,
        )
        session.add(action)
        session.flush()
    land(session, bfetch)

    eu = EvaluationUnit(action_id=action.id, status="pending", evaluation_mode="target",
                        sign_goal=1, lag_window_months=0, directly_attributable=True)
    if amounts is None or amounts["resources"] <= 0:
        eu.status = "non_scoreable_no_metric"
        eu.non_scoreable_reason = f"No budgetary-resources data for {agency_name} FY{fiscal_year}."
        session.add(eu)
        session.flush()
        return ScoreOutcome(action.id, eu.id, eu.status, None, None)

    metric = session.execute(
        select(Metric).where(Metric.code == f"agency_budget_{toptier_code}_{fiscal_year}_{realized_kind}")
    ).scalar_one_or_none()
    if metric is None:
        metric = Metric(
            code=f"agency_budget_{toptier_code}_{fiscal_year}_{realized_kind}",
            name=f"{agency_name} FY{fiscal_year} {realized_kind} (USAspending)",
            unit="USD", direction_good="up", source_id=usa_src.id,
            native_series_id=f"AGENCYBUDGET:{toptier_code}:{fiscal_year}:{realized_kind}",
            domain="Economics and Public Finance",
        )
        session.add(metric)
        session.flush()
    obj = Objective(action_id=action.id, source_id=usa_src.id, source_url=bfetch.source_url,
                    objective_level="operational",
                    text=(f"Obligate/outlay the budgetary resources available to {agency_name} "
                          f"in FY{fiscal_year} (execution of appropriated funds)."))
    session.add(obj)
    session.flush()
    eu.objective_id = obj.id
    eu.metric_id = metric.id
    eu.target_value = D(str(amounts["resources"]))
    session.add(eu)
    session.flush()

    preregister(session, eu, action_native_id=native_id, metric_code=metric.code,
                objective_level="operational", sign_goal=1, lag_window_months=0,
                masked_objective=f"budget_execution {agency_name} FY{fiscal_year} {realized_kind}"[:280])

    realized = amounts[realized_kind]
    tc = compute_target_outcome(realized=realized, target=amounts["resources"], sign_goal=1,
                                directly_attributable=True, eval_period=f"{fiscal_year}-09-01")
    signer = president_on(session, fy_end)
    actx = AttributionContext(
        eu_id=eu.id, action_type="budget", sponsor_official_id=None,
        signer_official_id=signer.id if signer else None,
        vote_margin=None, member_on_winning_side=None,
    )
    attributions = build_attribution(actx)
    return _finalize(session, eu, action, tc.outcome, attributions, alignment=D("0.95"),
                     observations=[], metric=metric, sign_goal=1,
                     event_period=f"{fiscal_year}-09-01", extra_source_urls=[bfetch.source_url],
                     s_outcome_override=tc.s_outcome)


def ingest_budget_execution(
    session: Session, fiscal_year: int, agencies: list[tuple[str, str]] | None = None,
    realized_kind: str = "obligated", limit: int | None = None,
) -> list[ScoreOutcome]:
    """Batch budget-execution scores. ``agencies`` = list of (toptier_code, name); if None,
    use the major cabinet departments."""
    import json as _json

    from degreezeor.ingestion.adapters.usaspending import usaspending_adapter

    if agencies is None:
        allag = _json.loads(usaspending_adapter.fetch_toptier_agencies()).get("results", [])
        wanted = {"Department of"}  # cabinet departments
        agencies = [(a["toptier_code"], a["agency_name"]) for a in allag
                    if any(a["agency_name"].startswith(w) for w in wanted)]
    results: list[ScoreOutcome] = []
    for code, name in agencies:
        if limit is not None and len(results) >= limit:
            break
        try:
            results.append(score_budget_execution(session, code, name, fiscal_year, realized_kind))
        except Exception as exc:  # noqa: BLE001
            log.warning("budget execution %s FY%s failed: %s", name, fiscal_year, exc)
    return results


def ingest_state_policies(session: Session, keys: list[str] | None = None) -> list[ScoreOutcome]:
    """Tier-4 batch: score curated state policies via synthetic control. The pre-fit gate
    decides which are scoreable (poor donor fit -> honest non-scoreable)."""
    results: list[ScoreOutcome] = []
    for key in (keys or list(STATE_POLICIES)):
        spec = STATE_POLICIES.get(key)
        if spec is None:
            continue
        try:
            results.append(score_state_policy(session, spec))
        except Exception as exc:  # noqa: BLE001
            log.warning("state policy %s failed: %s", key, exc)
    return results


def refresh_all(
    session: Session, *, budget_fiscal_year: int = 2024, congress: int = 117,
    law_limit: int = 25, eo_limit: int = 15,
) -> dict[str, int]:
    """Idempotent full ingestion/scoring pass — the production CRON entrypoint.

    Every scorer skips already-scored units, so this can run on a schedule without
    creating duplicates. Returns a per-stage count of evaluation units produced.
    """
    counts: dict[str, int] = {}
    counts["defc_delivery"] = len(ingest_defc_delivery(session))
    counts["budget_execution"] = len(ingest_budget_execution(session, budget_fiscal_year))
    counts["state_policies"] = len(ingest_state_policies(session))
    counts["court_survival"] = sum(
        1 for spec in COURT_SURVIVAL_SPECS.values() if score_court_survival(session, spec)
    )
    counts["curated_targets"] = sum(
        1 for key in ("CARES-DELIVERY", "IIJA-DELIVERY", "UKRAINE-2022-DELIVERY")
        if score_target(session, TARGET_SPECS[key])
    )
    counts["laws"] = len(batch_score_laws(session, congress, limit=law_limit))
    counts["executive_orders"] = len(batch_score_executive_orders(session, limit=eo_limit))
    # Best-effort name enrichment (bounded by Congress.gov throughput).
    try:
        from degreezeor.ingestion.loader import enrich_official_names
        counts["names_enriched"] = enrich_official_names(session)
    except Exception as exc:  # noqa: BLE001
        log.warning("name enrichment skipped: %s", exc)

    # Self-validate: the nightly pass must leave the append-only audit chain intact.
    # A break here means history was altered out-of-band — surfaced loudly, never hidden.
    session.flush()
    chain_ok, broken_id = audit.verify_chain(session)
    counts["audit_chain_ok"] = 1 if chain_ok else 0
    if not chain_ok:
        log.error("AUDIT CHAIN BROKEN after refresh (first broken record id=%s)", broken_id)
    return counts


@dataclass(frozen=True)
class ReproCheck:
    eu_id: int
    status: str  # reproduced | mismatch | error
    stored_hash: str | None
    recomputed_hash: str | None
    detail: str | None = None


@dataclass(frozen=True)
class ReproAudit:
    total: int  # scored EUs checked (those with a pinned reproducible hash)
    reproduced: int
    mismatched: int
    errored: int
    checks: list[ReproCheck]

    @property
    def all_reproduced(self) -> bool:
        # An audit "passes" only if every checkable score reproduced AND none mismatched.
        # Errors (e.g. a cold cache that can't re-fetch a series) are inconclusive, not
        # failures, but are reported so an operator can investigate.
        return self.mismatched == 0 and self.errored == 0 and self.total > 0


def verify_all_reproducible(session: Session) -> ReproAudit:
    """Platform-wide reproducibility self-audit (PLAN §9.9 / §16).

    Independently RE-RUNS every published score from its stored inputs and asserts each
    one reproduces its pinned ``reproducible_hash`` bit-for-bit — the operational proof
    that scores are deterministic and untampered. Each re-run happens inside a SAVEPOINT
    that is rolled back, so the audit never mutates the database (no extra score runs).

    A mismatch means the stored score does not regenerate from its recorded inputs +
    methodology — i.e. non-determinism or tampering — and is a hard failure. An error
    (e.g. a cold replay cache) is inconclusive and reported separately.
    """
    checks: list[ReproCheck] = []
    eus = session.execute(select(EvaluationUnit)).scalars().all()
    for eu in eus:
        run = session.execute(
            select(ScoreRun).where(ScoreRun.eu_id == eu.id).order_by(ScoreRun.id.desc()).limit(1)
        ).scalar_one_or_none()
        if run is None or run.reproducible_hash is None:
            continue  # not a scored EU
        stored = run.reproducible_hash
        sp = session.begin_nested()
        status, recomputed, detail = "error", None, None
        try:
            result = rescore_eu(session, eu.id)
            recomputed = result.reproducible_hash
            status = "reproduced" if recomputed == stored else "mismatch"
        except Exception as exc:  # noqa: BLE001 - inconclusive (e.g. cold cache), not a failure
            detail = str(exc)[:200]
        finally:
            if sp.is_active:
                sp.rollback()
            # Drop any stale identity-map state from the rolled-back re-run before the
            # next EU, so each check reads fresh persisted rows.
            session.expire_all()
        checks.append(ReproCheck(eu.id, status, stored, recomputed, detail))

    return ReproAudit(
        total=len(checks),
        reproduced=sum(1 for c in checks if c.status == "reproduced"),
        mismatched=sum(1 for c in checks if c.status == "mismatch"),
        errored=sum(1 for c in checks if c.status == "error"),
        checks=checks,
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

    existing = _existing_outcome_for_metric(session, action.id, metric.id)
    if existing is not None:  # idempotent (cron-safe)
        return existing
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
        event_period=event_period, donor_observations=donor_observations,
        extra_source_urls=donor_source_urls,
    )


def fetch_source_id(session: Session) -> int:
    return session.execute(
        select(DataSource.id).where(DataSource.name == generic_url_adapter.name)
    ).scalar_one()
