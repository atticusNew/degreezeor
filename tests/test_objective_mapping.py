"""Objective->metric mapping: party masking + deterministic, symmetric selection."""

from __future__ import annotations

from degreezeor.scoring.objective import mask_party_and_name, select_metrics


def test_party_labels_are_masked() -> None:
    raw = "Sponsored by Rep. Smith [R-TX-1], a Republican, to reduce unemployment."
    masked = mask_party_and_name(raw)
    assert "[R-TX-1]" not in masked
    assert "Republican" not in masked
    assert "Rep." not in masked
    assert "[MASKED]" in masked


def test_same_objective_maps_identically_regardless_of_party() -> None:
    dem = "A Democratic bill to create jobs and promote economic recovery."
    rep = "A Republican bill to create jobs and promote economic recovery."
    p_dem, _ = select_metrics(dem, "Economics and Public Finance")
    p_rep, _ = select_metrics(rep, "Economics and Public Finance")
    assert p_dem is not None and p_rep is not None
    assert p_dem.spec.code == p_rep.spec.code
    assert str(p_dem.alignment) == str(p_rep.alignment)


def test_unemployment_objective_selects_unemployment_or_jobs_metric() -> None:
    primary, _side = select_metrics("reduce unemployment and joblessness", None)
    assert primary is not None
    assert primary.spec.code in {"unemployment_rate", "nonfarm_employment"}


def test_no_match_returns_none() -> None:
    primary, side = select_metrics("a resolution honoring the national peach festival", None)
    assert primary is None
    assert side == []
