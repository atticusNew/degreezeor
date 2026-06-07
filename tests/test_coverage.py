"""Coverage dashboard aggregation (complete-visibility transparency)."""

from __future__ import annotations

from datetime import date

from degreezeor.api.presentation import build_coverage
from degreezeor.core.models import Action, EvaluationUnit


def _action(session, atype: str, n: int) -> int:
    a = Action(type=atype, title=f"{atype} {n}", action_date=date(2020, 1, 1),
               source_id=1, source_url="https://x", native_identifier=f"{atype}-{n}")
    session.add(a)
    session.flush()
    return a.id


def _seed(session) -> None:
    specs = [
        ("law", "scored"), ("law", "scored"), ("law", "insufficient_evidence"),
        ("law", "non_scoreable_no_metric"), ("eo", "insufficient_evidence"),
        ("eo", "non_scoreable_no_objective"),
    ]
    for i, (atype, status) in enumerate(specs):
        aid = _action(session, atype, i)
        session.add(EvaluationUnit(action_id=aid, status=status))
    session.flush()


def test_coverage_totals_and_shares(session) -> None:
    _seed(session)
    c = build_coverage(session)
    assert c["total_evaluation_units"] == 6
    assert c["scored"] == 2
    assert c["insufficient_evidence"] == 2
    assert c["non_scoreable"] == 2  # everything not scored/insufficient
    assert abs(c["scored_share"] - 2 / 6) < 1e-3  # share is rounded to 4 dp


def test_coverage_by_action_type(session) -> None:
    _seed(session)
    c = build_coverage(session)
    assert c["by_action_type"]["law"]["scored"] == 2
    assert c["by_action_type"]["eo"]["insufficient_evidence"] == 1


def test_coverage_empty_is_safe(session) -> None:
    c = build_coverage(session)
    assert c["total_evaluation_units"] == 0
    assert c["scored_share"] == 0.0
