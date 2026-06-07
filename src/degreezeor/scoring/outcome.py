"""Outcome computation (PLAN.md §6/§8.2).

Pure, deterministic given inputs + seed. Computes the baseline ensemble, the
signed outcome delta (toward the action's OWN stated objective), a standardized
effect ``z``, a bootstrap CI, and a model-dependence score (sign disagreement /
spread across baseline methods). No DB access here -> trivially unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np

from degreezeor.core.hashing import hash_payload
from degreezeor.core.interfaces import (
    BASELINE_METHODS,
    BaselineContext,
    BaselineEstimate,
    TimePoint,
)
from degreezeor.core.numeric import D, clamp01, q_score
from degreezeor.scoring.baseline import _eval_index, _month_index, split_series

# Comparison designs that address confounding (used preferentially over naive
# single-series baselines when eligible).
_STRONG_METHODS = {"synthetic_control", "difference_in_differences"}


@dataclass(frozen=True)
class OutcomeComputation:
    observed: object
    baseline_pooled: object
    delta: object
    z: object
    model_dependence: object  # 0..1 (1 = methods disagree on sign)
    ci_low: object
    ci_high: object
    per_method: list[BaselineEstimate]
    eval_period: str
    # Deterministic fingerprint of the exact numeric inputs (treated + donor series)
    # that produced this result. Drives the reproducible data-snapshot id, independent
    # of any volatile provenance bytes (e.g. dynamic HTML on a source page).
    input_hash: str


def _value_at(post: list, eval_index: int, origin: date) -> tuple[str, float] | None:
    """Observation nearest to the evaluation index (event + lag)."""
    best = None
    best_dist = None
    for tp in post:
        idx = _month_index(tp.period, origin)
        dist = abs(idx - eval_index)
        if best_dist is None or dist < best_dist:
            best_dist, best = dist, tp
    return (best.period, float(best.value)) if best is not None else None


def compute_outcome(
    observations: list[tuple[str, object]],
    *,
    event_period: str,
    lag_window_months: int,
    sign_goal: int,
    seed: int,
    donor_observations: dict[str, list[tuple[str, object]]] | None = None,
) -> OutcomeComputation | None:
    pre, post = split_series(observations, event_period)
    if len(pre) < 6 or not post:
        return None

    # Fingerprint the exact numeric inputs for reproducible, audit-complete snapshots.
    input_hash = hash_payload({
        "event_period": event_period,
        "lag_window_months": lag_window_months,
        "sign_goal": sign_goal,
        "observations": [(p, str(v)) for p, v in sorted(observations)],
        "donors": {
            unit: [(p, str(v)) for p, v in sorted(series)]
            for unit, series in sorted((donor_observations or {}).items())
        },
    })

    origin = date.fromisoformat(pre[0].period)
    donors: dict[str, list[TimePoint]] = {}
    for unit, series in (donor_observations or {}).items():
        donors[unit] = [TimePoint(period=p, value=D(v)) for p, v in sorted(series)]
    ctx = BaselineContext(
        eu_id=0,
        metric_code="",
        event_period=event_period,
        lag_window_months=lag_window_months,
        pre_series=pre,
        post_series=post,
        donors=donors,
    )
    eval_idx = _eval_index(ctx)
    at = _value_at(post, eval_idx, origin)
    if at is None:
        return None
    eval_period, observed = at

    estimates = [m.estimate(ctx) for m in BASELINE_METHODS.all() if m.eligible(ctx)]
    if not estimates:
        return None

    # Tiered pooling: if a comparison design (DiD / synthetic control) is available,
    # it drives the estimate — naive single-series baselines are reported for
    # transparency but must NOT dilute a well-identified counterfactual. Naive
    # baselines pool together only when no comparison design is eligible.
    strong = [e for e in estimates if e.method in _STRONG_METHODS]
    active = strong if strong else estimates

    baselines = np.array([float(e.baseline_value) for e in active])
    pooled = float(np.mean(baselines))
    deltas = observed - baselines  # per active method
    delta = observed - pooled

    # Model dependence: sign disagreement dominates; otherwise normalized spread.
    signs = {int(np.sign(d)) for d in deltas if abs(d) > 1e-9}
    if len({s for s in signs if s != 0}) > 1:
        model_dep = 1.0
    else:
        spread = float(np.max(baselines) - np.min(baselines))
        denom = abs(delta) + 1e-9
        model_dep = min(1.0, spread / denom) if denom > 0 else 0.0

    # Noise scale = pre-period detrended residual std (fallback: pre std).
    xs = np.array([_month_index(p.period, origin) for p in pre], dtype=float)
    ys = np.array([float(p.value) for p in pre], dtype=float)
    b, a = np.polyfit(xs, ys, 1)
    resid = ys - (a + b * xs)
    scale = float(np.std(resid)) or (float(np.std(ys)) or 1.0)

    delta_toward_goal = sign_goal * delta
    z = delta_toward_goal / scale

    # Deterministic bootstrap CI on the delta via resampled pre-residuals applied to
    # the pooled baseline projection.
    rng = np.random.default_rng(seed)
    n_boot = 2000
    boot_deltas = observed - (pooled + rng.choice(resid, size=n_boot, replace=True))
    ci_low = float(np.percentile(boot_deltas, 2.5))
    ci_high = float(np.percentile(boot_deltas, 97.5))

    return OutcomeComputation(
        observed=D(observed),
        baseline_pooled=D(pooled),
        delta=D(delta),
        z=D(z),
        model_dependence=clamp01(model_dep),
        ci_low=D(min(ci_low, ci_high)),
        ci_high=D(max(ci_low, ci_high)),
        per_method=estimates,
        eval_period=eval_period,
        input_hash=input_hash,
    )


def s_outcome_from_z(z: object) -> object:
    """Map standardized effect z -> 0..100 via the logistic CDF. 50 = no effect."""
    import math

    # Clamp the standardized effect to a numerically safe range (the logistic is
    # already saturated well before this), avoiding exp() overflow for huge effects.
    zf = max(-40.0, min(40.0, float(D(z))))
    val = 100.0 / (1.0 + math.exp(-1.702 * zf))  # logistic approx to normal CDF
    return q_score(val)
