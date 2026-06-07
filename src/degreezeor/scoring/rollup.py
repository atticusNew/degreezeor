"""Official-level roll-up (PLAN.md §8.5 / §14).

Aggregates an official's scored actions into a single, attribution-weighted view —
but NEVER without **coverage** (what fraction of their attributable actions are
actually scoreable) and confidence. If none of an official's actions clear the
confidence gate, the roll-up composite is ``None`` ("insufficient evidence"),
not a low score. The composite is opt-in/value-laden, exactly like the EU composite.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from degreezeor.core.numeric import D, q_score


@dataclass(frozen=True)
class ActionContribution:
    eu_id: int
    attribution: Decimal  # this official's attribution share on the EU (0..1)
    composite: Decimal | None  # EU composite (None when the EU is gated/non-scoreable)
    confidence: Decimal | None
    gated: bool


@dataclass(frozen=True)
class OfficialRollup:
    total_actions: int  # distinct attributable EUs
    scored_actions: int  # EUs that cleared the gate (composite present)
    coverage: Decimal  # scored / total  (0..1)
    composite: Decimal | None  # attribution-weighted mean composite over scored EUs
    confidence: Decimal | None  # attribution-weighted mean confidence over scored EUs


def rollup(items: list[ActionContribution]) -> OfficialRollup:
    total = len(items)
    scored = [i for i in items if not i.gated and i.composite is not None]
    coverage = q_score(D(len(scored)) / D(total)) if total else q_score(D(0))

    if not scored:
        return OfficialRollup(
            total_actions=total, scored_actions=0, coverage=coverage,
            composite=None, confidence=None,
        )

    weight = sum((D(i.attribution) for i in scored), D(0))
    if weight <= 0:
        # Degenerate: scored actions exist but with zero attribution weight.
        return OfficialRollup(
            total_actions=total, scored_actions=len(scored), coverage=coverage,
            composite=None, confidence=None,
        )
    composite = sum((D(i.attribution) * D(i.composite) for i in scored), D(0)) / weight
    confidence = sum(
        (D(i.attribution) * D(i.confidence) for i in scored if i.confidence is not None), D(0)
    ) / weight
    return OfficialRollup(
        total_actions=total, scored_actions=len(scored), coverage=coverage,
        composite=q_score(composite), confidence=q_score(confidence),
    )
