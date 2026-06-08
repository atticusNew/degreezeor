"""Integrity-at-scale monitoring (PLAN.md §9.12).

Adversarial-neutrality has two layers in this codebase:

1. **Procedural blindness (prevention).** Scoring code never reads party — proven
   statically by ``tests/test_party_blindness.py`` and behaviourally by
   ``tests/test_integrity.py`` (swapping an official's party cannot change a score).

2. **Outcome monitoring (detection).** Even with a provably party-blind formula, the
   *distribution* of scored results across parties should be watched: if one party's
   actions systematically score higher, or are scored (vs. left "insufficient
   evidence") at a very different rate, that is a signal worth a HUMAN methodological
   review — of the metric/baseline choices, never of individual scores, and NEVER an
   automated correction (auto-correcting toward parity would itself inject bias).

This module implements layer 2. It is deliberately placed OUTSIDE ``degreezeor/scoring``
so the party-blindness guard (which scans only the scoring package) still holds: party
is read here purely for after-the-fact auditing, exactly as the data model intends
(``Party`` docstring: "Stored for transparency only. Scoring code MUST NOT read this").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from degreezeor.core.models import (
    AttributionWeight,
    EUScore,
    OfficeTerm,
    Party,
    ScoreRun,
)
from degreezeor.core.numeric import D, q_score

# Defaults for flagging a gap for human review. These are NOT correction targets; a
# breach raises a review flag only. Chosen to be permissive (small samples are noisy).
DEFAULT_COMPOSITE_GAP_THRESHOLD = D("15")  # points on the 0..100 composite
DEFAULT_SHARE_GAP_THRESHOLD = D("0.25")  # difference in scored-share (0..1)
DEFAULT_MIN_SCORED = 2  # require at least this many scored EUs per party to compare composites


@dataclass
class PartyStats:
    abbrev: str
    attributed_eus: int  # distinct EUs with attribution to this party
    scored_eus: int  # of those, how many cleared the gate (composite present)
    scored_share: Decimal  # scored / attributed (0..1)
    mean_composite: Decimal | None  # attribution-weighted mean composite over scored EUs
    mean_confidence: Decimal | None  # attribution-weighted mean confidence over scored EUs


@dataclass
class PartySymmetryReport:
    parties: list[PartyStats] = field(default_factory=list)
    composite_gap: Decimal | None = None  # max-min weighted-mean composite across comparable parties
    scored_share_gap: Decimal | None = None  # max-min scored-share across parties with data
    composite_gap_threshold: Decimal = DEFAULT_COMPOSITE_GAP_THRESHOLD
    scored_share_gap_threshold: Decimal = DEFAULT_SHARE_GAP_THRESHOLD
    review_required: bool = False
    review_reasons: list[str] = field(default_factory=list)

    def to_public_dict(self) -> dict:
        def f(x: Decimal | None) -> float | None:
            return float(x) if x is not None else None

        return {
            "parties": [
                {
                    "abbrev": p.abbrev,
                    "attributed_eus": p.attributed_eus,
                    "scored_eus": p.scored_eus,
                    "scored_share": f(p.scored_share),
                    "mean_composite": f(p.mean_composite),
                    "mean_confidence": f(p.mean_confidence),
                }
                for p in self.parties
            ],
            "composite_gap": f(self.composite_gap),
            "scored_share_gap": f(self.scored_share_gap),
            "composite_gap_threshold": f(self.composite_gap_threshold),
            "scored_share_gap_threshold": f(self.scored_share_gap_threshold),
            "review_required": self.review_required,
            "review_reasons": self.review_reasons,
            "disclaimer": (
                "Scoring is provably party-blind (see the party-blindness guard); any gap "
                "here therefore originates in the world and the data, NOT in the formula. A "
                "flagged gap triggers a HUMAN review of metric/baseline choices — never an "
                "automated correction, and never a change to any individual score. "
                "'Insufficient evidence' is honest abstention, not a low score."
            ),
        }


def _official_party_abbrev(session: Session, official_id: int) -> str | None:
    """Resolve an official's party purely for AUDIT (never used by scoring).

    Uses the most recent office term that records a party. An official with no
    party-bearing term (e.g. a president ingested without one) returns None and is
    excluded from the partisan comparison rather than guessed at.
    """
    row = session.execute(
        select(Party.abbrev)
        .join(OfficeTerm, OfficeTerm.party_id == Party.id)
        .where(OfficeTerm.official_id == official_id)
        .order_by(OfficeTerm.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return row


def party_symmetry_report(
    session: Session,
    *,
    composite_gap_threshold: Decimal = DEFAULT_COMPOSITE_GAP_THRESHOLD,
    scored_share_gap_threshold: Decimal = DEFAULT_SHARE_GAP_THRESHOLD,
    min_scored: int = DEFAULT_MIN_SCORED,
) -> PartySymmetryReport:
    """Compute the party-level distribution of scored outcomes (PLAN.md §9.12).

    Attribution-weighted, consistent with the official-level roll-up (§8.5): each
    party's contribution to an EU is the summed attribution of that party's
    (non-residual) officials on the EU. Composites are averaged only over EUs that
    cleared the confidence gate; coverage (scored vs. attributed) is tracked separately
    so that abstention is never mistaken for a partisan score difference.
    """
    # Latest score per EU, cached so we hit the DB once per EU.
    score_cache: dict[int, EUScore | None] = {}

    def latest_score(eu_id: int) -> EUScore | None:
        if eu_id not in score_cache:
            run = session.execute(
                select(ScoreRun).where(ScoreRun.eu_id == eu_id).order_by(ScoreRun.id.desc()).limit(1)
            ).scalar_one_or_none()
            score_cache[eu_id] = (
                session.execute(
                    select(EUScore).where(EUScore.score_run_id == run.id)
                ).scalar_one_or_none()
                if run
                else None
            )
        return score_cache[eu_id]

    # Sum each party's attribution on each EU (a party can hold several edges on one EU,
    # e.g. many same-party legislators voting): party_eu_attr[(party, eu)] = Σ attribution.
    party_eu_attr: dict[tuple[str, int], Decimal] = {}
    party_cache: dict[int, str | None] = {}

    rows = session.execute(
        select(AttributionWeight).where(
            AttributionWeight.official_id.is_not(None),
            AttributionWeight.is_residual.is_(False),
        )
    ).scalars().all()
    for aw in rows:
        if aw.official_id not in party_cache:
            party_cache[aw.official_id] = _official_party_abbrev(session, aw.official_id)
        abbrev = party_cache[aw.official_id]
        if abbrev is None:
            continue
        key = (abbrev, aw.eu_id)
        party_eu_attr[key] = party_eu_attr.get(key, D(0)) + D(aw.attribution)

    # Aggregate per party.
    attributed: dict[str, set[int]] = {}
    scored: dict[str, set[int]] = {}
    comp_weight: dict[str, Decimal] = {}
    comp_weighted_sum: dict[str, Decimal] = {}
    conf_weighted_sum: dict[str, Decimal] = {}

    for (abbrev, eu_id), attr in party_eu_attr.items():
        attributed.setdefault(abbrev, set()).add(eu_id)
        score = latest_score(eu_id)
        if score is not None and not score.gated and score.composite is not None:
            scored.setdefault(abbrev, set()).add(eu_id)
            comp_weight[abbrev] = comp_weight.get(abbrev, D(0)) + attr
            comp_weighted_sum[abbrev] = comp_weighted_sum.get(abbrev, D(0)) + attr * D(score.composite)
            if score.confidence is not None:
                conf_weighted_sum[abbrev] = conf_weighted_sum.get(abbrev, D(0)) + attr * D(score.confidence)

    parties: list[PartyStats] = []
    for abbrev in sorted(attributed):
        n_attr = len(attributed[abbrev])
        n_scored = len(scored.get(abbrev, set()))
        weight = comp_weight.get(abbrev, D(0))
        mean_comp = q_score(comp_weighted_sum[abbrev] / weight) if weight > 0 else None
        mean_conf = q_score(conf_weighted_sum[abbrev] / weight) if weight > 0 and abbrev in conf_weighted_sum else None
        parties.append(PartyStats(
            abbrev=abbrev,
            attributed_eus=n_attr,
            scored_eus=n_scored,
            scored_share=q_score(D(n_scored) / D(n_attr)) if n_attr else q_score(D(0)),
            mean_composite=mean_comp,
            mean_confidence=mean_conf,
        ))

    report = PartySymmetryReport(
        parties=parties,
        composite_gap_threshold=composite_gap_threshold,
        scored_share_gap_threshold=scored_share_gap_threshold,
    )

    # Composite gap: only across parties with enough scored EUs to be worth comparing.
    comparable = [p.mean_composite for p in parties
                  if p.mean_composite is not None and p.scored_eus >= min_scored]
    if len(comparable) >= 2:
        report.composite_gap = q_score(max(comparable) - min(comparable))
        if report.composite_gap > composite_gap_threshold:
            report.review_required = True
            report.review_reasons.append(
                f"Mean-composite gap {report.composite_gap} exceeds the review threshold "
                f"{composite_gap_threshold} (points). Review whether metric/baseline choices "
                f"systematically favour one party's policy types — do NOT adjust scores."
            )

    # Scored-share gap: across parties with any attributed EUs.
    shares = [p.scored_share for p in parties if p.attributed_eus > 0]
    if len(shares) >= 2:
        report.scored_share_gap = q_score(max(shares) - min(shares))
        if report.scored_share_gap > scored_share_gap_threshold:
            report.review_required = True
            report.review_reasons.append(
                f"Scored-share gap {report.scored_share_gap} exceeds the review threshold "
                f"{scored_share_gap_threshold}. One party's actions clear the evidence gate far "
                f"more often; review data availability/design eligibility for symmetric coverage."
            )

    return report
