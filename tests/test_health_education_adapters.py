"""Vetted health (CDC overdose) + education (NAEP) state outcome adapters and their
wiring into the comparison-design scoring path (offline). Live end-to-end scoring +
reproducibility for the new policy specs is exercised by the DZ_RUN_LIVE tests."""

from __future__ import annotations

from degreezeor.categories import category_for
from degreezeor.ingestion.adapters.cdc import cdc_adapter
from degreezeor.ingestion.adapters.naep import naep_adapter
from degreezeor.ingestion.adapters.naep import parse_encoding as naep_enc
from degreezeor.pipeline import STATE_METRIC_KINDS, STATE_POLICIES, state_series_id


def test_naep_encoding_and_parse() -> None:
    nsid = "NAEP|reading|4|RRPCM|MS"
    assert naep_enc(nsid) == ("reading", "4", "RRPCM", "MS")
    content = (b'{"status":200,"result":['
               b'{"year":2013,"jurisdiction":"MS","value":208.5,"errorFlag":0},'
               b'{"year":2019,"jurisdiction":"MS","value":219.3,"errorFlag":0},'
               b'{"year":2099,"jurisdiction":"MS","value":null,"errorFlag":0},'
               b'{"year":2098,"jurisdiction":"MS","value":1.0,"errorFlag":1}]}')
    series = naep_adapter.parse_series(content, nsid)
    assert series == [("2013", "208.5"), ("2019", "219.3")]  # null + flagged dropped, sorted


def test_cdc_state_overdose_parse() -> None:
    # The CDC adapter already supports per-state filters via the encoding.
    nsid = "CDC|44rk-q6r2|year|ageadjrate|state=Florida;sex=Both Sexes;age=All Ages;race=All Races-All Origins"
    content = (b'[{"year":"2013","ageadjrate":"9.0"},'
               b'{"year":"2011","ageadjrate":"11.5"},'
               b'{"year":"2012","ageadjrate":null}]')
    series = cdc_adapter.parse_series(content, nsid)
    assert series == [("2011", "11.5"), ("2013", "9.0")]  # null dropped, sorted


def test_new_metric_kinds_resolve_to_official_series() -> None:
    assert "overdose" in STATE_METRIC_KINDS and "naep_reading4" in STATE_METRIC_KINDS
    od = state_series_id("12", "overdose")
    assert od.startswith("CDC|44rk-q6r2|year|ageadjrate|state=Florida")
    naep = state_series_id("28", "naep_reading4")
    assert naep == "NAEP|reading|4|RRPCM|MS"
    # Direction-of-good is fixed and correct (fewer deaths good; higher scores good).
    assert STATE_METRIC_KINDS["overdose"]["sign"] == -1
    assert STATE_METRIC_KINDS["naep_reading4"]["sign"] == 1


def test_new_specs_map_to_new_categories() -> None:
    fl = STATE_POLICIES["FL-2011-PILLMILL"]
    ms = STATE_POLICIES["MS-2013-LBPA"]
    assert category_for(STATE_METRIC_KINDS[fl.metric_kind]["domain"]) == "health"
    assert category_for(STATE_METRIC_KINDS[ms.metric_kind]["domain"]) == "education"
