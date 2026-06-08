"""Structural-integrity guards for the curated comparison-design specs.

These never touch the network; they assert each curated state-policy spec is
internally well-formed so a malformed entry (e.g. the treated state accidentally
left in its own donor pool, a bad FIPS, or a missing attribution anchor) is caught
before it reaches the scorer. The synthetic-control pre-fit gate still decides which
specs actually clear the confidence gate vs. honestly abstain.
"""

from __future__ import annotations

from datetime import date

import pytest

from degreezeor.pipeline import STATE_POLICIES


@pytest.mark.parametrize("key", list(STATE_POLICIES))
def test_state_policy_spec_well_formed(key: str) -> None:
    spec = STATE_POLICIES[key]
    assert spec.key == key
    # FIPS are two-digit strings; the treated state is never in its own donor pool.
    assert len(spec.state_fips) == 2 and spec.state_fips.isdigit()
    assert spec.donor_fips, "a comparison design needs at least one donor state"
    assert all(len(f) == 2 and f.isdigit() for f in spec.donor_fips)
    assert spec.state_fips not in spec.donor_fips
    assert len(spec.donor_fips) == len(set(spec.donor_fips)), "duplicate donor states"
    # Dates are real and the objective + Tier-0 source are present.
    assert 1 <= spec.enacted_month <= 12
    assert 2000 <= spec.enacted_year <= date.today().year
    assert spec.source_url.startswith("http")
    assert spec.objective_text.strip()
    # Every policy has an attribution anchor: a signer, a sponsor, or both.
    assert spec.signer_name or spec.sponsor_name, "no one to attribute the action to"


def test_state_policy_keys_unique() -> None:
    assert len(STATE_POLICIES) == len(set(STATE_POLICIES))
