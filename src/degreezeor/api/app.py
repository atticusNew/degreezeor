"""FastAPI read API exposing scorecards + the audit trail.

Read-only by design: scores are produced by the offline pipeline (pinned,
reproducible runs); the API only serves them with full provenance so anyone can
audit the path from score back to official source.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select

from degreezeor import __version__
from degreezeor.api import presentation
from degreezeor.config import settings
from degreezeor.core import audit
from degreezeor.core.db import session_scope

app = FastAPI(
    title="degreezeor — empirical political scoring",
    version=__version__,
    description=(
        "Measures public actions against their OWN stated objectives, with transparent "
        "baselines, attribution, confidence, and a full source trail. No default normative "
        "good/bad score; the composite is confidence-gated and value-weights are user-controlled."
    ),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": __version__,
            "confidence_publish_threshold": float(settings.confidence_publish_threshold)}


@app.get("/api/evaluation-units")
def list_evaluation_units() -> list[dict]:
    with session_scope() as s:
        return presentation.list_units(s)


@app.get("/api/evaluation-units/{eu_id}")
def get_evaluation_unit(eu_id: int) -> dict:
    with session_scope() as s:
        card = presentation.build_scorecard(s, eu_id)
    if card is None:
        raise HTTPException(status_code=404, detail="evaluation unit not found")
    return card


@app.get("/api/officials")
def list_officials(q: str | None = None, scored_only: bool = False) -> list[dict]:
    with session_scope() as s:
        return presentation.list_officials(s, q=q, scored_only=scored_only)


@app.get("/api/officials/{official_id}")
def get_official(official_id: int) -> dict:
    with session_scope() as s:
        card = presentation.build_official(s, official_id)
    if card is None:
        raise HTTPException(status_code=404, detail="official not found")
    return card


class DisputeIn(BaseModel):
    eu_id: int
    filer: str
    claim: str


@app.post("/api/disputes")
def create_dispute(payload: DisputeIn) -> dict:
    from degreezeor.disputes import file_dispute

    with session_scope() as s:
        try:
            d = file_dispute(s, eu_id=payload.eu_id, filer=payload.filer, claim=payload.claim)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return {"id": d.id, "eu_id": d.eu_id, "status": d.status, "filer": d.filer, "claim": d.claim}


@app.post("/api/disputes/{dispute_id}/resolve")
def resolve_dispute_endpoint(dispute_id: int) -> dict:
    from degreezeor.disputes import resolve_dispute

    with session_scope() as s:
        try:
            r = resolve_dispute(s, dispute_id=dispute_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return {"dispute_id": r.dispute_id, "status": r.status, "reproduced": r.reproduced,
                "public_diff": r.public_diff}


@app.get("/api/disputes")
def list_disputes(eu_id: int | None = None) -> list[dict]:
    from degreezeor.core.models import Dispute

    with session_scope() as s:
        q = select(Dispute).order_by(Dispute.id.desc())
        if eu_id is not None:
            q = q.where(Dispute.eu_id == eu_id)
        return [
            {"id": d.id, "eu_id": d.eu_id, "filer": d.filer, "claim": d.claim, "status": d.status,
             "resolution_run_id": d.resolution_run_id,
             "public_diff": json.loads(d.public_diff) if d.public_diff else None}
            for d in s.execute(q).scalars()
        ]


@app.get("/api/evaluation-units/{eu_id}/sensitivity")
def eu_sensitivity_endpoint(eu_id: int) -> dict:
    from dataclasses import asdict

    from degreezeor.pipeline import eu_sensitivity

    with session_scope() as s:
        result = eu_sensitivity(s, eu_id)
    if result is None:
        raise HTTPException(status_code=404, detail="sensitivity not available for this unit")
    out = asdict(result)
    # Decimals -> floats for JSON.
    for p in out["points"]:
        for k in ("delta", "delta_toward_goal", "z", "s_outcome", "ci_low", "ci_high"):
            p[k] = float(p[k])
    out["significant_fraction"] = float(out["significant_fraction"])
    return out


@app.get("/api/coverage")
def coverage() -> dict:
    with session_scope() as s:
        return presentation.build_coverage(s)


@app.get("/api/graph")
def relationship_graph(official_id: int | None = None, min_weight: float = 0.0) -> dict:
    from degreezeor.api import graph as graph_mod

    with session_scope() as s:
        return graph_mod.build_graph(s, official_id=official_id, min_weight=min_weight)


@app.get("/api/audit/verify")
def verify_audit() -> dict:
    with session_scope() as s:
        ok, broken = audit.verify_chain(s)
    return {"audit_chain_ok": ok, "first_broken_id": broken}


@app.get("/api/methodology")
def methodology() -> dict:
    """Machine-readable summary of the active scoring philosophy + bias controls."""
    return {
        "philosophy": (
            "Score actions against their own stated objectives; never score ideology. "
            "Default output is a decomposed factual vector + confidence, not a single verdict."
        ),
        "bias_controls": [
            "Pre-registration: metric+baseline hashed to the audit chain before outcomes are fetched.",
            "Party-masked metric selection.",
            "Identical pipeline for all officials (party-blind scoring code, enforced by tests).",
            "Confidence gate: low confidence => 'insufficient evidence', never a low score.",
            "Always a large explicit unattributable residual.",
            "Reproducible, pinned score runs; append-only hash-chained audit log.",
        ],
        "components_factual": ["outcome", "evidence", "attribution", "alignment", "dataquality", "durability"],
        "components_value_laden_off_by_default": ["cost", "distribution"],
        "confidence_publish_threshold": float(settings.confidence_publish_threshold),
    }


# --- Static explainability UI (zero-build SPA; a pure client of /api) ---
_WEB_DIR = Path(__file__).resolve().parents[3] / "web"
if _WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")
