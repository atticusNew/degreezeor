# Deploying degreezeor (Render)

The platform separates **ingestion/scoring** (slow, batch) from **serving** (fast,
read-only) so the UI is always quick and the heavy work runs on a schedule.

```
┌─ Cron job ───────────┐      ┌─ Postgres ──────────┐      ┌─ API web service ──┐      ┌─ Static site ─┐
│ degreezeor refresh   │ ───▶ │ actions, scores,    │ ◀─── │ FastAPI (read-only)│ ◀─── │ SPA (web/)    │
│ (nightly, idempotent)│      │ audit chain, …      │      │ /api/* in ms       │      │ Actions/…/Graph│
└──────────────────────┘      └─────────────────────┘      └────────────────────┘      └───────────────┘
```

## One-time deploy (Blueprint)
1. Push this repo to GitHub.
2. In Render: **New → Blueprint**, point at the repo. It reads [`render.yaml`](../render.yaml) and
   provisions: a **Postgres** DB, the **API** web service, the **static** frontend, and the
   **cron** ingestion job.
3. Set the secret env vars (Render dashboard → each service → Environment):
   - `DZ_CONGRESS_API_KEY` (Congress.gov / api.data.gov)
   - `DZ_BLS_API_KEY` (BLS registered tier)
   - `DZ_COURTLISTENER_TOKEN` (optional; raises CourtListener limits)
   `DZ_DATABASE_URL` is wired automatically from the managed DB; `DZ_DATA_DIR` points at a
   persistent disk for the replay cache / immutable landing.
4. First deploy runs `degreezeor migrate` (schema) automatically. Trigger the cron once
   (Render → cron job → "Run now") to populate the first dataset, or run `degreezeor refresh`
   from a one-off shell.

## How each piece maps
| Concern | Render resource | Command |
|---|---|---|
| Schema | (pre-deploy) | `degreezeor migrate` (Alembic `upgrade head`) |
| Serving | Web service | `uvicorn degreezeor.api.app:app --host 0.0.0.0 --port $PORT` |
| Frontend | Static site | SPA in `web/`; `config.js` is generated at build with the API URL |
| Ingestion + scoring | Cron job | `degreezeor refresh` (idempotent — safe to re-run) |

## Why it's fast in production
- The API only **reads precomputed scores** from Postgres — no live official-API calls on the
  request path. Page loads are DB reads (ms).
- Scores are **deterministic and pinned** (`{data_snapshot, methodology_version, git_sha, seed}`),
  so they're computed once by the cron and served until inputs/methodology change.
- The cron is **idempotent** (every scorer skips already-scored units), so nightly runs only add
  what's new and never duplicate.

## Operational notes
- **Migrations:** schema changes are versioned in `alembic/versions/`. Generate new ones with
  `alembic revision --autogenerate -m "..."`; deploys apply them via `degreezeor migrate`.
- **Durable cache/landing:** `DZ_DATA_DIR` (a Render disk) holds the URL replay cache + immutable
  raw landings, so re-runs are fast/offline and provenance survives redeploys. For multi-instance
  setups, move `RawLanding` to object storage (S3) — `storage_path` + `content_hash` already
  support it.
- **Secrets:** only via env vars (never committed). The local `.env` is git-ignored.
- **Scaling:** the API is stateless/read-only — scale horizontally. Postgres has indexes on the hot
  paths (`evaluation_units.action_id/status`, `attribution_weights.eu_id/official_id`, score-run FKs).
- **Audit:** the hash-chained audit log (`/api/audit/verify`) detects any tampering with history.

## Local development (no Render)
Use SQLite + a quick subset (see the README quickstart). For a Postgres dev env, set
`DZ_DATABASE_URL` and run `degreezeor migrate` (prod parity) or `degreezeor initdb` (create_all).
