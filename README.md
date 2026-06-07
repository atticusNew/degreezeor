# degreezeor

**Empirical, source-anchored scoring of public actions against their *own* stated objectives.**

degreezeor measures what officials *did* — votes, laws, executive actions, budgets — against
the objective the action itself stated, relative to a defensible counterfactual baseline, with
transparent attribution and an explicit confidence estimate, and a fully auditable path from
every number back to an official government source.

It is **not** an ideology scorer, a fact-checker, or a pundit. By design it does **not** emit a
default "good/bad politician" number — that would require a hidden value function. The default
artifact is a **decomposed, source-linked, confidence-gated score vector**; a single composite is
opt-in, value-laden, and confidence-gated.

> Read the philosophy and equations in [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md).

## Why "actions vs. their own stated objectives"?

The yardstick is the policy's **own** goal (from statutory purpose / official summaries / CBO
notes). This is party-symmetric: a jobs bill and a tax cut are each asked the same neutral
question — *did your own stated metric move, net of baseline, attributable to you, and how
confident are we?* When a defensible baseline cannot separate the policy from concurrent shocks,
the system reports **"insufficient evidence"** — never a low score.

## What is built (MVP slice)

End-to-end, on **real official data**:

- **Ingestion** (Tier-0/1 official sources): Congress.gov (laws, sponsors, CBO links), BLS (outcome series).
  Resilient HTTP (retry/backoff/circuit-breaker) → **immutable, content-addressed landing**.
- **Scoring engine**: party-masked objective→metric mapping → **pre-registration** (metric+baseline
  hashed to the audit log *before* outcomes are fetched) → baseline ensemble (pre-trend projection +
  flat) → signed outcome delta + bootstrap CI + model-dependence → attribution (sponsor + signer +
  decisive-vote pivotality, always with a large **unattributable residual**) → **confidence gate**.
- **Reproducibility**: every score run is pinned to `{data_snapshot, methodology_version, git_sha, seed}`
  and produces a bit-stable `reproducible_hash`.
- **Auditability**: append-only, hash-chained audit log (tamper-detecting).
- **API + UI**: FastAPI read API and a zero-build explainability UI — scorecards (decomposed vector,
  gate banner, baseline ensemble, attribution, source trail, reproducibility panel, user value-weights,
  **lag-window sensitivity band**, **challenge/appeal**), an **Officials** roll-up view, and a
  **relationship Graph** view.
- **Governance**: dispute/appeal workflow (independent reproducible re-run + public diff, audit-logged)
  and lag-window sensitivity analysis for every scoreable unit.

## Quickstart

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

# Postgres (target DB). Default DSN: postgresql+psycopg://degreezeor:degreezeor@localhost:5432/degreezeor
export DZ_DATABASE_URL=postgresql+psycopg://degreezeor:degreezeor@localhost:5432/degreezeor

degreezeor initdb
degreezeor score 111 5            # federal law: ARRA 2009 (PL 111-5) -> insufficient evidence
degreezeor score-state KS-HB2117  # state policy via synthetic control -> scored (real composite)
degreezeor score-eo --eo-number 14026   # executive order (Federal Register) -> scored vs its own objective
degreezeor list
degreezeor verify-audit

uvicorn degreezeor.api.app:app --port 8077    # UI + API at http://localhost:8077
```

**Comparison designs (Phase 2):** `score-state` evaluates a state policy against a pool of donor
states using **synthetic control / difference-in-differences** on official BLS state series. The
bundled Kansas 2012 tax-cut demo (`KS-HB2117`) is well-identified, clears the confidence gate, and
yields a real composite — a neutral, sourced finding that the policy's *own* job-creation objective
was not met relative to its synthetic control.

API keys: Congress.gov/GovInfo accept the shared `DEMO_KEY` (rate-limited) by default; the Federal
Register API is keyless. **Set `DZ_BLS_API_KEY`** (free registration) to raise BLS limits — the
keyless BLS tier has a low daily request cap that is easily exhausted when ingesting many series
(e.g. synthetic-control donor pools). A URL-keyed replay cache (`data/http_cache/`) makes repeat
runs deterministic and offline-capable; set `DZ_HTTP_CACHE=1` for cache-first (replay) mode. No
secrets are committed.

## Tests

```bash
pytest                 # unit + static neutrality guards
DZ_RUN_LIVE=1 pytest   # + live end-to-end + reproducibility (needs network + Postgres)
```

Key guards: party-blindness (scoring code may never read party), audit-chain tamper detection,
outcome determinism, attribution residual/normalization, and confidence-gate behavior.

## Deployment

Production runs as four Render resources (ingestion decoupled from serving): a managed
**Postgres**, a read-only **FastAPI web service**, a **static frontend**, and a nightly
**cron** that runs `degreezeor refresh` (idempotent ingestion + scoring). Schema is managed
by **Alembic** (`degreezeor migrate`). See [`docs/DEPLOY.md`](docs/DEPLOY.md) and
[`render.yaml`](render.yaml). The API serves precomputed, deterministic scores in
milliseconds — no live official-API calls on the request path.

## Status & roadmap

This is the **Phase-1 vertical slice**. The architecture is built behind pluggable interfaces
(`SourceAdapter`, `BaselineMethod`, `AttributionChannel`) so later phases add sources, stronger
causal designs (difference-in-differences, synthetic control), more domains, and sub-national
coverage without redesign. See [`PLAN`](docs/METHODOLOGY.md#roadmap) for the full roadmap.

## License

MIT. Methodology and scoring code are open by design.
