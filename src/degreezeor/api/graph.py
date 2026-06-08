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


def build_graph(
    session: Session, *, official_id: int | None = None, min_weight: float = 0.0
) -> dict[str, Any]:
    """Build the relationship graph. If ``official_id`` is given, restrict to that
    official's neighborhood. ``min_weight`` drops official→action edges below that
    attribution weight (e.g. the tiny non-decisive vote edges), so the full graph stays
    readable; officials left with no edges are pruned."""
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    def add_node(node_id: str, ntype: str, label: str, **extra: Any) -> None:
        if node_id not in nodes:
            nodes[node_id] = {"id": node_id, "type": ntype, "label": label, **extra}

    attributions = session.execute(
        select(AttributionWeight).where(AttributionWeight.is_residual.is_(False))
    ).scalars().all()

    # Bulk-load every entity once (avoids per-attribution N+1 — the old path did 4+
    # session.get() calls for each of hundreds of vote edges, even sub-threshold ones).
    eu_ids = {aw.eu_id for aw in attributions}
    eu_map = {e.id: e for e in session.execute(
        select(EvaluationUnit).where(EvaluationUnit.id.in_(eu_ids))).scalars()} if eu_ids else {}
    action_ids = {e.action_id for e in eu_map.values()}
    action_map = {a.id: a for a in session.execute(
        select(Action).where(Action.id.in_(action_ids))).scalars()} if action_ids else {}
    official_ids = {aw.official_id for aw in attributions if aw.official_id}
    official_map = {o.id: o for o in session.execute(
        select(Official).where(Official.id.in_(official_ids))).scalars()} if official_ids else {}
    jur_map = {j.id: j for j in session.execute(select(Jurisdiction)).scalars()}
    metric_map = {m.id: m for m in session.execute(select(Metric)).scalars()}

    # If focusing on one official, find their action set first.
    focus_action_ids: set[int] | None = None
    if official_id is not None:
        focus_action_ids = {
            eu_map[aw.eu_id].action_id
            for aw in attributions
            if aw.official_id == official_id and aw.eu_id in eu_map
        }

    seen_action_metric: set[tuple[int, int]] = set()
    seen_action_jur: set[tuple[int, int]] = set()
    for aw in attributions:
        # Drop sub-threshold (e.g. non-decisive vote) edges FIRST, before any work.
        if aw.official_id is None or float(aw.attribution) < min_weight:
            continue
        eu = eu_map.get(aw.eu_id)
        if eu is None:
            continue
        action = action_map.get(eu.action_id)
        if action is None:
            continue
        if focus_action_ids is not None and action.id not in focus_action_ids:
            continue
        official = official_map.get(aw.official_id)
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

        # action -> jurisdiction (once per action)
        if action.jurisdiction_id and (action.id, action.jurisdiction_id) not in seen_action_jur:
            seen_action_jur.add((action.id, action.jurisdiction_id))
            jur = jur_map.get(action.jurisdiction_id)
            if jur:
                jid = f"jurisdiction:{jur.id}"
                add_node(jid, "jurisdiction", jur.name)
                edges.append({"source": aid, "target": jid, "relation": "in"})

        # action -> metric (via its evaluation unit)
        if eu.metric_id and (action.id, eu.metric_id) not in seen_action_metric:
            seen_action_metric.add((action.id, eu.metric_id))
            metric = metric_map.get(eu.metric_id)
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
