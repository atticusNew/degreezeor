"""Confidence (PLAN.md §8.3) — the gate that prevents false precision.

C = c_design * c_data * c_attrib * c_modeldep, each in [0,1]. When C falls below
the publish threshold the composite is suppressed and the EU is rendered
"Insufficient evidence" — never a low score. This is what makes the ARRA-style
case (real effect indistinguishable from noise under a naive baseline) resolve to
honest abstention instead of a partisan-looking verdict.
"""

from __future__ import annotations

from dataclasses import dataclass

from degreezeor.core.numeric import D, clamp01, dprod

# Identification strength of the available design. Pre-trend projection on a single
# federal series cannot separate the policy from concurrent macro shocks, so its
# ceiling is deliberately modest. Stronger designs (DiD, synthetic control) raise this.
DESIGN_BASE = {
    "pretrend_projection": D("0.50"),
    "flat_last_value": D("0.30"),
}


@dataclass(frozen=True)
class ConfidenceBreakdown:
    c_design: object
    c_data: object
    c_attrib: object
    c_modeldep: object
    confidence: object


def _significance_factor(ci_low: object, ci_high: object) -> D:
    lo, hi = D(ci_low), D(ci_high)
    # Effect distinguishable from zero only if the CI excludes 0.
    if lo > 0 or hi < 0:
        return D("1.0")
    return D("0.40")


def compute_confidence(
    *,
    best_method: str,
    ci_low: object,
    ci_high: object,
    model_dependence: object,
    data_tier: int,
    data_completeness: object,
    attribution_widths: list[object],
) -> ConfidenceBreakdown:
    c_design = clamp01(DESIGN_BASE.get(best_method, D("0.4")) * _significance_factor(ci_low, ci_high))

    tier_factor = {0: D("0.95"), 1: D("0.95"), 2: D("0.85"), 3: D("0.60")}.get(data_tier, D("0.5"))
    c_data = clamp01(tier_factor * D(data_completeness))

    if attribution_widths:
        avg_width = sum((D(w) for w in attribution_widths), D(0)) / D(len(attribution_widths))
        c_attrib = clamp01(D(1) - avg_width)
    else:
        c_attrib = D("0.5")

    c_modeldep = clamp01(D(1) - D(model_dependence))

    confidence = clamp01(dprod([c_design, c_data, c_attrib, c_modeldep]))
    return ConfidenceBreakdown(
        c_design=c_design,
        c_data=c_data,
        c_attrib=c_attrib,
        c_modeldep=c_modeldep,
        confidence=confidence,
    )
