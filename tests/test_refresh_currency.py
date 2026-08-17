"""The nightly refresh must stay CURRENT by construction: fiscal year, congress, and
delivery windows are derived from dates, never pinned to constants that silently go stale."""

from __future__ import annotations

from datetime import date

from degreezeor.pipeline import current_congress, delivery_end_year, last_completed_fiscal_year


def test_last_completed_fiscal_year_rolls_over_on_oct_1() -> None:
    assert last_completed_fiscal_year(date(2026, 8, 17)) == 2025   # FY2026 not done yet
    assert last_completed_fiscal_year(date(2026, 9, 30)) == 2025
    assert last_completed_fiscal_year(date(2026, 10, 1)) == 2026   # FY2026 just closed
    assert last_completed_fiscal_year(date(2027, 2, 1)) == 2026


def test_current_congress_rolls_over_in_odd_januaries() -> None:
    assert current_congress(date(2025, 6, 1)) == 119
    assert current_congress(date(2026, 8, 17)) == 119
    assert current_congress(date(2027, 6, 1)) == 120  # 120th convenes Jan 2027


def test_delivery_window_is_pinned_to_the_action_never_the_clock() -> None:
    # Legacy floor: the first delivery EUs were scored with end=2025; these must not change
    # (their reproducibility hashes depend on fetching the identical window forever).
    assert delivery_end_year(date(2020, 3, 27)) == 2025  # CARES
    assert delivery_end_year(date(2021, 11, 15)) == 2025  # IIJA
    # Newer laws get windows past the old constant — still deterministic (enactment + 4).
    assert delivery_end_year(date(2023, 6, 3)) == 2027
    assert delivery_end_year(date(2026, 1, 20)) == 2030


def test_refresh_defaults_derive_from_today() -> None:
    # Signature guard: refresh_all must default these to None (derived), not to constants.
    import inspect

    from degreezeor.pipeline import refresh_all
    sig = inspect.signature(refresh_all)
    assert sig.parameters["budget_fiscal_year"].default is None
    assert sig.parameters["congress"].default is None
