# DegreeZero — A White Paper

**Empirical, source-anchored scoring of what public officials did, and whether it worked.**

Version 1.0 · Nonpartisan · Open methodology · Reproducible

---

## 1. The problem

Public accountability today is dominated by opinion. "Scorecards" published by
advocacy groups grade officials on whether their votes match an ideological
checklist — not on whether the things they did actually achieved their own stated
goals. Fact-checks are episodic and selective. Media coverage is fast but rarely
measures outcomes against a fixed, pre-declared standard. The result is that
citizens have no neutral, repeatable way to ask the two questions that matter most:

1. **What did this official actually act on?**
2. **Did it work — measured against the goal the action itself claimed?**

DegreeZero exists to answer those two questions with official data, a fixed
method, and a complete, auditable trail from every number back to its government
source.

## 2. Design principle: procedural neutrality, not "balance"

DegreeZero does not try to be "balanced" by splitting the difference between
parties. It is neutral in a stronger, structural sense: **the same code, the same
sources, and the same pre-registered method are applied identically to every
official and every action, and the scoring code never sees party.**

- Party affiliation is **not shown** anywhere in the product. It is stored only as
  audit metadata for an internal bias monitor (§9) and is **never read by scoring
  code** — a property enforced by automated tests.
- The method is **pre-registered**: the metric and baseline for an action are fixed
  and hashed to an append-only audit log *before* any outcome data is fetched, so
  results cannot be cherry-picked after the fact.
- Three irreducible human-judgment residues are **labeled, never hidden**: which
  actions are in scope, which official metric operationalizes a stated goal, and
  what counts as a credible counterfactual. Everything else is mechanical.

## 3. The two layers

DegreeZero publishes two clearly separated layers, never blended:

### 3.1 The scored layer — "did it work?"

A score is produced only for an **Evaluation Unit (EU)**: an atomic
`Action → Objective → Metric → Baseline → Outcome` tuple where the action's effect
can be isolated and measured. Most public actions cannot clear this bar, and the
system says so rather than guessing (§7).

### 3.2 The record layer — "what did they act on?"

Source-anchored facts describing breadth of activity, **unscored**, grouped by
topic:

- **Bills sponsored and cosponsored** (Congress.gov).
- **Roll-call votes** in the House (clerk.house.gov) and Senate (senate.gov),
  resolved to members via the official `lis ↔ bioguide` crosswalk.
- **Executive orders signed** (Federal Register).

The record layer is never fed into any score, attribution, or the bias monitor. It
exists so the public record of activity is visible alongside — and visually
distinct from — the rigorous scored layer.

## 4. Source hierarchy (provenance tiers)

Every datum is tagged with a provenance tier and stored with a content hash:

- **Tier 0** — the action record itself (Federal Register, Congress.gov bill text,
  clerk/Senate roll-call XML, court dockets).
- **Tier 1** — official government statistics (BLS, Census SAIPE/SAHIE, EIA,
  USAspending).
- **Tier 2** — official analysis (e.g. CBO, agency reports).
- **Tier 3** — verified mirrors / identifier crosswalks (entity resolution only,
  never used as a substantive datum).

## 5. The scoreable unit and the outcome score

For each EU the system computes a **sign-neutral outcome**: how far the measured
metric moved **toward the action's own stated goal**, net of a counterfactual
baseline (what the metric would likely have done anyway). A move away from the
stated goal correctly scores low; a move toward it scores high. Direction is
defined by the action's claim, not by any external value judgment.

State-policy effects are estimated with a **comparison design** (a
synthetic-control / difference-style fit against a pre-declared pool of donor
states that did not adopt the policy), so a national trend is not mistaken for a
policy effect.

## 6. Composite score (opt-in) and the default factual vector

- **Default output** is a decomposed **factual vector** — outcome, evidence,
  attribution, alignment, data quality, durability — each with uncertainty, plus
  confidence and the full source trail. There is **no default good/bad verdict.**
- **Composite (opt-in):** `EU_score = gate(confidence) · achievement-of-goal`, on a
  0–100 scale. The neutral default averages only the goal-directional achievement
  components and then scales by confidence. Value-laden lenses (cost, distribution)
  are off by default; users may reweight components, and the UI watermarks any
  non-neutral weighting.

