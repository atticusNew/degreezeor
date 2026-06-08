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

## What is built

End-to-end, on **real official data**:

- **Action types scored:** enacted **laws** (with House *and* Senate final-passage roll-call
  attribution), **executive orders**, **regulations** (final agency rules), **agency budget
  execution** (every toptier agency), **emergency/supplemental delivery** (DEFC-tagged spending),
  **court survival** of executive actions, and **state policies**.
- **Ingestion** (Tier-0/1 official sources): Congress.gov (laws, sponsors), House Clerk + Senate
  LIS roll-call XML (with the `unitedstates/congress-legislators` lis↔bioguide crosswalk), Federal
  Register (EOs, rules), USAspending (DEFC delivery, agency budgetary resources), **BLS** and **CDC**
  (outcome series), CourtListener (case provenance). Resilient HTTP (retry/backoff/circuit-breaker)
  → **immutable, content-addressed landing**; outcome ingestion is **pluggable by source**.
- **Two scoring modes:** *baseline-relative* (causal effect vs. a counterfactual — pre/post,
  difference-in-differences, synthetic control) and *target-relative* (promise-keeping: did the
  policy deliver its own pre-registered, source-linked number?), with an integrity guardrail that
  only credits directly-attributable realized series.
- **Scoring engine**: party-masked objective→metric mapping (cross-domain catalog: economics + health)
  → **pre-registration** (metric+baseline hashed to the audit log *before* outcomes are fetched) →
  baseline ensemble → signed outcome delta + bootstrap CI + model-dependence → attribution
  (sponsor + signer + per-chamber decisive-vote pivotality, always with a large **unattributable
  residual**) → **confidence gate**.
- **Reproducibility**: every score run is pinned to `{data_snapshot, methodology_version, git_sha, seed}`
  and produces a bit-stable `reproducible_hash`; a **platform-wide reproducibility self-audit**
  (`verify-scores`) re-derives every published score and flags any mismatch.
- **Integrity-at-scale monitoring**: a **party-symmetry** report (party-level distribution of scored
  outcomes, flagged for human review — audit only; scoring stays party-blind) and a nightly cron that
  self-validates the audit chain.
- **Auditability**: append-only, hash-chained audit log (tamper-detecting).
- **API + UI**: FastAPI read API and a zero-build explainability UI — scorecards (decomposed vector,
  gate banner, baseline ensemble, attribution, source trail, reproducibility panel, user value-weights,
  **lag-window sensitivity band**, **challenge/appeal**), **Officials** roll-up, **Graph**, **Coverage**,
  and **Integrity** views.
- **Governance**: dispute/appeal workflow (independent reproducible re-run + public diff, audit-logged)
  and lag-window sensitivity analysis for every scoreable unit.

## Quickstart

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

# Postgres (target DB). Default DSN: postgresql+psycopg://degreezeor:degreezeor@localhost:5432/degreezeor
export DZ_DATABASE_URL=postgresql+psycopg://degreezeor:degreezeor@localhost:5432/degreezeor

degreezeor initdb
degreezeor score 111 5                    # federal law: ARRA 2009 (PL 111-5); ingests House+Senate votes
degreezeor score-state KS-HB2117          # state policy via synthetic control -> scored (real composite)
degreezeor score-eo --eo-number 14026     # executive order (Federal Register)
degreezeor score-regulation 2024-08038    # final agency rule (Federal Register)
degreezeor score-target CARES-DELIVERY    # target-relative delivery (USAspending) -> scored
degreezeor budget-execution 2024 --all-agencies   # every toptier agency's execution rate
degreezeor list
degreezeor verify-audit                   # replay the hash-chained audit log
degreezeor verify-scores                  # reproducibility self-audit (re-derive every score)
degreezeor party-symmetry                 # integrity monitor: party-level score distribution

uvicorn degreezeor.api.app:app --port 8077    # UI + API at http://localhost:8077
```

The nightly production pass is a single idempotent command: `degreezeor refresh` (the Render cron
entrypoint), which runs every scorer above, enriches names, and self-validates the audit chain.

**Comparison designs (Phase 2):** `score-state` evaluates a state policy against a pool of donor
states using **synthetic control / difference-in-differences** on official BLS state series. The
bundled Kansas 2012 tax-cut demo (`KS-HB2117`) is well-identified, clears the confidence gate, and
yields a real composite — a neutral, sourced finding that the policy's *own* job-creation objective
was not met relative to its synthetic control. The curated set also includes North Carolina 2013,
Wisconsin 2011, Maine 2011, Indiana 2013, Ohio 2013, and Missouri 2014 (each source-verified); the
synthetic-control pre-fit gate decides which clear the confidence gate vs. honestly abstain.

**Neutral presentation:** the user-facing app shows no party (only name and, where derivable, the
office), uses a neutral mauve tone for scores (no green/red/party-blue), and groups actions into
objective topic categories (jobs and economy, cost and spending, health, public safety, energy and
environment, poverty and income, education) derived from each action's official domain and metric.
Party stays in the data layer for the audit-only party-symmetry monitor on the Integrity page.

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

Phase 1 (vertical slice) and much of Phase 2 are **shipped**: comparison designs
(difference-in-differences, synthetic control), executive orders, regulations, Senate roll-calls,
all-agency budget execution, target-relative delivery, the health (CDC) outcome domain, the
dispute/appeal workflow, the relationship graph, and the integrity-at-scale monitoring layer.
Everything is built behind pluggable interfaces (`SourceAdapter`, `BaselineMethod`,
`AttributionChannel`, and a pluggable outcome-observation loader) so further sources/domains add
without redesign.

**Next** (mostly gated on third-party API keys or product decisions): more outcome domains that
require keys — Census (poverty/income/coverage), EIA (energy), FBI (crime); a richer frontend; and
scale infrastructure (Iceberg/Dagster/dbt) when warranted. See
[`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) for the full design + roadmap.

## License

MIT. Methodology and scoring code are open by design.
