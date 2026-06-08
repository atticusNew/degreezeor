# degreezeor — Methodology (v0.3.0)

This document is the public, versioned methodology. Scores always display the methodology
version that produced them; historical scores are immutable and re-derivable.

## 1. Philosophy: procedural vs. substantive neutrality

Two kinds of neutrality must be separated:

- **Procedural neutrality** — identical rules, code, sources, and process for everyone; fully
  transparent, reproducible, auditable. **We target ~100% of this.**
- **Substantive (value) neutrality** — deciding what counts as a *good* outcome. **Impossible**:
  aggregating liberty vs. equality vs. growth vs. safety into one number is itself an ideology.

Therefore degreezeor does **not** emit a default normative "good/bad" score. It answers a *factual*
question and pushes values to a user-controlled layer with a neutral default.

> **The factual question:** *Did the measurable outcome tied to this action's own stated objective
> move, relative to a defensible baseline, and how much is credibly attributable to this official —
> with what confidence — and here is every official source?*

### Three irreducible non-empirical residues (labeled, never hidden)
1. **Metric choice** — even "use the stated objective" privileges the sponsor's framing.
2. **Baseline/counterfactual choice** — different defensible designs give different deltas.
3. **Attribution** — causal credit in multi-actor, multi-causal systems is underdetermined.

When a residue dominates → output is **"insufficient evidence / non-scoreable"**, *not* a low score.

## 2. Source hierarchy (provenance tiers)

- **Tier 0** — primary record of the action (Congress.gov / GovInfo / Federal Register).
- **Tier 1** — official statistical outcome data (BLS, BEA, Census, FBI, CDC, EIA, USAspending…).
- **Tier 2** — official nonpartisan analysis (CBO, GAO, CRS, agency IGs).
- **Tier 3** — convenience mirrors (FRED, OpenStates) — only with verification back to Tier 0/1.

Every stored datum carries `source_url`, `content_hash`, `retrieved_at`, `tier`, and a native
identifier (e.g., Public Law number, BLS series id).

## 3. Scoreable unit

- Atomic data unit: a polymorphic **Action** (vote / bill / law / EO / regulation / budget / …).
- Atomic **scoreable** unit: the tuple **(Action → Objective → Metric → Baseline → Outcome)**, an
  *Evaluation Unit (EU)*.
- Outcomes attach to **implemented policies**, not votes. Votes/sponsorship/signature are
  **attribution channels** that propagate a policy's measured outcome to officials.

## 4. Outcome score (sign-neutral)

```
delta             = observed − baseline_pooled
delta_toward_goal = sign_goal · delta        # sign_goal fixed at pre-registration from the objective
z                 = delta_toward_goal / noise_scale
S_outcome         = 100 · logistic(1.702 · z)    # 50 = no effect
```

`sign_goal = +1` if a *rise* in the metric moves toward the stated goal, `−1` if a *fall* does.
This is the ideology-free core: we only ask whether the metric moved toward the goal **you stated**.

## 5. Baseline ensemble & model dependence

A baseline ensemble estimates the counterfactual. Methods plug in behind one `BaselineMethod` interface:

- `pretrend_projection` — project the pre-period linear trend to the evaluation point. The formal
  answer to *"don't credit inherited trends"*: the pre-existing trajectory **is** the baseline.
- `flat_last_value` — naive no-change counterfactual.
- `difference_in_differences` — counterfactual = treated pre-level + the control group's pre→post
  change; eligible only when pre-trends are parallel. Differences out shocks common to treated and
  control units.
- `synthetic_control` — counterfactual = a nonnegative, sum-to-one weighted blend of donor units
  that best matches the treated unit's pre-period path (Abadie-style); eligible only when the
  pre-fit is tight. Strongest identification in the system.

**Tiered pooling:** when a comparison design (DiD / synthetic control) is eligible it *drives* the
estimate; naive single-series baselines are reported for transparency but never dilute a
well-identified counterfactual. Comparison designs require donor/control units (sub-national
variation) — e.g. a state policy scored against a pool of donor states' official series.

`model_dependence ∈ [0,1]` rises with sign disagreement / spread across the active methods and lowers
confidence. A bootstrap (deterministic seed) yields a 95% CI on the delta.

## 6. Attribution

`attribution = f(formal_authority, pivotality, implementation_control)`, reported as an interval,
**always** leaving a large explicit `unattributable_residual` (`Σ humans + residual = 1`,
human total capped at 0.70). Vote pivotality ≈ `1/(margin+1)` — high only for razor-thin votes.
For an enacted law, the final-passage roll-call of **both chambers** (House clerk XML + Senate
LIS XML, both Tier-0) is ingested, and each chamber's winning-side members receive decisive-vote
attribution scaled by **that chamber's own margin**. (Senate records key on `lis_member_id`; the
`unitedstates/congress-legislators` dataset bridges it to Bioguide — used strictly for identifier
resolution, never as a scored quantity.)

## 7. Confidence gate (prevents false precision)

```
C = c_design · c_data · c_attrib · c_modeldep · c_sensitivity   # each ∈ [0,1]
```
- `c_design` — identification strength. A single federal time series with a pre-trend baseline is
  capped (it cannot separate a policy from concurrent macro shocks), so federal macro cases often
  fall below the publish threshold **by design**.
- `c_data` — provenance tier + completeness.
- `c_attrib` — `1 − mean(attribution interval width)`.
- `c_modeldep` — `1 − model_dependence`.
- `c_sensitivity` — `0.5` when the effect's direction flips across defensible lag horizons
  (fragile), else `1.0`. Ties the sensitivity analysis (§9.10) back into the score.

