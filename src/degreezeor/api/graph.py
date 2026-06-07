"""Relationship graph (PLAN.md §10/§11).

Projects the relational system-of-record into a typed node/edge graph for queries and
visualization: who acted on what, in which jurisdiction, measured by which metric. The
slice builds this directly from Postgres relationships (no separate graph engine yet —
Apache AGE / Neo4j plug in later behind this same projection without changing callers).

Node types: official | action | jurisdiction | metric
Edge relations:
  official -> action      : attribution role (sponsor | signer | decisive_vote | ...) + weight
  action   -> jurisdiction: "in"
  action   -> metric      : "measured_by"
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from degreezeor.core.models import (
    Action,
    AttributionWeight,
    EvaluationUnit,
    Jurisdiction,
    Metric,
    Official,
)


def _truncate(text: str, n: int = 48) -> str:
    return text if len(text) <= n else text[: n - 1] + "\u2026"


def build_graph(session: Session, *, official_id: int | None = None) -> dict[str, Any]:
    """Build the relationship graph. If ``official_id`` is given, restrict to that
    official's neighborhood (their actions + those actions' jurisdictions/metrics +
    co-actors who share attribution on the same actions)."""
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    def add_node(node_id: str, ntype: str, label: str, **extra: Any) -> None:
        if node_id not in nodes:
            nodes[node_id] = {"id": node_id, "type": ntype, "label": label, **extra}

    attributions = session.execute(
        select(AttributionWeight).where(AttributionWeight.is_residual.is_(False))
    ).scalars().all()

    # If focusing on one official, find their action set first.
    focus_action_ids: set[int] | None = None
    if official_id is not None:
        focus_action_ids = set()
        for aw in attributions:
            if aw.official_id == official_id:
                eu = session.get(EvaluationUnit, aw.eu_id)
                if eu:
                    focus_action_ids.add(eu.action_id)

    seen_action_metric: set[tuple[int, int]] = set()
    for aw in attributions:
        eu = session.get(EvaluationUnit, aw.eu_id)
        if eu is None:
            continue
        action = session.get(Action, eu.action_id)
        if action is None:
            continue
        if focus_action_ids is not None and action.id not in focus_action_ids:
            continue

        official = session.get(Official, aw.official_id) if aw.official_id else None
        if official is None:
            continue
        oid = f"official:{official.id}"
        aid = f"action:{action.id}"
        add_node(oid, "official", official.full_name, ref_id=official.id)
        add_node(aid, "action", _truncate(action.title), ref_id=action.id,
                 subtype=action.type, eu_id=eu.id)
        edges.append({
            "source": oid, "target": aid, "relation": aw.role,
            "weight": float(aw.attribution),
        })

        # action -> jurisdiction
        if action.jurisdiction_id:
            jur = session.get(Jurisdiction, action.jurisdiction_id)
            if jur:
                jid = f"jurisdiction:{jur.id}"
                add_node(jid, "jurisdiction", jur.name)
                edges.append({"source": aid, "target": jid, "relation": "in"})

        # action -> metric (via its evaluation unit)
        if eu.metric_id and (action.id, eu.metric_id) not in seen_action_metric:
            seen_action_metric.add((action.id, eu.metric_id))
            metric = session.get(Metric, eu.metric_id)
            if metric:
                mid = f"metric:{metric.id}"
                add_node(mid, "metric", metric.name)
                edges.append({"source": aid, "target": mid, "relation": "measured_by"})

    return {
        "nodes": list(nodes.values()),
        "edges": edges,
        "focus": f"official:{official_id}" if official_id is not None else None,
        "legend": {
            "official": "person holding/held office",
            "action": "law / executive order / state policy",
            "jurisdiction": "governing jurisdiction",
            "metric": "official outcome series",
        },
    }
