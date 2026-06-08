"""Census (socioeconomic) + EIA (energy) outcome adapters + cross-domain mapping (offline)."""

from __future__ import annotations

from degreezeor.ingestion.adapters.census import census_adapter
from degreezeor.ingestion.adapters.census import parse_encoding as census_enc
from degreezeor.ingestion.adapters.eia import eia_adapter
from degreezeor.ingestion.adapters.eia import parse_encoding as eia_enc
from degreezeor.scoring.catalog import BY_CODE
from degreezeor.scoring.objective import select_metrics


def test_census_encoding_and_parse() -> None:
    nsid = "CENSUS|timeseries/poverty/saipe|SAEPOVRTALL_PT"
    assert census_enc(nsid) == ("timeseries/poverty/saipe", "SAEPOVRTALL_PT")
    content = b'[["SAEPOVRTALL_PT","time","us"],["12.5","2003","00"],["11.3","2000","00"],["x","2001","00"]]'
    series = census_adapter.parse_series(content, nsid)
    assert series == [("2000", "11.3"), ("2001", "x"), ("2003", "12.5")]  # sorted by year


def test_eia_encoding_and_parse() -> None:
    nsid = "EIA|total-energy|TETCEUS"
    assert eia_enc(nsid) == ("total-energy", "TETCEUS")
    content = (b'{"response":{"data":['
               b'{"period":"2001","value":5930.1},{"period":"2000","value":6007.5},'
               b'{"period":"2002","value":null}]}}')
    series = eia_adapter.parse_series(content, nsid)
    assert series == [("2000", "6007.5"), ("2001", "5930.1")]  # null dropped, sorted


def test_poverty_and_income_map_cross_domain() -> None:
    pov, _ = select_metrics("Reduce poverty and lift families out of poverty", "Economics and Public Finance")
    assert pov.spec.code == "poverty_rate" and pov.spec.source_name == "Census"
    inc, _ = select_metrics("Raise median household income for the middle class", "Economics and Public Finance")
    assert inc.spec.code == "median_household_income"


def test_carbon_objective_maps_to_eia() -> None:
    co2, _ = select_metrics("Cut carbon emissions and greenhouse gas to fight climate change", "Health")
    assert co2.spec.code == "energy_co2_emissions" and co2.spec.source_name == "EIA"


def test_catalog_directions() -> None:
    assert BY_CODE["poverty_rate"].direction_good == "down"
    assert BY_CODE["median_household_income"].direction_good == "up"
    assert BY_CODE["energy_co2_emissions"].direction_good == "down"
    assert BY_CODE["poverty_rate"].native_series_id.startswith("CENSUS|")
    assert BY_CODE["energy_co2_emissions"].native_series_id.startswith("EIA|")
