"""Outcome computation: determinism, sign handling, and CI behavior."""

from __future__ import annotations

from degreezeor.scoring.outcome import compute_outcome, s_outcome_from_z


def _flat_then_drop():
    # 24 pre-months flat at 6.0, then a clear drop to ~4.0 post.
    obs = [(f"2015-{m:02d}-01", "6.0") for m in range(1, 13)]
    obs += [(f"2016-{m:02d}-01", "6.0") for m in range(1, 13)]
    obs += [(f"2017-{m:02d}-01", "4.0") for m in range(1, 13)]
    obs += [(f"2018-{m:02d}-01", "4.0") for m in range(1, 13)]
    return obs


def test_compute_outcome_is_deterministic() -> None:
    obs = _flat_then_drop()
    r1 = compute_outcome(obs, event_period="2017-01-01", lag_window_months=12, sign_goal=-1, seed=42)
    r2 = compute_outcome(obs, event_period="2017-01-01", lag_window_months=12, sign_goal=-1, seed=42)
    assert r1 is not None and r2 is not None
    assert str(r1.delta) == str(r2.delta)
    assert str(r1.z) == str(r2.z)
    assert str(r1.ci_low) == str(r2.ci_low)


def test_sign_goal_orients_effect_toward_objective() -> None:
    obs = _flat_then_drop()
    # Objective: reduce the metric (sign_goal=-1). A drop => effect TOWARD goal => z>0.
    down = compute_outcome(obs, event_period="2017-01-01", lag_window_months=12, sign_goal=-1, seed=42)
    # Objective: raise the metric (sign_goal=+1). Same drop => effect AWAY => z<0.
    up = compute_outcome(obs, event_period="2017-01-01", lag_window_months=12, sign_goal=1, seed=42)
    assert float(down.z) > 0 > float(up.z)
    assert float(down.z) == -float(up.z)


def test_s_outcome_monotonic_and_centered() -> None:
    assert float(s_outcome_from_z(0)) == 50.0
    assert float(s_outcome_from_z(3)) > 90.0
    assert float(s_outcome_from_z(-3)) < 10.0


def test_insufficient_data_returns_none() -> None:
    obs = [(f"2017-{m:02d}-01", "5.0") for m in range(1, 4)]  # too few pre-points
    assert compute_outcome(obs, event_period="2017-02-01", lag_window_months=12, sign_goal=1, seed=1) is None