If `C < 0.60` (default), the composite is **suppressed** and the EU renders **"insufficient
evidence."** This is the correct, humble behavior — not a failure.

## 7b. Two evaluation modes

- **Baseline-relative (causal effect):** "did the metric move vs. a defensible counterfactual?"
  (pre/post, DiD, synthetic control). Identification is the hard part.
- **Target-relative (promise-keeping):** "did the policy deliver its own pre-registered, source-linked
  numeric target?" Realized = official data; target = the policy's CBO/statutory number.
  **Integrity guardrail:** target-relative earns high identification (`declared_target_direct`, 0.90)
  **only when the realized series is directly attributable to the action** (e.g. a law's own
  DEFC-tagged USAspending total). Economy-wide realized series are `declared_target_confounded`
  (0.35) and stay gated. This is how target-relative scoring reduces "insufficient evidence"
  *legitimately* — it only scores what's genuinely attributable.

## 8. Default output vs. opt-in composite

- **Default (public):** the decomposed factual component vector — `outcome, evidence, attribution,
  alignment, dataquality, durability` — each with uncertainty, plus confidence and the source trail.
- **Composite (opt-in, value-laden):** `EU_score = gate(C) · Σ_k w_k · S_k`. The **neutral default**
  averages only the **achievement** components — `outcome` and `durability`, both directional toward
  the stated goal — and then scales by confidence `C`. Crucially, `evidence` (=`c_design`) and
  `dataquality` (=`c_data`) are *already inside* `C`, so averaging them into the composite too would
  **double-count confidence**; `alignment` is a metric-fidelity quality measure shown for context.
  `durability` is goal-directional, so a *persistent move away from the objective* correctly scores
  **low** (not high). Value-laden lenses (`cost`, `distribution`) are off by default. Users may
  reweight any component; the UI watermarks non-neutral weightings.
- **Official roll-up:** attribution-weighted mean of EU scores, always reported with **coverage**
  (fraction of actions that are scoreable) and confidence.

## 9. Bias-minimization protocol

1. Identical pipeline for all officials; **scoring code is party-blind** — enforced statically (no
   `scoring/` module reads party) **and** behaviourally (swapping an official's party cannot change a
   stored score).
2. **Pre-registration**: metric + baseline + lag + sign_goal hashed to the audit chain **before**
   outcome data is fetched — kills outcome-driven cherry-picking.
3. **Party-masked** objective→metric selection.
4. No editorial labels; numbers, intervals, and sources only.
5. Open methodology + open-source formula; changes via reviewed PRs.
6. Uncertainty always shown; composite gated by confidence.
7. Append-only, hash-chained audit log; pinned, bit-reproducible score runs.
8. Always a large unattributable residual.
9. **Sensitivity analysis** — each scoreable EU re-evaluates its outcome across defensible lag
   horizons (12–60 months) and reports robustness (direction stability + significance fraction);
   a sign that flips with the horizon is flagged as not-robust. (`/api/evaluation-units/{id}/sensitivity`.)
10. **Dispute / appeal process** — anyone can trigger an independent, deterministic re-run; the
    result (reproduced vs. corrected) + public diff is recorded on the audit chain.
11. **Relationship graph** — officials↔actions↔jurisdictions↔metrics exposed for transparency.
12. **Reproducibility self-audit** — every published score can be independently re-derived from
    its stored inputs + pinned methodology and must reproduce its hash bit-for-bit; the platform-wide
    audit re-runs them all and flags any mismatch (non-determinism / tampering) as a hard failure.
    (`/api/integrity/reproducibility`, `degreezeor verify-scores`.)
13. **Integrity-at-scale monitoring** — because even a party-blind formula can produce uneven
    *distributions*, the platform publishes the party-level distribution of scored outcomes
    (attribution-weighted mean composite + coverage per party) and flags any systematic
    composite-gap or scored-share-gap for **human methodological review** of metric/baseline
    choices — never an automated correction, and never a change to any individual score. Party is
    read here for audit only. (`/api/integrity/party-symmetry`, `degreezeor party-symmetry`, and the
    "Integrity" view.)

## 10. What cannot be made fully empirical

Metric selection, baseline choice, causal attribution, and *whether an outcome is desirable*. These
are surfaced explicitly (intervals, model-dependence, residual, value-weight layer), never hidden.

## 11. "Insufficient evidence" is triggered by

Missing/contradictory data, no operational metric, no implementation, dominant external shocks,
sign disagreement across baselines, or confidence below the publish threshold.

## Roadmap

- **Phase 1 (shipped):** US Congress, enacted economic/fiscal laws; Congress.gov + CBO links +
  BLS; pre-trend/flat baselines; sponsor/signer/decisive-vote attribution (House + Senate roll-calls);
  confidence gate; API + UI.
- **Phase 2 (in progress):** **difference-in-differences & synthetic control shipped** (with
  tiered pooling and state-policy scoring on official BLS state series — e.g. the Kansas 2012 tax
  cuts demo, which clears the gate and yields a real composite). **Executive orders shipped**
  (Federal Register Tier-0 ingestion; the signing president carries high unilateral executive
  authority vs. shared law-signing). **Final agency rules (regulations) shipped** (Federal Register
  Tier-0; attributed to the administration in office on the rule's effective date). **Senate
  roll-calls shipped** (both chambers' passage voters credited, per-chamber pivotality). **Budget
  execution** scored for every toptier agency (reliable, commensurable by construction). Remaining:
  more domains (energy, health, education); deeper regulatory outcome metrics.
- **Phase 3:** state/local government; distributional & cost lenses; user value-weight profiles;
  reproducible notebooks; third-party audits.
- **Ultimate:** all levels of government, real-time ingestion, full causal ensemble, audit program.
