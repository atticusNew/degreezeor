"""Sensitivity analysis (PLAN.md §9.10, §14).

Re-evaluates the outcome under alternative, equally-defensible lag windows and reports
how the result moves — so a score can be judged for ROBUSTNESS, not taken on faith.
A directionally stable, significant effect across horizons is strong; a sign that flips
with the window is a red flag (and should temper confidence). This is a core bias control:
it surfaces the dependence of the result on an analyst choice (the evaluation horizon).
"""

from __future__ import annotations

from dataclasses import dataclass

from degreezeor.core.numeric import D, q_score
from degreezeor.scoring.outcome import compute_outcome, s_outcome_from_z

# Default defensible horizons (months) to probe around the registered lag.
DEFAULT_LAGS = (12, 24, 36, 48, 60)


@dataclass(frozen=True)
class LagPoint:
    lag_months: int
    eval_period: str
    delta: object
    delta_toward_goal: object
    z: object
    s_outcome: object
    ci_low: object
    ci_high: object
    significant: bool  # CI on delta excludes zero
    is_registered: bool


@dataclass(frozen=True)
class Sensitivity:
    registered_lag: int
    points: list[LagPoint]
    sign_stable: bool  # every horizon agrees on the direction of the effect
    significant_fraction: object  # share of horizons with a distinguishable effect
    summary: str


def analyze_lag_sensitivity(
    observations: list[tuple[str, object]],
    *,
    event_period: str,
    registered_lag: int,
    sign_goal: int,
    seed: int,
    donor_observations: dict[str, list[tuple[str, object]]] | None = None,
    lags: tuple[int, ...] = DEFAULT_LAGS,
) -> Sensitivity:
    candidate = sorted(set(lags) | {registered_lag})
    points: list[LagPoint] = []
    for lag in candidate:
        comp = compute_outcome(
            observations, event_period=event_period, lag_window_months=lag,
            sign_goal=sign_goal, seed=seed, donor_observations=donor_observations,
        )
        if comp is None:
            continue
        toward = D(sign_goal) * D(comp.delta)
        significant = float(comp.ci_low) > 0 or float(comp.ci_high) < 0
        points.append(LagPoint(
            lag_months=lag, eval_period=comp.eval_period,
            delta=q_score(comp.delta), delta_toward_goal=q_score(toward),
            z=q_score(comp.z), s_outcome=s_outcome_from_z(comp.z),
            ci_low=q_score(comp.ci_low), ci_high=q_score(comp.ci_high),
            significant=significant, is_registered=(lag == registered_lag),
        ))

    toward_signs = {1 if float(p.delta_toward_goal) > 0 else (-1 if float(p.delta_toward_goal) < 0 else 0)
                    for p in points}
    nonzero = {s for s in toward_signs if s != 0}
    sign_stable = len(nonzero) <= 1
    sig_frac = q_score(D(sum(1 for p in points if p.significant)) / D(len(points))) if points else q_score(D(0))

    if not points:
        summary = "No alternative horizons could be evaluated with available data."
    elif sign_stable and float(sig_frac) >= D("0.5"):
        direction = "toward" if next(iter(nonzero), 0) > 0 else "away from"
        summary = (f"Directionally robust: the effect points {direction} the stated goal across all "
                   f"{len(points)} evaluated horizons, and is statistically distinguishable in "
                   f"{int(float(sig_frac) * len(points))} of them.")
    elif sign_stable:
        summary = ("Direction is stable across horizons, but the effect is rarely distinguishable "
                   "from noise — treat magnitude with caution.")
    else:
        summary = ("NOT robust: the direction of the effect flips depending on the evaluation horizon. "
                   "This model/horizon dependence should temper confidence in any single number.")

    return Sensitivity(
        registered_lag=registered_lag, points=points,
        sign_stable=sign_stable, significant_fraction=sig_frac, summary=summary,
    )
