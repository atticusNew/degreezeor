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
from scipy.optimize import nnls

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


def _pre_value_vector(series: list[TimePoint], periods: list[str]) -> np.ndarray | None:
    """Donor values aligned to the treated unit's pre-period dates (exact match)."""
    lookup = {tp.period: float(tp.value) for tp in series}
    if not all(p in lookup for p in periods):
        return None
    return np.array([lookup[p] for p in periods], dtype=float)


def _donor_value_at(series: list[TimePoint], origin: date, eval_idx: int) -> float | None:
    best, best_dist = None, None
    for tp in series:
        idx = _month_index(tp.period, origin)
        dist = abs(idx - eval_idx)
        if best_dist is None or dist < best_dist:
            best_dist, best = dist, float(tp.value)
    return best


def _pre_slope(values: np.ndarray) -> float:
    xs = np.arange(len(values), dtype=float)
    b, _a = np.polyfit(xs, values, 1)
    return float(b)


class DifferenceInDifferences(BaselineMethod):
    """Counterfactual = treated pre-level + the control group's pre→post change.

    Identifies the effect by *differencing out* shocks common to treated and control
    units (the parallel-trends assumption), which a single-series pre/post cannot do.
    """

    name = "difference_in_differences"

    def _aligned(self, ctx: BaselineContext):
        pre_periods = [p.period for p in ctx.pre_series]
        treated_pre = np.array([float(p.value) for p in ctx.pre_series], dtype=float)
        origin = date.fromisoformat(pre_periods[0])
        eval_idx = _eval_index(ctx)
        donor_pre, donor_eval = [], []
        for series in ctx.donors.values():
            vp = _pre_value_vector(series, pre_periods)
            ve = _donor_value_at(series, origin, eval_idx)
            if vp is not None and ve is not None:
                donor_pre.append(vp)
                donor_eval.append(ve)
        return treated_pre, donor_pre, donor_eval, origin, eval_idx

    def eligible(self, ctx: BaselineContext) -> bool:
        if len(ctx.pre_series) < 6 or not ctx.donors:
            return False
        treated_pre, donor_pre, donor_eval, _o, _e = self._aligned(ctx)
        if not donor_pre:
            return False
        # Parallel pre-trends: treated and average-control slopes must be similar.
        control_pre = np.mean(np.vstack(donor_pre), axis=0)
        ts, cs = _pre_slope(treated_pre), _pre_slope(control_pre)
        scale = abs(ts) + abs(cs) + 1e-9
        return abs(ts - cs) / scale <= 0.5

    def estimate(self, ctx: BaselineContext) -> BaselineEstimate:
        treated_pre, donor_pre, donor_eval, _o, eval_idx = self._aligned(ctx)
        treated_pre_mean = float(np.mean(treated_pre))
        donor_pre_means = np.array([float(np.mean(dp)) for dp in donor_pre])
        donor_eval_arr = np.array(donor_eval, dtype=float)
        # Each donor implies a counterfactual; average them, band from their spread.
        implied = treated_pre_mean + (donor_eval_arr - donor_pre_means)
        cf = float(np.mean(implied))
        sd = float(np.std(implied)) if len(implied) > 1 else 0.0
        return BaselineEstimate(
            method=self.name,
            baseline_value=D(cf),
            ci_low=D(cf - 1.96 * sd),
            ci_high=D(cf + 1.96 * sd),
            spec={"n_donors": len(donor_eval), "control_change": round(float(np.mean(donor_eval_arr - donor_pre_means)), 4)},
        )


class SyntheticControl(BaselineMethod):
    """Counterfactual = a nonneg, sum-to-one weighted blend of donors that best
    matches the treated unit's PRE-period path (Abadie-style). Strong identification
    when the pre-fit is tight."""

    name = "synthetic_control"

    def _solve_weights(self, treated_pre: np.ndarray, donor_pre_matrix: np.ndarray):
        # Solve the constrained synthetic-control problem: w >= 0 AND sum(w) = 1,
        # JOINTLY (not nnls-then-normalize, which destroys the fit). The sum-to-one
        # constraint is enforced by augmenting the system with a heavily weighted
        # row of ones (penalty K), then running nonnegative least squares.
        n_pre = donor_pre_matrix.shape[0]
        k = 1e6 * (float(np.mean(np.abs(treated_pre))) + 1.0)
        a_aug = np.vstack([donor_pre_matrix, k * np.ones((1, donor_pre_matrix.shape[1]))])
        b_aug = np.concatenate([treated_pre, [k]])
        w, _resid = nnls(a_aug, b_aug)
        total = w.sum()
        w = np.ones(donor_pre_matrix.shape[1]) / donor_pre_matrix.shape[1] if total <= 0 else w / total
        _ = n_pre
        return w

    def _matrix(self, ctx: BaselineContext):
        pre_periods = [p.period for p in ctx.pre_series]
        treated_pre = np.array([float(p.value) for p in ctx.pre_series], dtype=float)
        origin = date.fromisoformat(pre_periods[0])
        eval_idx = _eval_index(ctx)
        cols, evals = [], []
        for series in ctx.donors.values():
            vp = _pre_value_vector(series, pre_periods)
            ve = _donor_value_at(series, origin, eval_idx)
            if vp is not None and ve is not None:
                cols.append(vp)
                evals.append(ve)
        if not cols:
            return treated_pre, None, None
        return treated_pre, np.column_stack(cols), np.array(evals, dtype=float)

    def eligible(self, ctx: BaselineContext) -> bool:
        if len(ctx.pre_series) < 6 or len(ctx.donors) < 2:
            return False
        treated_pre, dmat, _ev = self._matrix(ctx)
        if dmat is None or dmat.shape[1] < 2:
            return False
        w = self._solve_weights(treated_pre, dmat)
        synth_pre = dmat @ w
        rmspe = float(np.sqrt(np.mean((treated_pre - synth_pre) ** 2)))
        pre_sd = float(np.std(treated_pre)) or 1.0
        # Accept when the synthetic unit tracks the treated unit within ~one pre-period
        # std (a good, but not unrealistically perfect, counterfactual fit).
        return rmspe <= pre_sd

    def estimate(self, ctx: BaselineContext) -> BaselineEstimate:
        treated_pre, dmat, evals = self._matrix(ctx)
        w = self._solve_weights(treated_pre, dmat)
        cf = float(evals @ w)
        synth_pre = dmat @ w
        rmspe = float(np.sqrt(np.mean((treated_pre - synth_pre) ** 2)))
        return BaselineEstimate(
            method=self.name,
            baseline_value=D(cf),
            ci_low=D(cf - 1.96 * rmspe),
            ci_high=D(cf + 1.96 * rmspe),
            spec={
                "n_donors": int(dmat.shape[1]),
                "pre_fit_rmspe": round(rmspe, 4),
                "top_weights": sorted((round(float(x), 3) for x in w), reverse=True)[:3],
            },
        )


pretrend = BASELINE_METHODS.register(PretrendProjection())
flat = BASELINE_METHODS.register(FlatLastValue())
did = BASELINE_METHODS.register(DifferenceInDifferences())
synthetic_control = BASELINE_METHODS.register(SyntheticControl())


def split_series(
    observations: list[tuple[str, object]], event_period: str
) -> tuple[list[TimePoint], list[TimePoint]]:
    """Split (period_iso, value) observations into pre/post relative to event_period."""
    pre, post = [], []
    for period, value in sorted(observations):
        tp = TimePoint(period=period, value=D(value))
        (pre if period < event_period else post).append(tp)
    return pre, post
