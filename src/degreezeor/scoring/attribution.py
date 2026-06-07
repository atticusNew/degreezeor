"""Attribution (PLAN.md §7).

Attribution = f(formal_authority, pivotality, implementation_control), reported as
an interval, and ALWAYS leaving a large explicit ``unattributable residual`` so no
human is ever assigned credit/blame for a macro outcome. Channels are pluggable;
the slice ships sponsor + signer + decisive-vote.
"""

from __future__ import annotations

from dataclasses import dataclass

from degreezeor.core.interfaces import (
    ATTRIBUTION_CHANNELS,
    AttributionChannel,
    AttributionContext,
    AttributionContribution,
)
from degreezeor.core.numeric import D, clamp01, q_score

# A single legislative act is a small lever on an economy-wide metric. We cap the
# total human-attributable share so the structural/exogenous residual stays large.
RESIDUAL_FLOOR = D("0.30")
MAX_HUMAN_TOTAL = D("0.70")


class SponsorChannel(AttributionChannel):
    name = "sponsor"

    def contributions(self, ctx: AttributionContext) -> list[AttributionContribution]:
        if ctx.sponsor_official_id is None:
            return []
        return [
            AttributionContribution(
                official_id=ctx.sponsor_official_id,
                role="sponsor",
                authority=D("0.15"),
                pivotality=D("1.0"),
                raw_weight=D("0.15"),
                raw_low=D("0.10"),
                raw_high=D("0.25"),
            )
        ]


class SignerChannel(AttributionChannel):
    name = "signer"

    def contributions(self, ctx: AttributionContext) -> list[AttributionContribution]:
        if ctx.signer_official_id is None:
            return []
        # Formal authority depends on the instrument (PLAN.md §7): signing a law that
        # Congress passed is shared authority (~0.15); directing an executive order or
        # regulation is a unilateral executive act, so the signer's authority is high.
        if ctx.action_type in ("eo", "regulation"):
            authority, lo, hi = D("0.60"), D("0.40"), D("0.85")
        else:
            authority, lo, hi = D("0.15"), D("0.10"), D("0.25")
        return [
            AttributionContribution(
                official_id=ctx.signer_official_id,
                role="signer",
                authority=authority,
                pivotality=D("1.0"),
                raw_weight=authority,
                raw_low=lo,
                raw_high=hi,
            )
        ]


def pivotality_from_margin(winning_margin: int) -> D:
    """Decisiveness of a single vote: high only when the margin is razor-thin.

    Approximates the probability that one member is pivotal ~ 1/(margin+1).
    Lopsided votes -> ~0; one-vote margins -> ~0.5; ties -> 1.
    """
    if winning_margin <= 0:
        return D("1.0")
    return clamp01(D(1) / (D(winning_margin) + D(1)))


class DecisiveVoteChannel(AttributionChannel):
    name = "decisive_vote"

    def contributions(self, ctx: AttributionContext) -> list[AttributionContribution]:
        if (
            ctx.vote_margin is None
            or not ctx.member_on_winning_side
            or not ctx.decisive_official_ids
        ):
            return []
        piv = pivotality_from_margin(ctx.vote_margin)
        base_authority = D("0.05")
        out = []
        # Sorted for deterministic ordering => bit-reproducible score runs regardless of
        # whether IDs came from XML parse order or a DB query.
        for oid in sorted(ctx.decisive_official_ids):
            w = base_authority * piv
            out.append(
                AttributionContribution(
                    official_id=oid,
                    role="decisive_vote",
                    authority=base_authority,
                    pivotality=piv,
                    raw_weight=q_score(w),
                    raw_low=q_score(w * D("0.5")),
                    raw_high=q_score(w * D("1.5")),
                )
            )
        return out


sponsor_channel = ATTRIBUTION_CHANNELS.register(SponsorChannel())
signer_channel = ATTRIBUTION_CHANNELS.register(SignerChannel())
decisive_channel = ATTRIBUTION_CHANNELS.register(DecisiveVoteChannel())


@dataclass(frozen=True)
class NormalizedAttribution:
    official_id: int | None
    role: str
    authority: object
    pivotality: object
    attribution: object
    attr_ci_low: object
    attr_ci_high: object
    is_residual: bool


def normalize(contributions: list[AttributionContribution]) -> list[NormalizedAttribution]:
    """Normalize raw weights into attributions + an explicit residual summing to 1."""
    total = sum((c.raw_weight for c in contributions), D(0))
    scale = D(1)
    if total > MAX_HUMAN_TOTAL and total > 0:
        scale = MAX_HUMAN_TOTAL / total
        total = MAX_HUMAN_TOTAL

    out: list[NormalizedAttribution] = []
    for c in contributions:
        out.append(
            NormalizedAttribution(
                official_id=c.official_id,
                role=c.role,
                authority=c.authority,
                pivotality=c.pivotality,
                attribution=q_score(c.raw_weight * scale),
                attr_ci_low=q_score(c.raw_low * scale),
                attr_ci_high=q_score(c.raw_high * scale),
                is_residual=False,
            )
        )
    residual = clamp01(D(1) - total)
    out.append(
        NormalizedAttribution(
            official_id=None,
            role="unattributable_residual",
            authority=D(0),
            pivotality=D(0),
            attribution=q_score(residual),
            attr_ci_low=q_score(residual),
            attr_ci_high=q_score(residual),
            is_residual=True,
        )
    )
    return out


def build_attribution(ctx: AttributionContext) -> list[NormalizedAttribution]:
    contribs: list[AttributionContribution] = []
    for channel in ATTRIBUTION_CHANNELS.all():
        contribs.extend(channel.contributions(ctx))
    return normalize(contribs)
