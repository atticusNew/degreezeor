"""Presidents-by-date resolution (signer attribution depends on it).

Regression guard: a president serving non-consecutive terms must resolve to the SAME
official record across both, so their full record aggregates on one page.
"""

from __future__ import annotations

from datetime import date

from degreezeor.core.reference import president_on


def test_second_term_resolves_to_same_record(session) -> None:
    first = president_on(session, date(2019, 3, 1))
    second = president_on(session, date(2025, 3, 1))
    assert first is not None and second is not None
    assert first.bioguide_id == second.bioguide_id == "T000452p"
    assert first.id == second.id  # one record => both terms aggregate


def test_current_term_is_covered(session) -> None:
    # An action dated after the last fixed term boundary must still resolve a president
    # (the bug: current-presidency actions were attributed to nobody).
    assert president_on(session, date(2026, 1, 1)) is not None


def test_pre_first_term_is_unknown(session) -> None:
    assert president_on(session, date(1900, 1, 1)) is None
