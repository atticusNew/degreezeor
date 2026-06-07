"""Lag-window sensitivity analysis: robustness detection across horizons."""

from __future__ import annotations

from degreezeor.scoring.sensitivity import analyze_lag_sensitivity


def _series_with_persistent_drop():
    # 36 pre-months flat at 6.0, then a sustained drop to ~4.0 for 60 post-months.
    obs = []
    for t in range(36):
        y, m = 2012 + t // 12, t % 12 + 1
        obs.append((f"{y}-{m:02d}-01", "6.0"))
    for t in range(60):
        y, m = 2015 + t // 12, t % 12 + 1
        obs.append((f"{y}-{m:02d}-01", "4.0"))
    return obs


def test_persistent_effect_is_directionally_robust() -> None:
    obs = _series_with_persistent_drop()
    # Goal = reduce the metric (sign_goal=-1); a sustained drop is robustly toward-goal.
    s = analyze_lag_sensitivity(obs, event_period="2015-01-01", registered_lag=24,
                                sign_goal=-1, seed=1, lags=(12, 24, 36, 48))
    assert s.sign_stable is True
    assert all(float(p.delta_toward_goal) > 0 for p in s.points)
    assert "robust" in s.summary.lower()
    assert any(p.is_registered for p in s.points)


def test_registered_lag_always_included() -> None:
    obs = _series_with_persistent_drop()
    s = analyze_lag_sensitivity(obs, event_period="2015-01-01", registered_lag=30,
                                sign_goal=-1, seed=1, lags=(12, 24))
    assert 30 in [p.lag_months for p in s.points]


def test_sign_flip_is_flagged_not_robust() -> None:
    # Flat pre-period at 5.0; event 2015-01. Metric is ABOVE baseline at the 12-month
    # horizon (2016-01) but BELOW it at 24/36 months (2017-01, 2018-01) => the effect's
    # direction flips with the evaluation window => not robust.
    obs = []
    for y in (2012, 2013, 2014):
        obs += [(f"{y}-{m:02d}-01", "5.0") for m in range(1, 13)]
    for y in (2015, 2016):
        obs += [(f"{y}-{m:02d}-01", "7.0") for m in range(1, 13)]  # up through 2016
    for y in (2017, 2018, 2019):
        obs += [(f"{y}-{m:02d}-01", "3.0") for m in range(1, 13)]  # down from 2017
    s = analyze_lag_sensitivity(obs, event_period="2015-01-01", registered_lag=12,
                                sign_goal=1, seed=1, lags=(12, 24, 36))
    assert s.sign_stable is False
    assert "not robust" in s.summary.lower()
