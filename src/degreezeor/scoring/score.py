"""Score assembly + reproducible run pinning (PLAN.md §8).

Default public artifact = the decomposed factual component vector + confidence.
The single composite is OPT-IN and value-laden; the neutral default weights cover
ONLY the factual components and are equal-weighted, gated by confidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from degreezeor.config import settings
from degreezeor.core.numeric import D, clamp01, clamp_score, q_score

# All factual components shown in the default vector (transparency).
FACTUAL_VECTOR = ("outcome", "evidence", "attribution", "alignment", "dataquality", "durability")
# Achievement axis: components that measure achievement of the stated goal (higher =
# better, directional). The neutral composite averages ONLY these and then scales by
# confidence C. The other components (evidence, dataquality, attribution-width) are
# already inside C, so averaging them too would DOUBLE-COUNT confidence; alignment is
# a metric-fidelity quality measure shown for context. (Methodology v0.2.0.)
ACHIEVEMENT_COMPONENTS = ("outcome", "durability")
# Value-laden components: OFF by default, surfaced separately, user-weighted only.
VALUE_LADEN = ("cost", "distribution")


@dataclass(frozen=True)
class Component:
    name: str
    value: object  # 0..100 (None if not computed)
    ci_low: object | None = None
    ci_high: object | None = None
    is_value_laden: bool = False


@dataclass(frozen=True)
class AssembledScore:
    components: list[Component]
    confidence: object
    composite: object | None  # None when gated (insufficient evidence)
    gated: bool
    reason: str | None = field(default=None)


def assemble_score(
    *,
    s_outcome: object,
    s_evidence: object,
    s_attribution: object,
    s_alignment: object,
    s_dataquality: object,
    s_durability: object | None,
    confidence: object,
) -> AssembledScore:
    # Outcome uncertainty (the bootstrap CI on the delta) is reported in metric units
    # via OutcomeResult/ConfidenceInterval, not as a 0..100 component CI.
    comps = [
        Component("outcome", clamp_score(s_outcome)),
        Component("evidence", clamp_score(s_evidence)),
        Component("attribution", clamp_score(s_attribution)),
        Component("alignment", clamp_score(s_alignment)),
        Component("dataquality", clamp_score(s_dataquality)),
    ]
    if s_durability is not None:
        comps.append(Component("durability", clamp_score(s_durability)))

    conf = clamp01(confidence)
    gated = conf < settings.confidence_publish_threshold
    if gated:
        return AssembledScore(
            components=comps,
            confidence=conf,
            composite=None,
            gated=True,
            reason="insufficient_evidence",
        )

    # Neutral-default composite = confidence-scaled achievement of the stated goal.
    present = [c for c in comps if c.name in ACHIEVEMENT_COMPONENTS and c.value is not None]
    equal_w = D(1) / D(len(present))
    achievement = sum((D(c.value) * equal_w for c in present), D(0))
    composite = q_score(conf * achievement)
    return AssembledScore(components=comps, confidence=conf, composite=composite, gated=False)
