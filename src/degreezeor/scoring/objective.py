"""Objective extraction and party-masked objective->metric mapping (PLAN.md §5).

The matcher sees ONLY the objective text (domain optional) — never party or
official name. Inputs are additionally run through :func:`mask_party_and_name`
so that even if an objective string embedded a sponsor's party label, it cannot
influence metric selection. This is a core bias control.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from degreezeor.core.models import DataSource, Metric
from degreezeor.core.numeric import D, clamp01, q_score
from degreezeor.scoring.catalog import CATALOG, MetricSpec

# e.g. "[D-WI-7]", " D-WI", honorifics, bare party letters in brackets.
_PARTY_BRACKET = re.compile(r"\[[DRILG]-[A-Z]{2}(?:-\d+)?\]")
_HONORIFIC = re.compile(r"\b(Rep|Sen|Gov|Pres|President|Senator|Representative)\.?\b", re.IGNORECASE)
_PARTY_WORD = re.compile(r"\b(Democrat(?:ic)?|Republican|GOP|Independent)\b", re.IGNORECASE)


def mask_party_and_name(text: str) -> str:
    text = _PARTY_BRACKET.sub("[MASKED]", text)
    text = _HONORIFIC.sub("", text)
    text = _PARTY_WORD.sub("[MASKED]", text)
    return text


@dataclass(frozen=True)
class MetricMatch:
    spec: MetricSpec
    alignment: object  # Decimal 0..1
    matched_keywords: tuple[str, ...]


def score_alignment(objective_text: str, spec: MetricSpec) -> tuple[object, tuple[str, ...]]:
    """Fidelity of metric to objective: fraction-weighted keyword evidence (0..1)."""
    masked = mask_party_and_name(objective_text).lower()
    hits = tuple(kw for kw in spec.keywords if kw in masked)
    if not hits:
        return D(0), ()
    # Diminishing returns: 1 hit -> ~0.6, 2 -> ~0.8, 3+ -> ~0.9+. Deterministic.
    score = D(1) - (D("0.4") ** D(len(hits)))
    return clamp01(score), hits


def select_metrics(
    objective_text: str, domain: str | None = None
) -> tuple[MetricMatch | None, list[MetricMatch]]:
    """Return (primary, side_effects). The catalog is cross-domain: a metric is selected
    by KEYWORD evidence against the (party-masked) objective, regardless of the action's
    crude domain tag — so e.g. a health objective reaches a CDC health metric even on an
    action defaulted to the economic domain. ``domain`` is used only as a deterministic
    tie-breaker (prefer the action's own domain when alignment ties), never as an exclusion.
    Health/economic keyword sets are specific enough that cross-domain false matches do not
    occur in practice. Primary = highest alignment; ties -> same-domain -> catalog order."""
    matches: list[MetricMatch] = []
    for spec in CATALOG:
        align, hits = score_alignment(objective_text, spec)
        if hits:
            matches.append(MetricMatch(spec=spec, alignment=align, matched_keywords=hits))
    if not matches:
        return None, []
    matches.sort(
        key=lambda m: (D(m.alignment),
                       1 if (domain and m.spec.domain == domain) else 0,
                       -CATALOG.index(m.spec)),
        reverse=True,
    )
    primary = matches[0]
    side = matches[1:]
    return primary, side


def ensure_metric(session: Session, spec: MetricSpec) -> Metric:
    m = session.execute(select(Metric).where(Metric.code == spec.code)).scalar_one_or_none()
    if m is not None:
        return m
    src = session.execute(
        select(DataSource).where(DataSource.name == spec.source_name)
    ).scalar_one_or_none()
    if src is None:
        # Source is normally created during ingestion; create a correct placeholder if the
        # catalog is seeded first (base_url per known source so the trail stays accurate).
        base_urls = {"BLS": "https://api.bls.gov", "CDC": "https://data.cdc.gov/resource"}
        src = DataSource(name=spec.source_name, tier=1,
                         base_url=base_urls.get(spec.source_name, "https://example.gov"))
        session.add(src)
        session.flush()
    m = Metric(
        code=spec.code,
        name=spec.name,
        unit=spec.unit,
        direction_good=spec.direction_good,
        source_id=src.id,
        native_series_id=spec.native_series_id,
        domain=spec.domain,
    )
    session.add(m)
    session.flush()
    return m


def sign_goal_for(spec: MetricSpec) -> int:
    """+1 if a *rise* in the metric moves toward the stated goal, -1 if a *fall* does."""
    return 1 if spec.direction_good == "up" else -1


def q(x: object) -> object:
    return q_score(x)
