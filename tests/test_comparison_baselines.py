"""DiD & synthetic control recover a known effect through a shared shock.

We build a treated unit and donor units that share a strong nonlinear+trend
"shock" f(t). A naive pre/post baseline is confounded by f(t); a comparison
design differences it out. The known post-treatment effect is -2.0, and both
DiD and synthetic control must recover delta ≈ -2.0.
"""

from __future__ import annotations

import math

from degreezeor.core.interfaces import BaselineContext, TimePoint
from degreezeor.scoring.baseline import DifferenceInDifferences, SyntheticControl
from degreezeor.scoring.confidence import best_design
from degreezeor.scoring.outcome import compute_outcome

EVENT = "2017-01-01"
EFFECT = -2.0


def _f(t: int) -> float:
    return 3.0 * math.sin(t / 3.0) + 0.5 * t  # shared shock: nonlinear + trend


def _series(level: float, treated: bool) -> list[tuple[str, str]]:
    out = []
    for t in range(48):  # 2015-01 .. 2018-12
        year = 2015 + t // 12
        month = t % 12 + 1
        period = f"{year}-{month:02d}-01"
        val = level + _f(t)
        if treated and t >= 24:  # event at index 24 (2017-01)
            val += EFFECT
        out.append((period, f"{val:.6f}"))
    return out


def _setup():
    treated = _series(10.0, treated=True)
    donors = {
        "A": _series(12.0, treated=False),
        "B": _series(8.0, treated=False),
        "C": _series(10.0, treated=False),
    }
    return treated, donors


def test_did_recovers_known_effect_through_shared_shock() -> None:
    treated, donors = _setup()
    res = compute_outcome(
        treated, event_period=EVENT, lag_window_months=12, sign_goal=1, seed=1,
        donor_observations=donors,
    )
    assert res is not None
    methods = [e.method for e in res.per_method]
    assert "difference_in_differences" in methods
    assert "synthetic_control" in methods
    # Delta should be ~ the true effect (-2), NOT the shock-confounded naive delta.
    assert abs(float(res.delta) - EFFECT) < 0.5


def test_strong_design_is_preferred_and_low_model_dependence() -> None:
    treated, donors = _setup()
    res = compute_outcome(
        treated, event_period=EVENT, lag_window_months=12, sign_goal=1, seed=1,
        donor_observations=donors,
    )
    assert best_design([e.method for e in res.per_method]) == "synthetic_control"
    # DiD and synthetic control should broadly agree => low model dependence.
    assert float(res.model_dependence) < 0.5


def test_comparison_design_beats_naive_baseline() -> None:
    # Without donors only pre/post baselines run and the shared shock confounds them;
    # the comparison design recovers the true effect far more accurately.
    treated, donors = _setup()
    naive = compute_outcome(treated, event_period=EVENT, lag_window_months=12, sign_goal=1, seed=1)
    strong = compute_outcome(
        treated, event_period=EVENT, lag_window_months=12, sign_goal=1, seed=1,
        donor_observations=donors,
    )
    assert all(e.method in {"pretrend_projection", "flat_last_value"} for e in naive.per_method)
    naive_err = abs(float(naive.delta) - EFFECT)
    strong_err = abs(float(strong.delta) - EFFECT)
    assert strong_err < 0.5
    assert naive_err > strong_err + 0.3  # comparison design is materially more accurate


def test_did_requires_donors_and_synth_requires_two() -> None:
    treated, donors = _setup()
    pre = [TimePoint(p, __import__("decimal").Decimal(v)) for p, v in treated if p < EVENT]
    post = [TimePoint(p, __import__("decimal").Decimal(v)) for p, v in treated if p >= EVENT]
    no_donor = BaselineContext(eu_id=0, metric_code="", event_period=EVENT,
                               lag_window_months=12, pre_series=pre, post_series=post, donors={})
    assert DifferenceInDifferences().eligible(no_donor) is False
    assert SyntheticControl().eligible(no_donor) is False
