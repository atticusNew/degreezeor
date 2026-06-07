"""Relationship graph construction from relational data."""

from __future__ import annotations

from datetime import date

from degreezeor.api.graph import build_graph
from degreezeor.core.models import (
    Action,
    AttributionWeight,
    EvaluationUnit,
    Jurisdiction,
    Metric,
    Official,
)


def _seed(session):
    jur = Jurisdiction(type="federal", name="United States", fips="US")
    session.add(jur)
    metric = Metric(code="unrate", name="Unemployment Rate", unit="percent",
                    direction_good="down", source_id=1, native_series_id="LNS14000000")
    o1 = Official(full_name="Sponsor One", bioguide_id="S1")
    o2 = Official(full_name="Signer Two", bioguide_id="S2")
    session.add_all([metric, o1, o2])
    session.flush()
    action = Action(type="law", title="A Big Economic Law With A Very Long Title To Truncate",
                    action_date=date(2010, 1, 1), jurisdiction_id=jur.id, source_id=1,
                    source_url="https://x", native_identifier="PL1")
    session.add(action)
    session.flush()
    eu = EvaluationUnit(action_id=action.id, metric_id=metric.id, status="scored")
    session.add(eu)
    session.flush()
    session.add_all([
        AttributionWeight(eu_id=eu.id, official_id=o1.id, role="sponsor", authority=0.15,
                          pivotality=1, attribution=0.15, attr_ci_low=0.1, attr_ci_high=0.25),
        AttributionWeight(eu_id=eu.id, official_id=o2.id, role="signer", authority=0.15,
                          pivotality=1, attribution=0.15, attr_ci_low=0.1, attr_ci_high=0.25),
        AttributionWeight(eu_id=eu.id, official_id=None, role="unattributable_residual",
                          authority=0, pivotality=0, attribution=0.70, attr_ci_low=0.70,
                          attr_ci_high=0.70, is_residual=True),
    ])
    session.flush()
    return o1.id, action.id


def test_graph_has_expected_nodes_and_edges(session) -> None:
    o1_id, action_id = _seed(session)
    g = build_graph(session)
    types = sorted({n["type"] for n in g["nodes"]})
    assert types == ["action", "jurisdiction", "metric", "official"]
    # Two officials (sponsor + signer); residual is NOT a node.
    assert sum(1 for n in g["nodes"] if n["type"] == "official") == 2
    relations = {e["relation"] for e in g["edges"]}
    assert {"sponsor", "signer", "in", "measured_by"} <= relations


def test_residual_is_not_a_node(session) -> None:
    _seed(session)
    g = build_graph(session)
    assert all("residual" not in (n["label"] or "").lower() for n in g["nodes"])


def test_official_focus_restricts_graph(session) -> None:
    o1_id, action_id = _seed(session)
    g = build_graph(session, official_id=o1_id)
    assert g["focus"] == f"official:{o1_id}"
    # Sponsor One's neighborhood still includes the action + its metric/jurisdiction + co-actor.
    assert any(n["id"] == f"action:{action_id}" for n in g["nodes"])


def test_long_titles_truncated(session) -> None:
    _seed(session)
    g = build_graph(session)
    action_node = next(n for n in g["nodes"] if n["type"] == "action")
    assert len(action_node["label"]) <= 48
