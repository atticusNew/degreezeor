"""Baseline / counterfactual methods (PLAN.md §6).

The MVP slice ships two methods so the *ensemble* and *model-dependence* signals
are real from day one:

* ``pretrend_projection`` — fit the pre-period linear trend and project it to the
  evaluation point. This is the formal answer to "don't credit inherited trends":
  the pre-existing trajectory IS the counterfactual.
* ``flat_last_value`` — a naive no-change counterfactual (last pre-period value).

DiD and synthetic control plug in later behind the same :class:`BaselineMethod`
interface (they need sub-national / donor-pool data, which is Phase 2 scope).
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np

from degreezeor.core.interfaces import (
    BASELINE_METHODS,
    BaselineContext,
    BaselineEstimate,
    BaselineMethod,
    TimePoint,
)
from degreezeor.core.numeric import D


def _month_index(period_iso: str, origin: date) -> int:
    y, m, _ = (int(x) for x in period_iso.split("-"))
    return (y - origin.year) * 12 + (m - origin.month)


def _eval_index(ctx: BaselineContext) -> int:
    origin = date.fromisoformat(ctx.pre_series[0].period)
    event_idx = _month_index(ctx.event_period, origin)
    return event_idx + ctx.lag_window_months


class PretrendProjection(BaselineMethod):
    name = "pretrend_projection"

    def eligible(self, ctx: BaselineContext) -> bool:
        return len(ctx.pre_series) >= 6  # need enough pre-points to fit a trend

    def estimate(self, ctx: BaselineContext) -> BaselineEstimate:
        origin = date.fromisoformat(ctx.pre_series[0].period)
        xs = np.array([_month_index(p.period, origin) for p in ctx.pre_series], dtype=float)
        ys = np.array([float(p.value) for p in ctx.pre_series], dtype=float)
        n = len(xs)
        # OLS slope/intercept.
        b, a = np.polyfit(xs, ys, 1)
        x_eval = float(_eval_index(ctx))
        y_hat = a + b * x_eval
        # Prediction interval (t approx ~ 2 for typical n; use 1.96 for simplicity & determinism).
        resid = ys - (a + b * xs)
        dof = max(n - 2, 1)
        s = math.sqrt(float(np.sum(resid**2)) / dof)
        x_mean = float(np.mean(xs))
        sxx = float(np.sum((xs - x_mean) ** 2)) or 1.0
        se_pred = s * math.sqrt(1.0 + 1.0 / n + (x_eval - x_mean) ** 2 / sxx)
        z = 1.96
        return BaselineEstimate(
            method=self.name,
            baseline_value=D(y_hat),
            ci_low=D(y_hat - z * se_pred),
            ci_high=D(y_hat + z * se_pred),
            spec={
                "slope_per_month": round(float(b), 6),
                "intercept": round(float(a), 6),
                "n_pre": n,
                "eval_index": x_eval,
                "resid_std": round(s, 6),
            },
        )


class FlatLastValue(BaselineMethod):
    name = "flat_last_value"

    def eligible(self, ctx: BaselineContext) -> bool:
        return len(ctx.pre_series) >= 1

    def estimate(self, ctx: BaselineContext) -> BaselineEstimate:
        last = ctx.pre_series[-1]
        # Crude band from pre-period dispersion.
        ys = np.array([float(p.value) for p in ctx.pre_series], dtype=float)
        sd = float(np.std(ys)) if len(ys) > 1 else 0.0
        return BaselineEstimate(
            method=self.name,
            baseline_value=D(last.value),
            ci_low=D(float(last.value) - 1.96 * sd),
            ci_high=D(float(last.value) + 1.96 * sd),
            spec={"anchor_period": last.period, "pre_sd": round(sd, 6)},
        )


pretrend = BASELINE_METHODS.register(PretrendProjection())
flat = BASELINE_METHODS.register(FlatLastValue())


def split_series(
    observations: list[tuple[str, object]], event_period: str
) -> tuple[list[TimePoint], list[TimePoint]]:
    """Split (period_iso, value) observations into pre/post relative to event_period."""
    pre, post = [], []
    for period, value in sorted(observations):
        tp = TimePoint(period=period, value=D(value))
        (pre if period < event_period else post).append(tp)
    return pre, post
