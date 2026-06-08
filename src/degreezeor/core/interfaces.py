"""Pluggable interfaces — the anti-dead-end contracts.

The MVP slice ships *one* concrete implementation behind each interface (one source
family, one baseline method, a few attribution channels). Additional sources,
baseline methods, and attribution channels plug in later WITHOUT redesign, because
the rest of the system depends only on these abstractions.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


# ---------------------------------------------------------------------------
# Source adapters
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RawFetch:
    """Raw bytes captured from an official source, ready for the immutable landing."""

    source_name: str
    tier: int
    source_url: str
    native_identifier: str | None
    content: bytes
    content_hash: str
    retrieved_at: datetime


class SourceAdapter(ABC):
    """Fetches raw records from a single official source family."""

    name: str
    tier: int
    base_url: str
    license: str | None = None

    @abstractmethod
    def fetch(self, native_identifier: str, **params: Any) -> RawFetch:  # pragma: no cover
        ...


# ---------------------------------------------------------------------------
# Baseline / counterfactual methods
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TimePoint:
    period: str  # ISO date / period code
    value: Decimal


@dataclass(frozen=True)
class BaselineContext:
    eu_id: int
    metric_code: str
    event_period: str  # when the action took effect
    lag_window_months: int
    pre_series: list[TimePoint]
    post_series: list[TimePoint]
    # Optional comparators for adjusted methods (e.g. national index for de-trending).
    national_series: list[TimePoint] = field(default_factory=list)
    # Control/donor units for comparison designs (DiD, synthetic control):
    # unit_id -> full series spanning pre AND post periods. Empty for federal
    # single-series cases (which therefore only support pre/post baselines).
    donors: dict[str, list[TimePoint]] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BaselineEstimate:
    method: str
    baseline_value: Decimal  # counterfactual value of the metric at the evaluation point
    ci_low: Decimal | None
    ci_high: Decimal | None
    spec: dict[str, Any]


class BaselineMethod(ABC):
    """Estimates the counterfactual (what the metric would have been absent the action)."""

    name: str

    @abstractmethod
    def eligible(self, ctx: BaselineContext) -> bool:  # pragma: no cover
        ...

    @abstractmethod
    def estimate(self, ctx: BaselineContext) -> BaselineEstimate:  # pragma: no cover
        ...


# ---------------------------------------------------------------------------
# Attribution channels
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AttributionContext:
    eu_id: int
    action_type: str
    sponsor_official_id: int | None
    signer_official_id: int | None
    vote_margin: int | None  # House winning margin in votes (post-decisive)
    member_on_winning_side: bool | None
    decisive_official_ids: list[int] = field(default_factory=list)  # House winning-side members
    # Senate final-passage roll-call (a law must clear BOTH chambers); each chamber's
    # pivotality is scaled by its OWN margin.
    senate_vote_margin: int | None = None
    senate_decisive_official_ids: list[int] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AttributionContribution:
    official_id: int | None
    role: str
    authority: Decimal
    pivotality: Decimal
    # Pre-normalization raw weight + an uncertainty band on it (0..1).
    raw_weight: Decimal
    raw_low: Decimal
    raw_high: Decimal


class AttributionChannel(ABC):
    """Produces candidate attribution contributions for an action."""

    name: str

    @abstractmethod
    def contributions(self, ctx: AttributionContext) -> list[AttributionContribution]:  # pragma: no cover
        ...


# ---------------------------------------------------------------------------
# Simple registries (kept tiny on purpose; no plugin framework over-engineering)
# ---------------------------------------------------------------------------
class Registry:
    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._items: dict[str, Any] = {}

    def register(self, obj: Any) -> Any:
        self._items[obj.name] = obj
        return obj

    def get(self, name: str) -> Any:
        return self._items[name]

    def all(self) -> list[Any]:
        return list(self._items.values())


SOURCE_ADAPTERS = Registry("source_adapter")
BASELINE_METHODS = Registry("baseline_method")
ATTRIBUTION_CHANNELS = Registry("attribution_channel")
