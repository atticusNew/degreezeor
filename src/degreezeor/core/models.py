"""Relational schema (SQLAlchemy 2.0 declarative) — the system of record.

Mirrors PLAN.md §11. Design invariants that keep the ambitious goal reachable
(enforced here and by tests):

* ``Action`` is polymorphic via ``type`` (vote/bill/law/eo/regulation/budget/...).
* Outcomes attach to an ``EvaluationUnit`` (Action -> Objective -> Metric -> Baseline),
  never directly to a vote. Votes/sponsorship are *attribution channels*.
* Every externally sourced value carries provenance: tier, source_url, content_hash,
  retrieved_at, native_identifier, and (for Tier-3 mirrors) ``verified_against``.
* ``parties`` exists for transparency but is NEVER read by scoring code (party-blind).
* Score runs are pinned to {data_snapshot, methodology_version, code_git_sha, seed}
  for bit-for-bit reproducibility.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Reference / entity tables
# ---------------------------------------------------------------------------
class Jurisdiction(Base):
    __tablename__ = "jurisdictions"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(20))  # federal|state|county|city
    name: Mapped[str] = mapped_column(String(200))
    fips: Mapped[str | None] = mapped_column(String(10), nullable=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("jurisdictions.id"), nullable=True)


class Party(Base):
    """Stored for transparency only. Scoring code MUST NOT read this table."""

    __tablename__ = "parties"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    abbrev: Mapped[str] = mapped_column(String(10))


class Official(Base):
    __tablename__ = "officials"
    id: Mapped[int] = mapped_column(primary_key=True)
    bioguide_id: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
    fec_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    full_name: Mapped[str] = mapped_column(String(200))
    dob: Mapped[date | None] = mapped_column(Date, nullable=True)


class Office(Base):
    __tablename__ = "offices"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    branch: Mapped[str] = mapped_column(String(20))  # legislative|executive|judicial
    level: Mapped[str] = mapped_column(String(20))  # federal|state|local


class OfficeTerm(Base):
    __tablename__ = "office_terms"
    id: Mapped[int] = mapped_column(primary_key=True)
    official_id: Mapped[int] = mapped_column(ForeignKey("officials.id"))
    office_id: Mapped[int | None] = mapped_column(ForeignKey("offices.id"), nullable=True)
    jurisdiction_id: Mapped[int | None] = mapped_column(ForeignKey("jurisdictions.id"))
    party_id: Mapped[int | None] = mapped_column(ForeignKey("parties.id"))
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------
class DataSource(Base):
    __tablename__ = "data_sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    tier: Mapped[int] = mapped_column(Integer)  # 0=action,1=stats,2=analysis,3=mirror
    base_url: Mapped[str] = mapped_column(String(300))
    license: Mapped[str | None] = mapped_column(String(200), nullable=True)


class RawLanding(Base):
    """Immutable, content-addressed raw snapshot of an official response.

    Parsing happens downstream; this is the audit anchor proving exactly what bytes
    the platform saw and when.
    """

    __tablename__ = "raw_landing"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id"))
    source_url: Mapped[str] = mapped_column(String(600))
    native_identifier: Mapped[str | None] = mapped_column(String(200), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)  # sha256 hex
    byte_size: Mapped[int] = mapped_column(Integer)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    storage_path: Mapped[str] = mapped_column(String(600))


# ---------------------------------------------------------------------------
# Actions (polymorphic) + subtypes
# ---------------------------------------------------------------------------
class Action(Base):
    __tablename__ = "actions"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(20), index=True)
    # vote|bill|law|eo|regulation|budget_line|promise|program
    title: Mapped[str] = mapped_column(Text)
    action_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    jurisdiction_id: Mapped[int | None] = mapped_column(ForeignKey("jurisdictions.id"))
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id"))
    source_url: Mapped[str] = mapped_column(String(600))
    native_identifier: Mapped[str | None] = mapped_column(String(200), index=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(60), nullable=True)
    domain_confidence: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    implemented: Mapped[bool] = mapped_column(Boolean, default=False)


class Bill(Base):
    __tablename__ = "bills"
    action_id: Mapped[int] = mapped_column(ForeignKey("actions.id"), primary_key=True)
    congress: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bill_number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    sponsor_official_id: Mapped[int | None] = mapped_column(ForeignKey("officials.id"))
    status: Mapped[str | None] = mapped_column(String(60), nullable=True)
    became_law_action_id: Mapped[int | None] = mapped_column(ForeignKey("actions.id"))


class Law(Base):
    __tablename__ = "laws"
    action_id: Mapped[int] = mapped_column(ForeignKey("actions.id"), primary_key=True)
    public_law_number: Mapped[str | None] = mapped_column(String(40), index=True)
    statutes_at_large_cite: Mapped[str | None] = mapped_column(String(80), nullable=True)
    enacted_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    signed_by_official_id: Mapped[int | None] = mapped_column(ForeignKey("officials.id"))


class ExecutiveOrder(Base):
    __tablename__ = "executive_orders"
    action_id: Mapped[int] = mapped_column(ForeignKey("actions.id"), primary_key=True)
    eo_number: Mapped[str | None] = mapped_column(String(20), index=True, nullable=True)
    signing_official_id: Mapped[int | None] = mapped_column(ForeignKey("officials.id"))
    fr_doc_number: Mapped[str | None] = mapped_column(String(40), nullable=True)


class Vote(Base):
    __tablename__ = "votes"
    id: Mapped[int] = mapped_column(primary_key=True)
    action_id: Mapped[int] = mapped_column(ForeignKey("actions.id"))
    chamber: Mapped[str] = mapped_column(String(20))  # house|senate
    question: Mapped[str | None] = mapped_column(Text, nullable=True)
    vote_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    yea: Mapped[int] = mapped_column(Integer, default=0)
    nay: Mapped[int] = mapped_column(Integer, default=0)
    present: Mapped[int] = mapped_column(Integer, default=0)
    not_voting: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[str | None] = mapped_column(String(40), nullable=True)


class VotePosition(Base):
    __tablename__ = "vote_positions"
    id: Mapped[int] = mapped_column(primary_key=True)
    vote_id: Mapped[int] = mapped_column(ForeignKey("votes.id"))
    official_id: Mapped[int] = mapped_column(ForeignKey("officials.id"))
    position: Mapped[str] = mapped_column(String(10))  # yea|nay|present|nv


# ---------------------------------------------------------------------------
# Objectives, metrics, observations
# ---------------------------------------------------------------------------
class Objective(Base):
    __tablename__ = "objectives"
    id: Mapped[int] = mapped_column(primary_key=True)
    action_id: Mapped[int] = mapped_column(ForeignKey("actions.id"))
    text: Mapped[str] = mapped_column(Text)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id"))
    source_url: Mapped[str] = mapped_column(String(600))
    # statutory|agency|cbo|sponsor|operational  (precedence order, PLAN.md §5)
    objective_level: Mapped[str] = mapped_column(String(20))
    registered_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Metric(Base):
    __tablename__ = "metrics"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    unit: Mapped[str] = mapped_column(String(60))
    direction_good: Mapped[str] = mapped_column(String(10))  # up|down|context
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id"))
    native_series_id: Mapped[str] = mapped_column(String(80))  # e.g. BLS series id
    domain: Mapped[str | None] = mapped_column(String(60), nullable=True)


class ObjectiveMetricMap(Base):
    __tablename__ = "objective_metric_map"
    id: Mapped[int] = mapped_column(primary_key=True)
    objective_id: Mapped[int] = mapped_column(ForeignKey("objectives.id"))
    metric_id: Mapped[int] = mapped_column(ForeignKey("metrics.id"))
    role: Mapped[str] = mapped_column(String(12), default="primary")  # primary|side_effect
    alignment_score: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registered_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Observation(Base):
    __tablename__ = "observations"
    id: Mapped[int] = mapped_column(primary_key=True)
    metric_id: Mapped[int] = mapped_column(ForeignKey("metrics.id"))
    jurisdiction_id: Mapped[int | None] = mapped_column(ForeignKey("jurisdictions.id"))
    period: Mapped[str] = mapped_column(String(20))  # ISO date or YYYY-Mmm period code
    value: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id"))
    source_url: Mapped[str] = mapped_column(String(600))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64))
    verified_against: Mapped[int | None] = mapped_column(
        ForeignKey("observations.id"), nullable=True
    )
    __table_args__ = (
        UniqueConstraint("metric_id", "jurisdiction_id", "period", name="uq_obs_series_period"),
    )


# ---------------------------------------------------------------------------
# Evaluation units + scoring outputs
# ---------------------------------------------------------------------------
class EvaluationUnit(Base):
    """The atomic *scoreable* unit: (Action -> Objective -> Metric -> Baseline -> Outcome)."""

    __tablename__ = "evaluation_units"
    id: Mapped[int] = mapped_column(primary_key=True)
    action_id: Mapped[int] = mapped_column(ForeignKey("actions.id"))
    objective_id: Mapped[int | None] = mapped_column(ForeignKey("objectives.id"))
    metric_id: Mapped[int | None] = mapped_column(ForeignKey("metrics.id"))
    lag_window_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sign_goal: Mapped[int | None] = mapped_column(Integer, nullable=True)  # +1 / -1
    # Objective->metric fidelity used for scoring (persisted so re-runs are faithful).
    alignment: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    # Evaluation mode: 'baseline' (causal effect vs counterfactual) or 'target'
    # (promise-keeping vs a pre-registered, source-linked numeric target).
    evaluation_mode: Mapped[str] = mapped_column(String(12), default="baseline")
    target_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 6), nullable=True)
    directly_attributable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="pending")
    # pending|scored|non_scoreable_no_objective|non_scoreable_no_metric|
    # non_scoreable_not_implemented|insufficient_evidence|high_model_dependence
    non_scoreable_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    prereg_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prereg_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Baseline(Base):
    __tablename__ = "baselines"
    id: Mapped[int] = mapped_column(primary_key=True)
    eu_id: Mapped[int] = mapped_column(ForeignKey("evaluation_units.id"))
    method: Mapped[str] = mapped_column(String(40))
    spec_json: Mapped[str] = mapped_column(Text)
    baseline_value: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    ci_low: Mapped[Decimal | None] = mapped_column(Numeric(24, 6), nullable=True)
    ci_high: Mapped[Decimal | None] = mapped_column(Numeric(24, 6), nullable=True)


class OutcomeResult(Base):
    __tablename__ = "outcome_results"
    id: Mapped[int] = mapped_column(primary_key=True)
    eu_id: Mapped[int] = mapped_column(ForeignKey("evaluation_units.id"))
    observed: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    baseline_pooled: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    delta: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    z: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    model_dependence: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    ci_low: Mapped[Decimal | None] = mapped_column(Numeric(24, 6), nullable=True)
    ci_high: Mapped[Decimal | None] = mapped_column(Numeric(24, 6), nullable=True)


class AttributionWeight(Base):
    __tablename__ = "attribution_weights"
    id: Mapped[int] = mapped_column(primary_key=True)
    eu_id: Mapped[int] = mapped_column(ForeignKey("evaluation_units.id"))
    official_id: Mapped[int | None] = mapped_column(ForeignKey("officials.id"))
    role: Mapped[str] = mapped_column(String(40))  # sponsor|decisive_vote|signer|residual...
    authority: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    pivotality: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    attribution: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    attr_ci_low: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    attr_ci_high: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    is_residual: Mapped[bool] = mapped_column(Boolean, default=False)


class MethodologyVersion(Base):
    __tablename__ = "methodology_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    semver: Mapped[str] = mapped_column(String(20), unique=True)
    git_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    effective_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    params_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class ScoreRun(Base):
    __tablename__ = "score_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    eu_id: Mapped[int] = mapped_column(ForeignKey("evaluation_units.id"))
    methodology_version_id: Mapped[int] = mapped_column(ForeignKey("methodology_versions.id"))
    data_snapshot_id: Mapped[str] = mapped_column(String(64))  # hash of input content hashes
    code_git_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    seed: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reproducible_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    # JSON list of every official source URL whose data fed this run (incl. donor
    # series for comparison designs), so the source trail is audit-complete.
    input_source_urls: Mapped[str | None] = mapped_column(Text, nullable=True)


class ScoreComponent(Base):
    __tablename__ = "score_components"
    id: Mapped[int] = mapped_column(primary_key=True)
    score_run_id: Mapped[int] = mapped_column(ForeignKey("score_runs.id"))
    component: Mapped[str] = mapped_column(String(20))
    value: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    ci_low: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    ci_high: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    is_value_laden: Mapped[bool] = mapped_column(Boolean, default=False)


class EUScore(Base):
    __tablename__ = "eu_scores"
    id: Mapped[int] = mapped_column(primary_key=True)
    score_run_id: Mapped[int] = mapped_column(ForeignKey("score_runs.id"))
    confidence: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    composite: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    gated: Mapped[bool] = mapped_column(Boolean, default=False)
    coverage: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)


class ConfidenceInterval(Base):
    __tablename__ = "confidence_intervals"
    id: Mapped[int] = mapped_column(primary_key=True)
    score_run_id: Mapped[int] = mapped_column(ForeignKey("score_runs.id"))
    quantity: Mapped[str] = mapped_column(String(40))
    ci_low: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    ci_high: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    method: Mapped[str] = mapped_column(String(40))


class AuditRecord(Base):
    """Append-only, hash-chained audit log. See core.audit."""

    __tablename__ = "audit_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor: Mapped[str] = mapped_column(String(20))  # system|user|reviewer
    event_type: Mapped[str] = mapped_column(String(40))
    payload_json: Mapped[str] = mapped_column(Text)
    prev_hash: Mapped[str] = mapped_column(String(64))
    this_hash: Mapped[str] = mapped_column(String(64), unique=True)


class Dispute(Base):
    __tablename__ = "disputes"
    id: Mapped[int] = mapped_column(primary_key=True)
    eu_id: Mapped[int] = mapped_column(ForeignKey("evaluation_units.id"))
    filer: Mapped[str] = mapped_column(String(120))
    claim: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="open")
    resolution_run_id: Mapped[int | None] = mapped_column(ForeignKey("score_runs.id"))
    public_diff: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = [name for name in dir() if name[0].isupper()]
