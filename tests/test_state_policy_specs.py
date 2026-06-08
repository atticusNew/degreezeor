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
    # The comparison-design metric kind is one we know how to resolve to an official series.
    from degreezeor.pipeline import STATE_METRIC_KINDS
    assert spec.metric_kind in STATE_METRIC_KINDS


def test_state_series_ids_are_well_formed() -> None:
    from degreezeor.pipeline import state_series_id

    # BLS SM series are 20 chars: SM + seasonal + FIPS(2) + area(5) + industry(8) + datatype(2).
    emp = state_series_id("06", "employment")
    wage = state_series_id("06", "wage")
    assert emp == "SMS06000000000000001" and len(emp) == 20
    assert wage == "SMU06000000500000003" and len(wage) == 20
    # Census/EIA kinds resolve to their adapter-prefixed, per-state series ids.
    assert state_series_id("06", "poverty") == "CENSUS|timeseries/poverty/saipe|SAEPOVRTALL_PT|state:06"
    assert state_series_id("06", "income") == "CENSUS|timeseries/poverty/saipe|SAEMHI_PT|state:06"
    assert state_series_id("06", "uninsured") == "CENSUS|timeseries/healthins/sahie|PCTUI_PT|state:06"
    assert state_series_id("06", "energy") == (
        "EIA|co2-emissions/co2-emissions-aggregates|stateId=CA;sectorId=TT;fuelId=TO")


def test_state_policy_keys_unique() -> None:
    assert len(STATE_POLICIES) == len(set(STATE_POLICIES))