## 7. The confidence gate — "insufficient evidence," never a low score

Confidence combines design strength, data quality, attribution clarity, model
dependence, and sensitivity. **If confidence is below the publication threshold,
DegreeZero withholds the score and reports "insufficient evidence."** This is the
core anti-spin safeguard: a weak or unisolatable case never becomes a misleadingly
precise number. Honest abstention is a feature, not a gap.

## 8. Attribution

Credit for an outcome is shared, never assumed. Attribution is a function of
formal authority, pivotality (e.g. the margin of a decisive vote), and
implementation control, normalized so that a **large, explicit unattributable
residual** always remains. A 60–40 vote credits each member far less than a 51–50
vote; a president gets signer credit but not sole credit for an economy.

## 9. Bias-minimization protocol

1. Identical pipeline for all officials; **scoring code is party-blind** (enforced
   statically and behaviourally — swapping an official's party cannot change a
   stored score).
2. **Pre-registration**: metric + baseline hashed to the audit chain before
   outcomes are fetched.
3. Party-masked metric selection.
4. Confidence gate (§7).
5. Always a large explicit unattributable residual (§8).
6. **Party-symmetry monitor** (audit only): the party-level distribution of scored
   outcomes is published for human methodological review and systematic gaps are
   flagged — never auto-corrected, never used in scoring.

## 10. Reproducibility and auditability

- Every published score is a **pinned run**: methodology version, code commit,
  random seed, and a snapshot id derived from the exact input content hashes.
- A self-audit **re-derives every published score and confirms it reproduces its
  pinned hash bit-for-bit**. Any mismatch indicates non-determinism or tampering.
- All history is recorded on an **append-only, hash-chained audit log**; the chain
  is verified after every refresh and surfaced in the product.
- Anyone may **dispute** a score. Resolution is not editorial: it triggers an
  independent, deterministic re-run and publishes whether the score changed (a
  public diff).

## 11. What cannot be made fully empirical

DegreeZero is explicit about its limits. Scope selection, metric mapping, and
counterfactual credibility involve judgment; these are labeled and versioned, not
hidden. Many important actions are simply not isolatable to a single measured
outcome — those live in the record layer, not the scored layer. The platform's
credibility rests on saying so plainly.

## 12. Architecture (brief)

- **Ingestion**: idempotent, incremental, resumable adapters for each official
  source, with bounded timeouts, retry/backoff, and a per-host circuit breaker.
- **Scoring**: deterministic, pre-registered, pinned runs (Python/Decimal for all
  financial and score math).
- **Storage**: PostgreSQL via SQLAlchemy; Alembic migrations.
- **API**: read-only FastAPI serving precomputed scores with full provenance.
- **UI**: a zero-build single-page client; a pure consumer of the API.
- **Refresh**: a nightly cron (`degreezeor refresh`) ingests new actions and scores
  them; the web service only serves precomputed results.

## 13. How it stays current

The web service is read-only and serves precomputed data. A scheduled **cron** runs
`degreezeor migrate && degreezeor refresh` on a fixed cadence (recommended:
nightly). Each refresh is idempotent and recency-first, ingesting new bills, votes,
executive orders, and statistics, scoring what newly qualifies, backfilling
identifiers, verifying the audit chain, and pruning empty records. Repeated runs
fill in historical depth without duplication. Read responses are cached briefly so
repeat views are fast; the "Recently scored" feed shows what each refresh added.

## 14. Status and roadmap

Live today: federal executive orders, enacted-law delivery, agency budget
execution, ~30 curated state comparison-design policies (jobs, wages, poverty,
child poverty, health coverage, energy), the full congressional activity layer
(sponsored, cosponsored, votes), and the executive activity layer.

Near-term: additional vetted outcome series (housing permits via Census BPS,
traffic fatalities via NHTSA FARS, educational attainment via NAEP), each added
behind its own reproducibility test; deeper historical backfill; and expanded
state coverage.

---

*DegreeZero is nonpartisan, open methodology, and reproducible. Every number links
to its official source; the method is public; every score can be re-derived. For
the precise scoring formulas and parameters, see `docs/METHODOLOGY.md`.*
