"""Target-relative outcome (PLAN.md §5 'stated objective with a quantitative target').

A different, complementary question from baseline-relative scoring:
  baseline-relative: "did the metric move vs. what would've happened anyway?" (causal effect)
  target-relative:   "did the policy DELIVER its own promised number?"   (promise-keeping)

There is no counterfactual model here — the realized value (official data) is compared
to the policy's pre-registered, source-linked target. Integrity guardrail (encoded in
confidence): this earns high identification ONLY when the realized series is *directly
attributable* to the action (e.g. a law's own DEFC-tagged spending). Economy-wide
realized series stay confounded -> low confidence -> "insufficient evidence".
"""

from __future__ import annotations

from dataclasses import dataclass

from degreezeor.core.hashing import hash_payload
from degreezeor.core.interfaces import BaselineEstimate
from degreezeor.core.numeric import D, clamp_score, q_money, q_score
from degreezeor.scoring.outcome import OutcomeComputation

# Relative tolerance: a gap smaller than this share of the target is "on target"
# (not a distinguishable miss/overshoot).
DEFAULT_TOLERANCE = D("0.02")


@dataclass(frozen=True)
class TargetComputation:
    outcome: OutcomeComputation
    s_outcome: object  # 0..100 achievement of the target
    best_method: str  # declared_target_direct | declared_target_confounded
    achievement_ratio: object


def compute_target_outcome(
    *,
    realized: object,
    target: object,
    sign_goal: int,
    directly_attributable: bool,
    eval_period: str,
    tolerance: object = DEFAULT_TOLERANCE,
) -> TargetComputation:
    # Quantize the realized/target amounts to cents at entry. This is the reproducibility
    # keystone for money: a NUMERIC value round-trips through a float in some stores (e.g.
    # SQLite REAL), and at trillion-scale a double cannot hold sub-cent precision — so a
    # score-time float and a re-fetched-from-DB Decimal can differ at the 4th decimal and
    # break the bit-reproducible hash. Cents is coarser than that float noise (and lossless
    # for the bounded 0..100 indices this also scores), so the fingerprint is stable.
    realized_d, target_d = q_money(D(realized)), q_money(D(target))
    if target_d == 0:
        raise ValueError("target must be non-zero")
    delta = realized_d - target_d
    achievement = realized_d / target_d
    tol_abs = abs(target_d) * D(tolerance)

    # Achievement -> 0..100.
    #   sign_goal >= 0: "deliver at least the target" — meeting/exceeding = 100.
    #   sign_goal <  0: "stay within the target (e.g. cost)" — overrun is penalized.
    if sign_goal >= 0:
        s_outcome = clamp_score(D(100) * (achievement if achievement < 1 else D(1)))
    else:
        ratio = (target_d / realized_d) if realized_d > 0 else D(0)
        s_outcome = clamp_score(D(100) * (ratio if ratio < 1 else D(1)))

    # "Significant" = the gap exceeds tolerance (a real miss/overshoot, not rounding).
    ci_low, ci_high = delta - tol_abs, delta + tol_abs
    # A display-only standardized gap (gap in tolerance units).
    z = delta / tol_abs if tol_abs != 0 else D(0)

    method = "declared_target_direct" if directly_attributable else "declared_target_confounded"
    estimate = BaselineEstimate(
        method=method, baseline_value=q_score(target_d),
        ci_low=q_score(target_d), ci_high=q_score(target_d),
        spec={"kind": "declared_target", "directly_attributable": directly_attributable,
              "tolerance": float(tolerance)},
    )
    input_hash = hash_payload({
        # Quantize so the fingerprint is identical whether the target came from a float
        # spec or a DB NUMERIC (str(Decimal) forms differ otherwise) => reproducible re-runs.
        "mode": "target", "realized": str(q_score(realized_d)), "target": str(q_score(target_d)),
        "sign_goal": sign_goal, "directly_attributable": directly_attributable,
    })
    comp = OutcomeComputation(
        observed=q_score(realized_d), baseline_pooled=q_score(target_d), delta=q_score(delta),
        z=q_score(z), model_dependence=D(0), ci_low=q_score(ci_low), ci_high=q_score(ci_high),
        per_method=[estimate], eval_period=eval_period, input_hash=input_hash,
    )
    return TargetComputation(
        outcome=comp, s_outcome=q_score(s_outcome), best_method=method,
        achievement_ratio=q_score(achievement),
    )
