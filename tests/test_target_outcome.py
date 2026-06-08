"""Target-relative ('promise-keeping') outcome + the directly-attributable guardrail."""

from __future__ import annotations

from decimal import Decimal

from degreezeor.scoring.confidence import compute_confidence
from degreezeor.scoring.target_outcome import compute_target_outcome


def test_fingerprint_stable_across_numeric_float_roundtrip() -> None:
    """Reproducibility keystone (regression): a trillion-scale money value passed as a
    raw float (score time) vs. its float-expanded Decimal (after a NUMERIC->REAL->Decimal
    storage round-trip, as on SQLite) MUST yield the identical fingerprint + outcome.
    Caught by the reproducibility self-audit: at trillion-scale a double can't hold 4-dp
    precision, so the cents-quantization at entry is what keeps the hash bit-stable."""
    realized = 1393415222232.11  # DoD FY2024 obligated
    target = 1991791156124.69  # DoD FY2024 budgetary resources
    at_score = compute_target_outcome(
        realized=realized, target=target, sign_goal=1,
        directly_attributable=True, eval_period="2024-09-01")
    # Decimal(float) reproduces the full binary expansion a DB read-back produces.
    at_rescore = compute_target_outcome(
        realized=Decimal(realized), target=Decimal(target), sign_goal=1,
        directly_attributable=True, eval_period="2024-09-01")
    assert at_score.outcome.input_hash == at_rescore.outcome.input_hash
    assert at_score.outcome.observed == at_rescore.outcome.observed
    assert at_score.outcome.baseline_pooled == at_rescore.outcome.baseline_pooled
    assert at_score.outcome.delta == at_rescore.outcome.delta
    assert at_score.s_outcome == at_rescore.s_outcome


def test_full_delivery_scores_high() -> None:
    tc = compute_target_outcome(
        realized=300, target=285, sign_goal=1, directly_attributable=True, eval_period="2020-03-01"
    )
    assert float(tc.s_outcome) == 100.0  # delivered >= committed
    assert tc.best_method == "declared_target_direct"


def test_partial_delivery_is_proportional() -> None:
    tc = compute_target_outcome(
        realized=239.2, target=285.4, sign_goal=1, directly_attributable=True, eval_period="2020-03-01"
    )
    assert 83.0 < float(tc.s_outcome) < 84.5  # ~84% delivered


def test_cost_overrun_penalized_when_lower_is_better() -> None:
    # sign_goal=-1: "stay within the projected cost"; spending 2x target is bad.
    tc = compute_target_outcome(
        realized=200, target=100, sign_goal=-1, directly_attributable=True, eval_period="2020-03-01"
    )
    assert float(tc.s_outcome) == 50.0  # target/realized = 0.5


def test_confounded_realized_uses_weak_design_method() -> None:
    tc = compute_target_outcome(
        realized=239, target=285, sign_goal=1, directly_attributable=False, eval_period="2020-03-01"
    )
    assert tc.best_method == "declared_target_confounded"


def test_guardrail_direct_clears_but_confounded_is_gated() -> None:
    # Same delivery; only attributability differs -> confidence diverges across the gate.
    common = dict(ci_low=-50, ci_high=-42, model_dependence=0, data_tier=1,
                  data_completeness=1.0, attribution_widths=[(1.0, 0.15)])
    direct = compute_confidence(best_method="declared_target_direct", **common)
    confounded = compute_confidence(best_method="declared_target_confounded", **common)
    assert float(direct.confidence) >= 0.60   # directly attributable => scoreable
    assert float(confounded.confidence) < 0.60  # economy-wide => insufficient evidence


def test_input_hash_deterministic() -> None:
    a = compute_target_outcome(realized=239, target=285, sign_goal=1, directly_attributable=True, eval_period="2020-03-01")
    b = compute_target_outcome(realized=239, target=285, sign_goal=1, directly_attributable=True, eval_period="2020-03-01")
    assert a.outcome.input_hash == b.outcome.input_hash
