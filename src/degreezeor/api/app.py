"""FastAPI read API exposing scorecards + the audit trail.

Read-only by design: scores are produced by the offline pipeline (pinned,
reproducible runs); the API only serves them with full provenance so anyone can
audit the path from score back to official source.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

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
