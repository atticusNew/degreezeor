"""CDC health-outcome adapter + cross-domain objective→metric mapping (offline)."""

from __future__ import annotations

from degreezeor.ingestion.adapters.cdc import cdc_adapter, parse_encoding
from degreezeor.scoring.catalog import BY_CODE
from degreezeor.scoring.objective import select_metrics

_NSID = "CDC|w9j2-ggv5|year|average_life_expectancy|race=All Races;sex=Both Sexes"


def test_parse_encoding() -> None:
    resource, year_field, value_field, filters = parse_encoding(_NSID)
    assert resource == "w9j2-ggv5"
    assert year_field == "year" and value_field == "average_life_expectancy"
    assert filters == {"race": "All Races", "sex": "Both Sexes"}


def test_parse_series_skips_nulls_and_sorts() -> None:
    content = (
        b'[{"year":"2002","average_life_expectancy":"77.0"},'
        b' {"year":"2000","average_life_expectancy":"76.8"},'
        b' {"year":"2001","average_life_expectancy":null},'
        b' {"year":"2003"}]'
    )
    series = cdc_adapter.parse_series(content, _NSID)
    assert series == [("2000", "76.8"), ("2002", "77.0")]  # null/missing dropped, sorted


def test_health_objective_maps_to_cdc_metric_cross_domain() -> None:
    # An action crudely tagged with the economic domain must still reach a health metric
    # when its objective carries health keywords (catalog is cross-domain).
    primary, _side = select_metrics(
        "Reduce premature death and improve life expectancy", "Economics and Public Finance"
    )
    assert primary is not None
    assert primary.spec.source_name == "CDC"
    assert primary.spec.code in {"life_expectancy", "age_adjusted_death_rate"}


def test_economic_objective_still_maps_to_bls_not_health() -> None:
    # Cross-domain matching must not pull economic objectives into health metrics.
    primary, _side = select_metrics("Create jobs and reduce unemployment", "Economics and Public Finance")
    assert primary is not None
    assert primary.spec.source_name == "BLS"


def test_catalog_has_cdc_health_metrics() -> None:
    assert BY_CODE["life_expectancy"].direction_good == "up"
    assert BY_CODE["age_adjusted_death_rate"].direction_good == "down"
    assert BY_CODE["life_expectancy"].native_series_id.startswith("CDC|")
