"""End-to-end scoring pipeline for one enacted law (PLAN.md §12).

Order is significant for neutrality:
  ingest law + objective  ->  select metric (party-masked, objective-only)
  ->  PRE-REGISTER (hash to audit)  ->  ingest outcome series  ->  compute outcome
  ->  attribution  ->  confidence + gate  ->  pinned, reproducible score run.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from degreezeor.config import settings
from degreezeor.core import audit
from degreezeor.core.hashing import canonical_json, hash_payload
from degreezeor.core.interfaces import AttributionContext
from degreezeor.core.models import (
    Action,
    AttributionWeight,
    Baseline,
    Bill,
    BillCosponsor,
    ConfidenceInterval,
    DataSource,
    EUScore,
    EvaluationUnit,
    ExecutiveOrder,
    Jurisdiction,
    Law,
    MethodologyVersion,
    Metric,
    Objective,
    Observation,
    Official,
    OutcomeResult,
    ScoreComponent,
    ScoreRun,
    Vote,
    VotePosition,
)
from degreezeor.core.numeric import D, q_score
from degreezeor.ingestion.adapters.bls import bls_adapter
from degreezeor.ingestion.adapters.generic import generic_url_adapter
from degreezeor.ingestion.landing import ensure_source, land
from degreezeor.ingestion.loader import (
    ensure_bls_source,
    load_executive_order,
    load_house_final_passage_vote,
    load_law,
    load_observations,
    load_regulation,
    load_senate_final_passage_vote,
)
from degreezeor.provenance import current_git_sha, data_snapshot_id
from degreezeor.scoring.attribution import build_attribution
from degreezeor.scoring.baseline import split_series  # noqa: F401  (ensures registration import)
from degreezeor.scoring.confidence import best_design, compute_confidence
from degreezeor.scoring.objective import (
    ensure_metric,
    mask_party_and_name,
    select_metrics,
    sign_goal_for,
)
from degreezeor.scoring.outcome import compute_outcome, s_outcome_from_z
from degreezeor.scoring.prereg import preregister
from degreezeor.scoring.score import assemble_score
from degreezeor.scoring.sensitivity import analyze_lag_sensitivity

log = logging.getLogger("degreezeor.pipeline")


def state_employment_series_id(fips: str) -> str:
    """BLS state total-nonfarm-employment series (SA): SMS + FIPS(2) + 15-char suffix."""
    return f"SMS{fips}000000000000001"


def state_wage_series_id(fips: str) -> str:
    """BLS state average hourly earnings, total private (NSA): SMU + FIPS(2) + area(00000)
    + industry(05000000 = total private) + datatype(03 = avg hourly earnings, all employees).
    Seasonally adjusted AHE is not published at the state level, so the not-seasonally-adjusted
    series is used; the synthetic-control donor pool matches the shared seasonal path across
    states, so a treated-vs-synthetic comparison at matched months is still valid."""
    return f"SMU{fips}000000500000003"


# FIPS -> USPS abbreviation (EIA state series key on the postal abbreviation, not FIPS).
_FIPS_USPS: dict[str, str] = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT",
    "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL",
    "18": "IN", "19": "IA", "20": "KS", "21": "KY", "22": "LA", "23": "ME", "24": "MD",
    "25": "MA", "26": "MI", "27": "MN", "28": "MS", "29": "MO", "30": "MT", "31": "NE",
    "32": "NV", "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND",
    "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
    "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA", "54": "WV",
    "55": "WI", "56": "WY",
}

# Descriptor per comparison-design ``metric_kind``: the official state series and which way
# is "toward" the policy's own stated goal. ``sign`` is fixed at pre-registration.
STATE_METRIC_KINDS: dict[str, dict[str, object]] = {
    "employment": {"code": "state_nonfarm_employment", "name": "Total Nonfarm Employment (SA)",
                   "unit": "thousands of jobs", "direction_good": "up", "sign": 1,
                   "domain": "Economics and Public Finance"},
    "wage": {"code": "state_avg_hourly_earnings", "name": "Average Hourly Earnings, Total Private (NSA)",
             "unit": "dollars/hour", "direction_good": "up", "sign": 1,
             "domain": "Economics and Public Finance"},
    "poverty": {"code": "state_poverty_rate", "name": "Poverty Rate, All Ages (Census SAIPE)",
                "unit": "percent", "direction_good": "down", "sign": -1, "domain": "Income and Poverty"},
    "income": {"code": "state_median_household_income", "name": "Median Household Income (Census SAIPE)",
               "unit": "dollars", "direction_good": "up", "sign": 1, "domain": "Income and Poverty"},
    "uninsured": {"code": "state_uninsured_rate", "name": "Uninsured Rate, All Ages (Census SAHIE)",
                  "unit": "percent", "direction_good": "down", "sign": -1, "domain": "Health"},
    "energy": {"code": "state_co2_emissions", "name": "Total Energy CO2 Emissions (EIA)",
               "unit": "million metric tons CO2", "direction_good": "down", "sign": -1,
               "domain": "Energy and Environment"},
    "child_poverty": {"code": "state_child_poverty_rate",
                      "name": "Child Poverty Rate, Under 18 (Census SAIPE)",
                      "unit": "percent", "direction_good": "down", "sign": -1,
                      "domain": "Income and Poverty"},
}
_ANNUAL_KINDS = {"poverty", "income", "uninsured", "energy", "child_poverty"}


def state_series_id(fips: str, metric_kind: str = "employment") -> str:
    """Resolve the official state series id for a comparison-design metric kind
    (BLS for jobs/wages; Census SAIPE/SAHIE for poverty/income/uninsured; EIA for CO2)."""
    if metric_kind == "wage":
        return state_wage_series_id(fips)
    if metric_kind == "poverty":
        return f"CENSUS|timeseries/poverty/saipe|SAEPOVRTALL_PT|state:{fips}"
    if metric_kind == "income":
        return f"CENSUS|timeseries/poverty/saipe|SAEMHI_PT|state:{fips}"
    if metric_kind == "child_poverty":
        return f"CENSUS|timeseries/poverty/saipe|SAEPOVRTUNDER18_PT|state:{fips}"
    if metric_kind == "uninsured":
        return f"CENSUS|timeseries/healthins/sahie|PCTUI_PT|state:{fips}"
    if metric_kind == "energy":
        return f"EIA|co2-emissions/co2-emissions-aggregates|stateId={_FIPS_USPS.get(fips, fips)};sectorId=TT;fuelId=TO"
    return state_employment_series_id(fips)


def _series_is_annual(native_series_id: str | None) -> bool:
    return bool(native_series_id) and native_series_id.startswith(("CENSUS|", "EIA|", "CDC|"))


def _pre_years_for(native_series_id: str | None) -> int:
    """Pre-period span to pull. Annual official series need a longer pre-window than monthly
    BLS series to give the comparison-design pre-fit enough points (>= 6)."""
    return 12 if _series_is_annual(native_series_id) else 3


def _fetch_state_series_points(native_series_id: str, start_year: int, end_year: int):
    """Fetch one official state series via the right adapter; return (RawFetch, points) where
    points are [(ISO-date, value)] (monthly -> YYYY-MM-01; annual -> YYYY-01-01)."""
    from degreezeor.ingestion.adapters.census import census_adapter
    from degreezeor.ingestion.adapters.eia import eia_adapter

    if native_series_id.startswith("CENSUS|"):
        f = census_adapter.fetch(native_series_id, start_year=start_year, end_year=end_year)
        pts = [(f"{y}-01-01", v) for y, v in census_adapter.parse_series(f.content, native_series_id)]
        return f, pts
    if native_series_id.startswith("EIA|"):
        f = eia_adapter.fetch(native_series_id, start_year=start_year, end_year=end_year)
        pts = [(f"{y}-01-01", v) for y, v in eia_adapter.parse_series(f.content, native_series_id)]
        return f, pts
    f = bls_adapter.fetch(native_series_id, start_year=start_year, end_year=end_year)
    series = json.loads(f.content)["Results"]["series"][0]
    pts = [(f"{pt['year']}-{int(pt['period'][1:]):02d}-01", pt["value"])
           for pt in series["data"] if pt["period"].startswith("M")]
    return f, pts


@dataclass
class StatePolicySpec:
    key: str  # e.g. "KS-HB2117"
    title: str
    state_fips: str
    state_name: str
    donor_fips: list[str]
    source_url: str  # official state legislature URL (Tier-0 provenance)
    objective_text: str
    enacted_year: int
    enacted_month: int
    lag_window_months: int = 24
    signer_name: str | None = None
    sponsor_name: str | None = None
    signer_party: str | None = None  # public record; audit metadata only
    sponsor_party: str | None = None
    # Comparison-design outcome metric: "employment" (jobs) or "wage" (avg hourly earnings).
    metric_kind: str = "employment"


# Documented demo state policies (public record). The objective text states the
# policy's OWN goal verbatim-in-spirit; the system then measures against it neutrally.
STATE_POLICIES: dict[str, StatePolicySpec] = {
    "KS-HB2117": StatePolicySpec(
        key="KS-HB2117",
        title="Kansas 2012 income tax cuts (HB 2117)",
        state_fips="20",
        state_name="Kansas",
        # Regional comparison pool of states that did not enact comparable 2012 cuts.
        donor_fips=["31", "29", "40", "19", "05", "08", "27", "46"],  # NE MO OK IA AR CO MN SD
        source_url="http://www.kslegislature.org/li_2012/b2011_12/measures/hb2117/",
        objective_text=(
            "Reduce individual income tax rates and exempt certain business income in order to "
            "grow the Kansas economy and create jobs and employment."
        ),
        enacted_year=2012,
        enacted_month=5,
        # 48-month horizon: a structural income-tax cut's job-creation claim is
        # appropriately evaluated over a multi-year window, declared at pre-registration.
        lag_window_months=48,
        signer_name="Sam Brownback", signer_party="R",
    ),
    "NC-2013-TAX": StatePolicySpec(
        key="NC-2013-TAX",
        title="North Carolina 2013 tax reform",
        state_fips="37",
        state_name="North Carolina",
        # Regional comparison pool (Southeast/border states).
        donor_fips=["45", "51", "13", "47", "21", "29", "01"],  # SC VA GA TN KY MO AL
        source_url="https://www.ncleg.gov/Sessions/2013/Bills/House/PDF/H998v7.pdf",
        objective_text=(
            "Lower and simplify income tax rates to grow the North Carolina economy and "
            "create jobs and employment."
        ),
        enacted_year=2013, enacted_month=7, lag_window_months=48,
        signer_name="Pat McCrory", signer_party="R",
    ),
    "WI-2011-ACT32": StatePolicySpec(
        key="WI-2011-ACT32",
        title="Wisconsin 2011 budget and tax cuts (Act 32)",
        state_fips="55",
        state_name="Wisconsin",
        # Comparable industrial/Midwest states without comparable 2011 income-tax cuts.
        donor_fips=["27", "17", "19", "26", "42"],  # MN IL IA MI PA
        source_url="https://docs.legis.wisconsin.gov/2011/related/acts/32",
        objective_text=(
            "Cut taxes and restrain spending to grow Wisconsin's economy and create jobs "
            "(the administration's stated goal of 250,000 private-sector jobs)."
        ),
        enacted_year=2011, enacted_month=6, lag_window_months=48,
        signer_name="Scott Walker", signer_party="R",
    ),
    "ME-2011-TAX": StatePolicySpec(
        key="ME-2011-TAX",
        title="Maine 2011 tax cuts (LD 1043 / PL 2011 c.380)",
        state_fips="23",
        state_name="Maine",
        # New England + New York comparison pool without comparable 2011 income-tax cuts.
        donor_fips=["33", "50", "25", "09", "44", "36"],  # NH VT MA CT RI NY
        source_url="https://legislature.maine.gov/LawMakerWeb/summary.asp?SessionID=9&paper=HP0778",
        objective_text=(
            "Reduce Maine's top individual income tax rate (8.5% to 7.95%) to provide tax relief "
            "and grow the state economy."
        ),
        enacted_year=2011, enacted_month=6, lag_window_months=48,
        signer_name="Paul LePage", signer_party="R",
    ),
    "IN-2013-HEA1001": StatePolicySpec(
        key="IN-2013-HEA1001",
        title="Indiana 2013 income tax cut (HEA 1001)",
        state_fips="18",
        state_name="Indiana",
        # Midwest/border states without a comparable 2013 income-tax cut.
        donor_fips=["17", "26", "27", "42", "19"],  # IL MI MN PA IA
        source_url="http://iga.in.gov/legislative/2013/bills/house/1001",
        objective_text=(
            "Reduce the individual income tax rate (3.4% to 3.23%) and cut other taxes to grow "
            "Indiana's economy and create jobs (the administration's stated jobs budget)."
        ),
        enacted_year=2013, enacted_month=5, lag_window_months=48,
        signer_name="Mike Pence", signer_party="R",
    ),
    "OH-2013-HB59": StatePolicySpec(
        key="OH-2013-HB59",
        title="Ohio 2013 income tax cut (HB 59 budget)",
        state_fips="39",
        state_name="Ohio",
        # Regional states without a comparable 2013 income-tax cut.
        donor_fips=["42", "26", "17", "27", "21"],  # PA MI IL MN KY
        source_url="https://search-prod.lis.state.oh.us/api/v2/general_assembly_130/legislation/hb59",
        objective_text=(
            "Cut individual income tax rates by 10% over three years and deduct small-business "
            "income to create a more job-friendly tax climate and spur job creation in Ohio."
        ),
        enacted_year=2013, enacted_month=6, lag_window_months=48,
        signer_name="John Kasich", signer_party="R",
    ),
    "MO-2014-SB509": StatePolicySpec(
        key="MO-2014-SB509",
        title="Missouri 2014 income tax cut (SB 509)",
        state_fips="29",
        state_name="Missouri",
        # Regional states without a comparable 2014 income-tax cut.
        donor_fips=["17", "19", "21", "31", "27"],  # IL IA KY NE MN
        source_url="https://senate.mo.gov/14info/BTS_Web/Bill.aspx?SessionType=R&BillID=27723520",
        objective_text=(
            "Phase in a cut to the top individual income tax rate (6% to 5.5%) and a business-income "
            "deduction to stimulate Missouri's economy, help small businesses grow, and create jobs."
        ),
        # Enacted over the governor's veto; credit goes to the bill's sponsor, not a signer.
        enacted_year=2014, enacted_month=5, lag_window_months=48,
        sponsor_name="Will Kraus", sponsor_party="R",
    ),
    "MI-2011-HB4361": StatePolicySpec(
        key="MI-2011-HB4361",
        title="Michigan 2011 business tax overhaul (HB 4361, PA 38)",
        state_fips="26",
        state_name="Michigan",
        # Midwest states without a comparable 2011 business/income-tax overhaul.
        donor_fips=["17", "27", "42", "19", "21"],  # IL MN PA IA KY
        source_url="https://www.legislature.mi.gov/Bills/Bill?ObjectName=2011-HB-4361",
        objective_text=(
            "Replace the Michigan Business Tax with a simpler, lower 6% corporate income tax to "
            "improve the business climate and create jobs."
        ),
        enacted_year=2011, enacted_month=5, lag_window_months=48,
        signer_name="Rick Snyder", signer_party="R",
    ),
    "UT-2022-SB59": StatePolicySpec(
        key="UT-2022-SB59",
        title="Utah 2022 income tax cut (SB 59)",
        state_fips="49",
        state_name="Utah",
        donor_fips=["16", "56", "32", "30", "35"],  # ID WY NV MT NM
        source_url="https://le.utah.gov/~2022/bills/static/SB0059.html",
        objective_text=(
            "Cut the individual and corporate income tax rate (4.95% to 4.85%) to return money to "
            "families and support the economy."
        ),
        enacted_year=2022, enacted_month=2, lag_window_months=36,
        signer_name="Spencer Cox", signer_party="R",
    ),
    "KY-2022-HB8": StatePolicySpec(
        key="KY-2022-HB8",
        title="Kentucky 2022 income tax cut (HB 8)",
        state_fips="21",
        state_name="Kentucky",
        donor_fips=["47", "54", "39", "29", "18"],  # TN WV OH MO IN
        source_url="https://apps.legislature.ky.gov/record/22rs/hb8.html",
        objective_text=(
            "Cut the individual income tax rate (5% to 4.5%, with triggers toward elimination) to "
            "grow Kentucky's economy and competitiveness."
        ),
        # Enacted over the governor's veto; credit goes to the bill's sponsor, not a signer.
        enacted_year=2022, enacted_month=4, lag_window_months=36,
        sponsor_name="Jason Petrie", sponsor_party="R",
    ),
    "IA-2018-SF2417": StatePolicySpec(
        key="IA-2018-SF2417",
        title="Iowa 2018 income tax cut (SF 2417)",
        state_fips="19",
        state_name="Iowa",
        donor_fips=["27", "17", "31", "46", "21"],  # MN IL NE SD KY
        source_url="https://www.legis.iowa.gov/legislation/BillBook?ga=87&ba=SF2417",
        objective_text=(
            "Cut and simplify individual and corporate income tax rates to grow Iowa's economy "
            "and create jobs."
        ),
        enacted_year=2018, enacted_month=5, lag_window_months=48,
        signer_name="Kim Reynolds", signer_party="R",
    ),
    # --- Minimum-wage increases (metric_kind="wage"): the policy's own stated goal is to
    # raise workers' wages, measured against the official state average-hourly-earnings
    # series via synthetic control. Same neutral question, a different objective metric.
    "CA-2016-SB3": StatePolicySpec(
        key="CA-2016-SB3",
        title="California 2016 minimum wage increase (SB 3)",
        state_fips="06",
        state_name="California",
        # Large/diverse states that kept the federal minimum (no comparable $15 increase).
        donor_fips=["48", "42", "13", "37", "51"],  # TX PA GA NC VA
        source_url="https://leginfo.legislature.ca.gov/faces/billNavClient.xhtml?bill_id=201520160SB3",
        objective_text=(
            "Raise the statewide minimum wage to $15 per hour so that full-time work does not "
            "leave workers in poverty, raising wages for low-wage workers."
        ),
        enacted_year=2016, enacted_month=4, lag_window_months=48,
        signer_name="Edmund G. Brown Jr.", signer_party="D",
        metric_kind="wage",
    ),
    "MA-2018-H4640": StatePolicySpec(
        key="MA-2018-H4640",
        title="Massachusetts 2018 minimum wage increase (H 4640, grand bargain)",
        state_fips="25",
        state_name="Massachusetts",
        # Regional states without a comparable minimum-wage increase in the window.
        donor_fips=["33", "42", "51", "48", "37"],  # NH PA VA TX NC
        source_url="https://malegislature.gov/Bills/190/H4640",
        objective_text=(
            "Raise the state minimum wage from $11 to $15 per hour over five years to raise "
            "workers' wages."
        ),
        enacted_year=2018, enacted_month=6, lag_window_months=48,
        signer_name="Charlie Baker", signer_party="R",
        metric_kind="wage",
    ),
    "MD-2019-SB280": StatePolicySpec(
        key="MD-2019-SB280",
        title="Maryland 2019 minimum wage increase (SB 280, Fight for Fifteen)",
        state_fips="24",
        state_name="Maryland",
        # Regional states without a comparable minimum-wage increase in the window.
        donor_fips=["42", "37", "47", "13", "48"],  # PA NC TN GA TX
        source_url="https://mgaleg.maryland.gov/mgawebsite/Legislation/Details/sb0280?ys=2019RS",
        objective_text=(
            "Raise Maryland's minimum wage to $15 per hour (Fight for Fifteen) to raise wages "
            "for workers."
        ),
        # Enacted over the governor's veto; credit goes to the bill's sponsor, not a signer.
        enacted_year=2019, enacted_month=3, lag_window_months=48,
        sponsor_name="Cory McCray", sponsor_party="D",
        metric_kind="wage",
    ),
    "NJ-2019-A15": StatePolicySpec(
        key="NJ-2019-A15",
        title="New Jersey 2019 minimum wage increase (A15)",
        state_fips="34",
        state_name="New Jersey",
        # Regional states without a comparable minimum-wage increase in the window.
        donor_fips=["42", "37", "48", "13", "39"],  # PA NC TX GA OH
        source_url="https://www.njleg.state.nj.us/bill-search/2018/A15",
        objective_text=(
            "Raise the state minimum wage to $15 per hour to lift pay for low-wage workers."
        ),
        enacted_year=2019, enacted_month=2, lag_window_months=48,
        signer_name="Phil Murphy", signer_party="D",
        metric_kind="wage",
    ),
    "IL-2019-SB1": StatePolicySpec(
        key="IL-2019-SB1",
        title="Illinois 2019 minimum wage increase (SB 1, PA 101-0001)",
        state_fips="17",
        state_name="Illinois",
        donor_fips=["18", "55", "19", "21", "48"],  # IN WI IA KY TX
        source_url="https://www.ilga.gov/Legislation/PublicActs/View/101-0001",
        objective_text=(
            "Raise the state minimum wage to $15 per hour by 2025 to lift pay for low-wage workers "
            "(Lifting Up Illinois Working Families Act)."
        ),
        enacted_year=2019, enacted_month=2, lag_window_months=48,
        signer_name="J.B. Pritzker", signer_party="D",
        metric_kind="wage",
    ),
    "OR-2016-SB1532": StatePolicySpec(
        key="OR-2016-SB1532",
        title="Oregon 2016 minimum wage increase (SB 1532)",
        state_fips="41",
        state_name="Oregon",
        donor_fips=["16", "30", "49", "56", "48"],  # ID MT UT WY TX
        source_url="https://olis.oregonlegislature.gov/liz/2016R1/Measures/Overview/SB1532",
        objective_text=(
            "Raise Oregon's minimum wage on a tiered schedule through 2022 to lift pay for "
            "low-wage workers."
        ),
        enacted_year=2016, enacted_month=3, lag_window_months=48,
        signer_name="Kate Brown", signer_party="D",
        metric_kind="wage",
    ),
    "MN-2014-HF2091": StatePolicySpec(
        key="MN-2014-HF2091",
        title="Minnesota 2014 minimum wage increase (HF 2091)",
        state_fips="27",
        state_name="Minnesota",
        donor_fips=["55", "18", "38", "46", "21"],  # WI IN ND SD KY
        source_url="https://www.house.mn.gov/bills/billnum.asp?Bill=HF2091&ssn=0&y=2014",
        objective_text="Raise the state minimum wage (to $9.50 by 2016) to lift pay for low-wage workers.",
        enacted_year=2014, enacted_month=4, lag_window_months=48,
        signer_name="Mark Dayton", signer_party="D",
        metric_kind="wage",
    ),
    "NM-2019-SB437": StatePolicySpec(
        key="NM-2019-SB437",
        title="New Mexico 2019 minimum wage increase (SB 437)",
        state_fips="35",
        state_name="New Mexico",
        donor_fips=["48", "40", "20", "49", "28"],  # TX OK KS UT MS
        source_url="https://www.nmlegis.gov/Legislation/Legislation?chamber=S&legType=B&legNo=437&year=19",
        objective_text="Raise the statewide minimum wage to $12 per hour by 2023 to lift pay for low-wage workers.",
        enacted_year=2019, enacted_month=4, lag_window_months=36,
        signer_name="Michelle Lujan Grisham", signer_party="D",
        metric_kind="wage",
    ),
    "NV-2019-AB456": StatePolicySpec(
        key="NV-2019-AB456",
        title="Nevada 2019 minimum wage increase (AB 456)",
        state_fips="32",
        state_name="Nevada",
        donor_fips=["49", "16", "48", "40", "30"],  # UT ID TX OK MT
        source_url="https://www.leg.state.nv.us/App/NELIS/REL/80th2019/Bill/6730/Overview",
        objective_text="Raise the state minimum wage on an annual schedule (to $12 by 2024) to lift pay for low-wage workers.",
        enacted_year=2019, enacted_month=6, lag_window_months=36,
        signer_name="Steve Sisolak", signer_party="D",
        metric_kind="wage",
    ),
    # --- Poverty and income (metric_kind="poverty"): scored on the Census SAIPE state
    # poverty rate, where a fall is toward the policy's own stated anti-poverty goal. ---
    "CA-2015-SB80": StatePolicySpec(
        key="CA-2015-SB80",
        title="California Earned Income Tax Credit (CalEITC, SB 80)",
        state_fips="06",
        state_name="California",
        # Large states without a comparable refundable state EITC enacted in the window.
        donor_fips=["48", "12", "13", "37", "42"],  # TX FL GA NC PA
        source_url="https://leginfo.legislature.ca.gov/faces/billNavClient.xhtml?bill_id=201520160SB80",
        objective_text=(
            "Create a refundable California Earned Income Tax Credit whose stated statutory purpose "
            "is to reduce poverty among California's poorest working families and individuals."
        ),
        enacted_year=2015, enacted_month=6, lag_window_months=48,
        signer_name="Edmund G. Brown Jr.", signer_party="D",
        metric_kind="poverty",
    ),
    # --- Energy and environment (metric_kind="energy"): scored on EIA state CO2 emissions,
    # where a fall is toward the policy's own stated decarbonization goal. ---
    "CA-2015-SB350": StatePolicySpec(
        key="CA-2015-SB350",
        title="California 2015 Clean Energy and Pollution Reduction Act (SB 350)",
        state_fips="06",
        state_name="California",
        # Large states without a comparable 2015 clean-energy/emissions mandate.
        donor_fips=["48", "12", "39", "42", "13"],  # TX FL OH PA GA
        source_url="https://leginfo.legislature.ca.gov/faces/billNavClient.xhtml?bill_id=201520160SB350",
        objective_text=(
            "Cut greenhouse gas emissions by raising the renewable electricity share to 50% and "
            "doubling energy efficiency savings by 2030 (Clean Energy and Pollution Reduction Act)."
        ),
        enacted_year=2015, enacted_month=10, lag_window_months=48,
        signer_name="Edmund G. Brown Jr.", signer_party="D",
        metric_kind="energy",
    ),
    "VA-2020-VCEA": StatePolicySpec(
        key="VA-2020-VCEA",
        title="Virginia Clean Economy Act (HB 1526)",
        state_fips="51",
        state_name="Virginia",
        # Large states without a comparable 100% clean-energy / emissions mandate.
        donor_fips=["48", "12", "13", "37", "47"],  # TX FL GA NC TN
        source_url="https://lis.virginia.gov/cgi-bin/legp604.exe?201+sum+HB1526",
        objective_text=(
            "End carbon dioxide emissions from electricity and require 100% clean energy "
            "(Virginia Clean Economy Act)."
        ),
        enacted_year=2020, enacted_month=4, lag_window_months=24,
        signer_name="Ralph Northam", signer_party="D",
        metric_kind="energy",
    ),
    "NM-2019-SB489": StatePolicySpec(
        key="NM-2019-SB489",
        title="New Mexico 2019 Energy Transition Act (SB 489)",
        state_fips="35",
        state_name="New Mexico",
        donor_fips=["48", "40", "04", "49", "56"],  # TX OK AZ UT WY
        source_url="https://www.nmlegis.gov/Legislation/Legislation?chamber=S&legType=B&legNo=489&year=19",
        objective_text=(
            "Transition to carbon-free electricity (50% renewable by 2030, 100% carbon-free by "
            "2045) and cut power-sector emissions (Energy Transition Act)."
        ),
        enacted_year=2019, enacted_month=3, lag_window_months=36,
        signer_name="Michelle Lujan Grisham", signer_party="D",
        metric_kind="energy",
    ),
    "CO-2019-HB1261": StatePolicySpec(
        key="CO-2019-HB1261",
        title="Colorado 2019 Climate Action Plan (HB 19-1261)",
        state_fips="08",
        state_name="Colorado",
        donor_fips=["48", "40", "49", "56", "20"],  # TX OK UT WY KS
        source_url="https://leg.colorado.gov/bills/hb19-1261",
        objective_text=(
            "Cut statewide greenhouse gas emissions (at least 26% by 2025, 50% by 2030, 90% by "
            "2050 vs. 2005) (Climate Action Plan to Reduce Pollution)."
        ),
        enacted_year=2019, enacted_month=5, lag_window_months=36,
        signer_name="Jared Polis", signer_party="D",
        metric_kind="energy",
    ),
    "WA-2019-CETA": StatePolicySpec(
        key="WA-2019-CETA",
        title="Washington Clean Energy Transformation Act (SB 5116)",
        state_fips="53",
        state_name="Washington",
        # Western/other states without a comparable 100% clean-electricity mandate.
        donor_fips=["16", "30", "49", "56", "48"],  # ID MT UT WY TX
        source_url="https://app.leg.wa.gov/billsummary?BillNumber=5116&Year=2019",
        objective_text=(
            "Transition to a greenhouse-gas-free electricity supply by 2045 and cut power-sector "
            "emissions (Clean Energy Transformation Act)."
        ),
        enacted_year=2019, enacted_month=5, lag_window_months=36,
        signer_name="Jay Inslee", signer_party="D",
        metric_kind="energy",
    ),
    # --- Health (metric_kind="uninsured"): scored on the Census SAHIE state uninsured rate,
    # where a fall is toward the policy's own stated coverage goal. ---
    "CA-2014-MEDICAID": StatePolicySpec(
        key="CA-2014-MEDICAID",
        title="California Affordable Care Act Medicaid (Medi-Cal) expansion (ABX1 1)",
        state_fips="06",
        state_name="California",
        # Large states that did not expand Medicaid in 2014 (clean non-treated controls).
        donor_fips=["48", "12", "13", "37", "47"],  # TX FL GA NC TN
        source_url="https://leginfo.legislature.ca.gov/faces/billTextClient.xhtml?bill_id=201320141ABX11",
        objective_text=(
            "Expand Medicaid (Medi-Cal) eligibility under the Affordable Care Act to reduce the "
            "number of uninsured Californians."
        ),
        # Evaluated from the coverage effective date (Jan 1, 2014).
        enacted_year=2014, enacted_month=1, lag_window_months=36,
        signer_name="Edmund G. Brown Jr.", signer_party="D",
        metric_kind="uninsured",
    ),
    "LA-2016-MEDICAID": StatePolicySpec(
        key="LA-2016-MEDICAID",
        title="Louisiana Medicaid expansion (Executive Order JBE 16-01)",
        state_fips="22",
        state_name="Louisiana",
        # Southern states that had not expanded Medicaid (clean non-treated controls).
        donor_fips=["48", "28", "01", "45", "47"],  # TX MS AL SC TN
        source_url="https://gov.louisiana.gov/assets/ExecutiveOrders/JBE-16-01.pdf",
        objective_text=(
            "Expand Medicaid eligibility under the Affordable Care Act to reduce the number of "
            "uninsured working Louisianans (effective July 1, 2016)."
        ),
        enacted_year=2016, enacted_month=7, lag_window_months=36,
        signer_name="John Bel Edwards", signer_party="D",
        metric_kind="uninsured",
    ),
    "MT-2016-MEDICAID": StatePolicySpec(
        key="MT-2016-MEDICAID",
        title="Montana Medicaid expansion (HELP Act, SB 405)",
        state_fips="30",
        state_name="Montana",
        donor_fips=["56", "46", "20", "48", "28"],  # WY SD KS TX MS
        source_url="https://leg.mt.gov/bills/2015/billpdf/SB0405.pdf",
        objective_text=(
            "Expand Medicaid coverage to low-income adults to reduce the number of uninsured "
            "Montanans (Montana HELP Act; coverage effective Jan 1, 2016)."
        ),
        enacted_year=2016, enacted_month=1, lag_window_months=36,
        signer_name="Steve Bullock", signer_party="D",
        metric_kind="uninsured",
    ),
    "VA-2019-MEDICAID": StatePolicySpec(
        key="VA-2019-MEDICAID",
        title="Virginia Medicaid expansion (2018 budget, HB 5002)",
        state_fips="51",
        state_name="Virginia",
        donor_fips=["37", "47", "13", "45", "48"],  # NC TN GA SC TX
        source_url="https://lis.virginia.gov/cgi-bin/legp604.exe?181+sum+HB5002",
        objective_text=(
            "Expand Medicaid eligibility to low-income adults to reduce the number of uninsured "
            "Virginians (coverage effective Jan 1, 2019)."
        ),
        enacted_year=2019, enacted_month=1, lag_window_months=24,
        signer_name="Ralph Northam", signer_party="D",
        metric_kind="uninsured",
    ),
    "ID-2020-MEDICAID": StatePolicySpec(
        key="ID-2020-MEDICAID",
        title="Idaho Medicaid expansion (Proposition 2)",
        state_fips="16",
        state_name="Idaho",
        donor_fips=["56", "46", "20", "48", "47"],  # WY SD KS TX TN
        source_url="https://sos.idaho.gov/elect/inits/2018/prop2.pdf",
        objective_text=(
            "Expand Medicaid eligibility under the Affordable Care Act (voter Proposition 2) to "
            "reduce the number of uninsured Idahoans (coverage effective Jan 1, 2020)."
        ),
        enacted_year=2020, enacted_month=1, lag_window_months=24,
        signer_name="Brad Little", signer_party="R",
        metric_kind="uninsured",
    ),
    # --- Additional minimum-wage laws (metric_kind="wage"): scored on the BLS state average
    # hourly earnings series, where a rise is toward the law's stated wage-raising goal. ---
    "CT-2019-HB5004": StatePolicySpec(
        key="CT-2019-HB5004",
        title="Connecticut minimum wage increase to $15 (Public Act 19-4)",
        state_fips="09",
        state_name="Connecticut",
        # Peer states that kept the federal minimum in the window.
        donor_fips=["42", "47", "13", "45", "48"],  # PA TN GA SC TX
        source_url="https://www.cga.ct.gov/2019/act/pa/pdf/2019PA-00004-R00HB-05004-PA.pdf",
        objective_text=(
            "Raise Connecticut's minimum wage in steps to $15 by 2023 to increase pay for the "
            "state's lowest-wage workers (Public Act 19-4; first increase Oct 1, 2019)."
        ),
        enacted_year=2019, enacted_month=10, lag_window_months=36,
        signer_name="Ned Lamont", signer_party="D",
        metric_kind="wage",
    ),
    "VA-2020-HB395": StatePolicySpec(
        key="VA-2020-HB395",
        title="Virginia minimum wage increase (HB 395)",
        state_fips="51",
        state_name="Virginia",
        donor_fips=["37", "47", "13", "45", "48"],  # NC TN GA SC TX
        source_url="https://lis.virginia.gov/cgi-bin/legp604.exe?201+sum+HB395",
        objective_text=(
            "Raise Virginia's minimum wage in steps toward $15 to increase pay for the state's "
            "lowest-wage workers (HB 395; first increase May 1, 2021)."
        ),
        enacted_year=2021, enacted_month=5, lag_window_months=24,
        signer_name="Ralph Northam", signer_party="D",
        metric_kind="wage",
    ),
    "DE-2021-SB15": StatePolicySpec(
        key="DE-2021-SB15",
        title="Delaware minimum wage increase to $15 (SB 15)",
        state_fips="10",
        state_name="Delaware",
        donor_fips=["42", "47", "13", "45", "37"],  # PA TN GA SC NC
        source_url="https://legis.delaware.gov/BillDetail?legislationId=68328",
        objective_text=(
            "Raise Delaware's minimum wage in steps to $15 by 2025 to increase pay for the state's "
            "lowest-wage workers (SB 15; first increase Jan 1, 2022)."
        ),
        enacted_year=2022, enacted_month=1, lag_window_months=24,
        signer_name="John Carney", signer_party="D",
        metric_kind="wage",
    ),
    # --- Child poverty (metric_kind="child_poverty"): an anti-poverty credit measured against
    # the Census SAIPE under-18 poverty rate, where a fall is toward its stated goal. ---
    "CA-2015-SB80-CHILD": StatePolicySpec(
        key="CA-2015-SB80-CHILD",
        title="California EITC (CalEITC) \u2014 child-poverty outcome (SB 80)",
        state_fips="06",
        state_name="California",
        donor_fips=["48", "12", "13", "37", "42"],  # TX FL GA NC PA
        source_url="https://leginfo.legislature.ca.gov/faces/billNavClient.xhtml?bill_id=201520160SB80",
        objective_text=(
            "Create a refundable California Earned Income Tax Credit whose stated purpose is to "
            "reduce poverty among California's poorest working families, including children."
        ),
        enacted_year=2015, enacted_month=6, lag_window_months=48,
        signer_name="Edmund G. Brown Jr.", signer_party="D",
        metric_kind="child_poverty",
    ),
}


@dataclass
class TargetSpec:
    """A curated, source-linked, pre-registered numeric target for target-relative
    ('promise-keeping') scoring. The realized series is a law's own DEFC-tagged
    USAspending total (directly attributable)."""

    key: str
    congress: int
    law_number: int
    law_type: str
    objective_text: str
    defc: str  # USAspending Disaster Emergency Fund Code for this law
    realized_kind: str  # 'outlay' | 'obligation'
    target_source_url: str
    # 'disaster' = COVID-relief DEFCs (obligation+outlay via disaster endpoint);
    # 'general'  = any DEFC (obligations via spending_by_geography; outlays unavailable).
    realized_source: str = "disaster"
    # Committed/promised amount. None => use the committed OBLIGATION from USAspending as
    # the target (delivery/execution: "did the law outlay the funds it committed?"). A
    # number => a curated, source-linked target (e.g. a CBO/statutory figure).
    target_value: float | None = None
    sign_goal: int = 1  # +1 = "deliver at least the committed amount"
    directly_attributable: bool = True


# Demo: of the emergency-relief award funding a law COMMITTED, how much has actually
# been delivered (outlayed)? Directly attributable (the law's own DEFC-tagged money).
TARGET_SPECS: dict[str, TargetSpec] = {
    "CARES-DELIVERY": TargetSpec(
        key="CARES-DELIVERY",
        congress=116, law_number=136, law_type="pub",
        objective_text=(
            "Disburse the committed CARES Act emergency-relief award funding to provide "
            "rapid economic relief (delivery of obligated funds)."
        ),
        defc="N",
        realized_kind="outlay",
        # Committed CARES award funding (USAspending DEFC 'N' obligations snapshot).
        target_value=285_400_000_000.0,
        target_source_url="https://api.usaspending.gov/api/v2/disaster/award/amount/?def_codes=N",
    ),
    # Non-COVID delivery (obligations vs the law's headline appropriation). Only laws whose
    # DEFC obligations are cleanly commensurable with a confident appropriation figure are
    # included — DEFCs whose totals don't map to a single appropriation are deliberately
    # omitted to avoid misrepresentation (integrity guardrail).
    "UKRAINE-2022-DELIVERY": TargetSpec(
        key="UKRAINE-2022-DELIVERY",
        congress=117, law_number=128, law_type="pub",
        objective_text=(
            "Obligate the ~$40.1B appropriated by the Additional Ukraine Supplemental "
            "Appropriations Act, 2022 (share of the law's appropriation obligated to date)."
        ),
        defc="6", realized_kind="obligation", realized_source="general",
        target_value=40_100_000_000.0,
        target_source_url="https://www.congress.gov/bill/117th-congress/house-bill/7691",
    ),
    "IIJA-DELIVERY": TargetSpec(
        key="IIJA-DELIVERY",
        congress=117, law_number=58, law_type="pub",
        objective_text=(
            "Obligate the ~$550B in new investment authorized by the Infrastructure Investment "
            "and Jobs Act (share of the headline new-investment commitment obligated to date)."
        ),
        defc="Z", realized_kind="obligation", realized_source="general",
        target_value=550_000_000_000.0,
        target_source_url="https://www.congress.gov/bill/117th-congress/house-bill/3684",
    ),
}


@dataclass
class ScoreOutcome:
    action_id: int
    eu_id: int
    status: str
    score_run_id: int | None
    reproducible_hash: str | None


def _obs_window(enacted: date, lag_months: int, pre_years: int = 3) -> tuple[str, str]:
    """Inclusive ISO bounds for an EU's outcome series, so EUs that share a metric
    (e.g. two laws both scored on nonfarm employment) never pollute each other's
    observation set — which keeps every score run deterministic and reproducible.
    ``pre_years`` widens the pre-period (annual official series need more years)."""
    start = f"{enacted.year - pre_years}-01-01"
    end = f"{enacted.year + (lag_months // 12) + 2}-12-31"
    return start, end


def _windowed_observations(session: Session, metric_id: int, enacted: date, lag_months: int):
    # Derive the pre-window from the metric's series kind so the original score and any
    # reproduced re-run pull the exact same observation set (annual -> longer pre-window).
    nsid = session.execute(
        select(Metric.native_series_id).where(Metric.id == metric_id)
    ).scalar_one_or_none()
    start, end = _obs_window(enacted, lag_months, _pre_years_for(nsid))
    rows = session.execute(
        select(Observation.period, Observation.value).where(
            Observation.metric_id == metric_id,
            Observation.period >= start,
            Observation.period <= end,
        ).order_by(Observation.period)
    ).all()
    return [(p, v) for p, v in rows]


def _ensure_methodology(session: Session) -> MethodologyVersion:
    mv = session.execute(
        select(MethodologyVersion).where(MethodologyVersion.semver == settings.methodology_version)
    ).scalar_one_or_none()
    if mv is None:
        mv = MethodologyVersion(
            semver=settings.methodology_version,
            git_sha=current_git_sha(),
            description="MVP slice: pretrend/flat baseline ensemble; sponsor+signer attribution.",
        )
        session.add(mv)
        session.flush()
    return mv


def _objective_for_matching(session: Session, action_id: int) -> Objective | None:
    objs = session.execute(
        select(Objective).where(Objective.action_id == action_id)
    ).scalars().all()
    # Prefer the richer agency/CRS summary; fall back to statutory short title.
    by_level = {o.objective_level: o for o in objs}
    return by_level.get("agency") or by_level.get("statutory") or (objs[0] if objs else None)


def _durability(observations: list[tuple[str, object]], eval_period: str, baseline_pooled: D, sign_goal: int, delta_toward_goal: D) -> object | None:
    """Sustained achievement of the stated goal: fraction of post-evaluation periods
    on the GOAL-WARD side of the baseline. Goal-directional, so a persistent move
    *away* from the objective correctly scores LOW durability (not high)."""
    later = [(p, D(v)) for p, v in observations if p > eval_period]
    if not later:
        return None
    toward = sum(1 for _p, v in later if D(sign_goal) * (v - baseline_pooled) > 0)
    frac = D(toward) / D(len(later))
    return q_score(frac * D(100))


def _finalize(
    session: Session,
    eu: EvaluationUnit,
    action: Action,
    comp,
    attributions,
    *,
    alignment: object,
    observations: list[tuple[str, object]],
    metric: Metric,
    sign_goal: int,
    event_period: str,
    donor_observations: dict[str, list[tuple[str, object]]] | None = None,
    extra_source_urls: list[str] | None = None,
    s_outcome_override: object | None = None,
    definitive: bool = False,
) -> ScoreOutcome:
    """Shared scoring tail: confidence → components → assemble → pinned reproducible run.

    Used by every scoring pipeline (federal laws, state policies, …) so the formula,
    gate, persistence, and reproducibility hash are identical across action types.
    """
    eu.alignment = q_score(D(alignment))  # persist for faithful re-runs (disputes)

    # OutcomeResult / Baseline / AttributionWeight describe the EU's CURRENT result and
    # are keyed by eu_id; a re-run (e.g. dispute resolution) replaces them. Run-level
    # history is preserved via versioned ScoreRun + components + reproducible_hash.
    for model in (OutcomeResult, Baseline, AttributionWeight):
        session.execute(sa_delete(model).where(model.eu_id == eu.id))
    session.flush()

    residual = next((a.attribution for a in attributions if a.is_residual), D(0))
    # (attribution_share, ci_width) per human contributor — weighted in c_attrib so a
    # large roll-call of negligible-share voters can't spuriously inflate confidence.
    human_widths = [
        (D(a.attribution), D(a.attr_ci_high) - D(a.attr_ci_low))
        for a in attributions if not a.is_residual
    ]

    # Sensitivity of the result to the evaluation-horizon choice feeds confidence (§9.10):
    # a direction that flips across defensible lags is fragile.
    sens = analyze_lag_sensitivity(
        observations, event_period=event_period, registered_lag=eu.lag_window_months,
        sign_goal=sign_goal, seed=settings.deterministic_seed,
        donor_observations=donor_observations or None,
    )

    best_method = best_design([e.method for e in comp.per_method])
    conf = compute_confidence(
        best_method=best_method,
        ci_low=comp.ci_low, ci_high=comp.ci_high,
        model_dependence=comp.model_dependence,
        data_tier=1, data_completeness=D("1.0"),
        attribution_widths=human_widths,
        sensitivity_sign_stable=sens.sign_stable,
        definitive=definitive,
    )

    delta_toward_goal = D(sign_goal) * D(comp.delta)
    durability = _durability(observations, comp.eval_period, D(comp.baseline_pooled), sign_goal, delta_toward_goal)
    # Target-relative scoring supplies its own achievement-based S_outcome (delivery of
    # the promised number); baseline-relative maps the standardized effect through the CDF.
    s_outcome = s_outcome_override if s_outcome_override is not None else s_outcome_from_z(comp.z)
    s_evidence = q_score(D(conf.c_design) * D(100))
    s_attribution = q_score((D(1) - D(residual)) * D(100))
    s_alignment = q_score(D(alignment) * D(100))
    s_dataquality = q_score(D(conf.c_data) * D(100))

    assembled = assemble_score(
        s_outcome=s_outcome, s_evidence=s_evidence, s_attribution=s_attribution,
        s_alignment=s_alignment, s_dataquality=s_dataquality, s_durability=durability,
        confidence=conf.confidence,
    )

    session.add(OutcomeResult(
        eu_id=eu.id, observed=q_score(comp.observed), baseline_pooled=q_score(comp.baseline_pooled),
        delta=q_score(comp.delta), z=q_score(comp.z), model_dependence=q_score(comp.model_dependence),
        ci_low=q_score(comp.ci_low), ci_high=q_score(comp.ci_high),
    ))
    for e in comp.per_method:
        session.add(Baseline(
            eu_id=eu.id, method=e.method, spec_json=canonical_json(e.spec),
            baseline_value=q_score(e.baseline_value),
            ci_low=q_score(e.ci_low) if e.ci_low is not None else None,
            ci_high=q_score(e.ci_high) if e.ci_high is not None else None,
        ))
    for a in attributions:
        session.add(AttributionWeight(
            eu_id=eu.id, official_id=a.official_id, role=a.role,
            authority=q_score(a.authority), pivotality=q_score(a.pivotality),
            attribution=q_score(a.attribution), attr_ci_low=q_score(a.attr_ci_low),
            attr_ci_high=q_score(a.attr_ci_high), is_residual=a.is_residual,
        ))

    mv = _ensure_methodology(session)
    # Snapshot identity = the exact numeric inputs that determine the score (treated +
    # donor series, fingerprinted by compute_outcome) plus the metric spec. Independent
    # of volatile provenance bytes (e.g. dynamic HTML), so re-runs are bit-reproducible.
    snapshot = data_snapshot_id(
        [comp.input_hash, metric.native_series_id, str(sign_goal), str(eu.lag_window_months)]
    )

    input_urls = sorted({action.source_url, *(extra_source_urls or [])})
    run = ScoreRun(
        eu_id=eu.id, methodology_version_id=mv.id, data_snapshot_id=snapshot,
        code_git_sha=current_git_sha(), seed=settings.deterministic_seed,
        input_source_urls=canonical_json(input_urls),
    )
    session.add(run)
    session.flush()

    for c in assembled.components:
        session.add(ScoreComponent(
            score_run_id=run.id, component=c.name, value=q_score(c.value),
            ci_low=q_score(c.ci_low) if c.ci_low is not None else None,
            ci_high=q_score(c.ci_high) if c.ci_high is not None else None,
            is_value_laden=c.is_value_laden,
        ))
    session.add(ConfidenceInterval(
        score_run_id=run.id, quantity="outcome_delta",
        ci_low=q_score(comp.ci_low), ci_high=q_score(comp.ci_high), method="bootstrap_2000",
    ))
    session.add(EUScore(
        score_run_id=run.id, confidence=q_score(assembled.confidence),
        composite=q_score(assembled.composite) if assembled.composite is not None else None,
        gated=assembled.gated, coverage=D("1.0"),
    ))

    repro_payload = {
        "data_snapshot_id": snapshot,
        "methodology_version": settings.methodology_version,
        "seed": settings.deterministic_seed,
        "confidence": str(q_score(assembled.confidence)),
        "composite": str(q_score(assembled.composite)) if assembled.composite is not None else None,
        "gated": assembled.gated,
        "components": {c.name: str(q_score(c.value)) for c in assembled.components},
        "outcome": {
            "observed": str(q_score(comp.observed)),
            "baseline_pooled": str(q_score(comp.baseline_pooled)),
            "delta": str(q_score(comp.delta)),
            "z": str(q_score(comp.z)),
        },
        "attribution": [
            {"official_id": a.official_id, "role": a.role, "attribution": str(q_score(a.attribution))}
            for a in attributions
        ],
    }
    run.reproducible_hash = hash_payload(repro_payload)

    eu.status = "insufficient_evidence" if assembled.gated else "scored"
    if assembled.gated:
        eu.non_scoreable_reason = "Confidence below publish threshold; outcome not distinguishable enough."
    session.flush()

    audit.append(session, event_type="SCORE", payload={
        "eu_id": eu.id, "score_run_id": run.id, "reproducible_hash": run.reproducible_hash,
        "gated": assembled.gated,
    })
    return ScoreOutcome(action.id, eu.id, eu.status, run.id, run.reproducible_hash)


def _ingest_passage_votes(session: Session, action: Action, bill: Bill | None):
    """Ingest the final-passage House + Senate roll-call votes for a law so the members
    who passed it receive pivotality-weighted decisive-vote attribution (full member
    records stored for transparency). Best-effort — scoring proceeds even if a chamber's
    vote is unavailable. Returns (house_margin, house_ids, senate_margin, senate_ids)."""
    house_margin: int | None = None
    house_ids: list[int] = []
    senate_margin: int | None = None
    senate_ids: list[int] = []
    if not (bill and bill.congress and bill.bill_number):
        return house_margin, house_ids, senate_margin, senate_ids
    m = re.match(r"([a-z]+)(\d+)", bill.bill_number)
    if not m:
        return house_margin, house_ids, senate_margin, senate_ids
    btype, bnum = m.group(1), int(m.group(2))
    try:
        result = load_house_final_passage_vote(session, action, bill.congress, btype, bnum)
        if result is not None:
            hv, house_ids = result
            house_margin = hv.margin
    except Exception as exc:  # noqa: BLE001 - vote data is optional, never block scoring
        log.warning("house vote ingestion failed for %s: %s", action.native_identifier, exc)
    try:
        sresult = load_senate_final_passage_vote(session, action, bill.congress, btype, bnum)
        if sresult is not None:
            sv, senate_ids = sresult
            senate_margin = sv.margin
    except Exception as exc:  # noqa: BLE001 - vote data is optional, never block scoring
        log.warning("senate vote ingestion failed for %s: %s", action.native_identifier, exc)
    return house_margin, house_ids, senate_margin, senate_ids


def _reconstruct_passage_votes(session: Session, action: Action):
    """Rebuild decisive-vote attribution inputs from STORED roll-call rows (no re-fetch),
    for BOTH chambers — so a re-run reproduces the original attribution exactly. Returns
    (house_margin, house_ids, senate_margin, senate_ids)."""
    def _stored(chamber: str) -> tuple[int | None, list[int]]:
        v = session.execute(
            select(Vote).where(Vote.action_id == action.id, Vote.chamber == chamber)
            .order_by(Vote.id.desc()).limit(1)
        ).scalar_one_or_none()
        if v is None:
            return None, []
        winning = "yea" if v.yea >= v.nay else "nay"
        ids = list(session.execute(
            select(VotePosition.official_id).where(
                VotePosition.vote_id == v.id, VotePosition.position == winning
            )
        ).scalars())
        return abs(v.yea - v.nay), ids

    hm, hids = _stored("house")
    sm, sids = _stored("senate")
    return hm, hids, sm, sids


def score_law(session: Session, congress: int, law_number: int, law_type: str = "pub") -> ScoreOutcome:
    ensure_bls_source(session)
    action = load_law(session, congress, law_number, law_type)
    existing = _existing_outcome(session, action.id)
    if existing is not None:  # idempotent: already scored (safe batch re-runs)
        return existing

    obj = _objective_for_matching(session, action.id)
    if obj is None:
        eu = EvaluationUnit(action_id=action.id, status="non_scoreable_no_objective",
                            non_scoreable_reason="No stated objective found.")
        session.add(eu)
        session.flush()
        return ScoreOutcome(action.id, eu.id, eu.status, None, None)

    primary, _side = select_metrics(obj.text, action.domain)
    if primary is None:
        eu = EvaluationUnit(action_id=action.id, objective_id=obj.id, status="non_scoreable_no_metric",
                            non_scoreable_reason="No official metric operationalizes the stated objective.")
        session.add(eu)
        session.flush()
        return ScoreOutcome(action.id, eu.id, eu.status, None, None)

    metric = ensure_metric(session, primary.spec)
    sign_goal = sign_goal_for(primary.spec)
    lag = primary.spec.default_lag_months

    eu = EvaluationUnit(
        action_id=action.id, objective_id=obj.id, metric_id=metric.id,
        lag_window_months=lag, sign_goal=sign_goal, status="pending",
    )
    session.add(eu)
    session.flush()

    # --- PRE-REGISTER before any outcome data is consulted ---
    preregister(
        session, eu,
        action_native_id=action.native_identifier,
        metric_code=primary.spec.code,
        objective_level=obj.objective_level,
        sign_goal=sign_goal,
        lag_window_months=lag,
        masked_objective=mask_party_and_name(obj.text)[:280],
    )

    # --- Now ingest outcome series ---
    enacted = action.action_date
    start_year = enacted.year - 3
    end_year = enacted.year + (lag // 12) + 2
    load_observations(session, metric, start_year, end_year)

    observations = _windowed_observations(session, metric.id, enacted, lag)
    event_period = f"{enacted.year}-{enacted.month:02d}-01"

    comp = compute_outcome(
        observations, event_period=event_period, lag_window_months=lag,
        sign_goal=sign_goal, seed=settings.deterministic_seed,
    )
    if comp is None:
        eu.status = "insufficient_evidence"
        eu.non_scoreable_reason = "Insufficient outcome observations around the evaluation window."
        session.flush()
        return ScoreOutcome(action.id, eu.id, eu.status, None, None)

    # --- Attribution ---
    bill = session.get(Bill, action.id)
    law = session.get(Law, action.id)

    vote_margin, decisive_ids, senate_margin, senate_decisive_ids = _ingest_passage_votes(
        session, action, bill
    )

    actx = AttributionContext(
        eu_id=eu.id,
        action_type=action.type,
        sponsor_official_id=bill.sponsor_official_id if bill else None,
        signer_official_id=law.signed_by_official_id if law else None,
        vote_margin=vote_margin,
        member_on_winning_side=bool(decisive_ids),
        decisive_official_ids=decisive_ids,
        senate_vote_margin=senate_margin,
        senate_decisive_official_ids=senate_decisive_ids,
    )
    attributions = build_attribution(actx)
    return _finalize(
        session, eu, action, comp, attributions,
        alignment=D(primary.alignment), observations=observations, metric=metric, sign_goal=sign_goal,
        event_period=event_period,
    )


def score_executive_order(session: Session, document_number: str) -> ScoreOutcome:
    """Ingest + score one executive order (Federal Register) end-to-end.

    Same neutral machinery as laws; attribution gives the signing president high
    executive authority (EOs are unilateral). Most EOs will be non-scoreable or
    insufficient-evidence (narrow / diffuse objectives) — reported honestly.
    """
    ensure_bls_source(session)
    action = load_executive_order(session, document_number)
    existing = _existing_outcome(session, action.id)
    if existing is not None:
        return existing

    obj = _objective_for_matching(session, action.id)
    if obj is None:
        eu = EvaluationUnit(action_id=action.id, status="non_scoreable_no_objective",
                            non_scoreable_reason="No stated objective found.")
        session.add(eu)
        session.flush()
        return ScoreOutcome(action.id, eu.id, eu.status, None, None)

    primary, _side = select_metrics(obj.text, action.domain)
    if primary is None:
        eu = EvaluationUnit(action_id=action.id, objective_id=obj.id, status="non_scoreable_no_metric",
                            non_scoreable_reason="No official metric operationalizes the stated objective.")
        session.add(eu)
        session.flush()
        return ScoreOutcome(action.id, eu.id, eu.status, None, None)

    metric = ensure_metric(session, primary.spec)
    sign_goal = sign_goal_for(primary.spec)
    lag = primary.spec.default_lag_months
    eu = EvaluationUnit(action_id=action.id, objective_id=obj.id, metric_id=metric.id,
                        lag_window_months=lag, sign_goal=sign_goal, status="pending")
    session.add(eu)
    session.flush()

    preregister(
        session, eu, action_native_id=action.native_identifier, metric_code=primary.spec.code,
        objective_level=obj.objective_level, sign_goal=sign_goal, lag_window_months=lag,
        masked_objective=mask_party_and_name(obj.text)[:280],
    )

    enacted = action.action_date
    load_observations(session, metric, enacted.year - 3, enacted.year + (lag // 12) + 2)
    observations = _windowed_observations(session, metric.id, enacted, lag)
    event_period = f"{enacted.year}-{enacted.month:02d}-01"

    comp = compute_outcome(observations, event_period=event_period, lag_window_months=lag,
                           sign_goal=sign_goal, seed=settings.deterministic_seed)
    if comp is None:
        eu.status = "insufficient_evidence"
        eu.non_scoreable_reason = "Insufficient outcome observations around the evaluation window."
        session.flush()
        return ScoreOutcome(action.id, eu.id, eu.status, None, None)

    eo = session.get(ExecutiveOrder, action.id)
    actx = AttributionContext(
        eu_id=eu.id, action_type=action.type,
        sponsor_official_id=None,
        signer_official_id=eo.signing_official_id if eo else None,
        vote_margin=None, member_on_winning_side=None,
    )
    attributions = build_attribution(actx)
    return _finalize(
        session, eu, action, comp, attributions,
        alignment=D(primary.alignment), observations=observations, metric=metric, sign_goal=sign_goal,
        event_period=event_period,
    )


def score_regulation(session: Session, document_number: str) -> ScoreOutcome:
    """Ingest + score one final agency rule (Federal Register) end-to-end.

    Same neutral machinery as laws/EOs. A regulation is an agency act under delegated
    executive authority, so it is attributed to the administration in office on its
    effective date (high executive authority via the signer channel's 'regulation' case,
    with a large unattributable residual). Most rules will be non-scoreable or
    insufficient-evidence (narrow / diffuse objectives) — reported honestly.
    """
    from degreezeor.core.reference import president_on

    ensure_bls_source(session)
    action = load_regulation(session, document_number)
    existing = _existing_outcome(session, action.id)
    if existing is not None:
        return existing

    obj = _objective_for_matching(session, action.id)
    if obj is None:
        eu = EvaluationUnit(action_id=action.id, status="non_scoreable_no_objective",
                            non_scoreable_reason="No stated objective found.")
        session.add(eu)
        session.flush()
        return ScoreOutcome(action.id, eu.id, eu.status, None, None)

    primary, _side = select_metrics(obj.text, action.domain)
    if primary is None:
        eu = EvaluationUnit(action_id=action.id, objective_id=obj.id, status="non_scoreable_no_metric",
                            non_scoreable_reason="No official metric operationalizes the stated objective.")
        session.add(eu)
        session.flush()
        return ScoreOutcome(action.id, eu.id, eu.status, None, None)

    metric = ensure_metric(session, primary.spec)
    sign_goal = sign_goal_for(primary.spec)
    lag = primary.spec.default_lag_months
    eu = EvaluationUnit(action_id=action.id, objective_id=obj.id, metric_id=metric.id,
                        lag_window_months=lag, sign_goal=sign_goal, status="pending")
    session.add(eu)
    session.flush()

    preregister(
        session, eu, action_native_id=action.native_identifier, metric_code=primary.spec.code,
        objective_level=obj.objective_level, sign_goal=sign_goal, lag_window_months=lag,
        masked_objective=mask_party_and_name(obj.text)[:280],
    )

    enacted = action.action_date
    load_observations(session, metric, enacted.year - 3, enacted.year + (lag // 12) + 2)
    observations = _windowed_observations(session, metric.id, enacted, lag)
    event_period = f"{enacted.year}-{enacted.month:02d}-01"

    comp = compute_outcome(observations, event_period=event_period, lag_window_months=lag,
                           sign_goal=sign_goal, seed=settings.deterministic_seed)
    if comp is None:
        eu.status = "insufficient_evidence"
        eu.non_scoreable_reason = "Insufficient outcome observations around the evaluation window."
        session.flush()
        return ScoreOutcome(action.id, eu.id, eu.status, None, None)

    # Attribute to the administration in office on the rule's effective date (derived from
    # the action date, so it reconstructs identically on re-run — no subtype row needed).
    signer = president_on(session, action.action_date) if action.action_date else None
    actx = AttributionContext(
        eu_id=eu.id, action_type=action.type, sponsor_official_id=None,
        signer_official_id=signer.id if signer else None,
        vote_margin=None, member_on_winning_side=None,
    )
    attributions = build_attribution(actx)
    return _finalize(
        session, eu, action, comp, attributions,
        alignment=D(primary.alignment), observations=observations, metric=metric, sign_goal=sign_goal,
        event_period=event_period,
    )


def _eu_donor_observations(action: Action) -> tuple[dict[str, list[tuple[str, object]]], list[str]]:
    """Reconstruct a state policy's donor series (cache-first, no new network)."""
    donor_observations: dict[str, list[tuple[str, object]]] = {}
    donor_source_urls: list[str] = []
    spec = STATE_POLICIES.get(action.native_identifier or "")
    if spec is None or not spec.donor_fips:
        return donor_observations, donor_source_urls
    prev = os.environ.get("DZ_HTTP_CACHE")
    os.environ["DZ_HTTP_CACHE"] = "1"
    try:
        nsid0 = state_series_id(spec.state_fips, spec.metric_kind)
        sy = spec.enacted_year - _pre_years_for(nsid0)
        ey = spec.enacted_year + (spec.lag_window_months // 12) + 2
        for dfips in spec.donor_fips:
            dfetch, pts = _fetch_state_series_points(state_series_id(dfips, spec.metric_kind), start_year=sy, end_year=ey)
            donor_source_urls.append(dfetch.source_url)
            donor_observations[dfips] = pts
    finally:
        if prev is None:
            os.environ.pop("DZ_HTTP_CACHE", None)
        else:
            os.environ["DZ_HTTP_CACHE"] = prev
    return donor_observations, donor_source_urls


def eu_sensitivity(session: Session, eu_id: int):
    """Lag-window sensitivity analysis for an EU (PLAN.md §9.10), from stored data."""
    from degreezeor.scoring.sensitivity import DEFAULT_LAGS, analyze_lag_sensitivity

    eu = session.get(EvaluationUnit, eu_id)
    if eu is None or eu.metric_id is None or eu.sign_goal is None:
        return None
    action = session.get(Action, eu.action_id)
    enacted = action.action_date
    # Window wide enough to cover the longest probed horizon (uses whatever is stored/cached).
    observations = _windowed_observations(session, eu.metric_id, enacted, max(DEFAULT_LAGS))
    if len(observations) < 7:
        return None
    donors, _ = _eu_donor_observations(action)
    event_period = f"{enacted.year}-{enacted.month:02d}-01"
    return analyze_lag_sensitivity(
        observations, event_period=event_period, registered_lag=eu.lag_window_months,
        sign_goal=eu.sign_goal, seed=settings.deterministic_seed,
        donor_observations=donors or None,
    )


def _rescore_target_eu(session: Session, eu, action, metric) -> ScoreOutcome:
    from degreezeor.ingestion.adapters.usaspending import usaspending_adapter
    from degreezeor.scoring.target_outcome import compute_target_outcome

    # Curated-fact EUs (e.g. court survival) store the realized value directly — re-runs
    # use it as-is (no re-fetch), so they're deterministic by construction.
    if eu.realized_value is not None:
        event_period = f"{action.action_date.year}-{action.action_date.month:02d}-01"
        from degreezeor.scoring.target_outcome import compute_target_outcome
        tc = compute_target_outcome(
            realized=float(eu.realized_value), target=float(eu.target_value), sign_goal=eu.sign_goal,
            directly_attributable=bool(eu.directly_attributable), eval_period=event_period)
        eo = session.get(ExecutiveOrder, action.id)
        law = session.get(Law, action.id)
        bill = session.get(Bill, action.id)
        signer = (law.signed_by_official_id if law else None) or (eo.signing_official_id if eo else None)
        h_margin, h_ids, s_margin, s_ids = _reconstruct_passage_votes(session, action)
        actx = AttributionContext(
            eu_id=eu.id, action_type=action.type,
            sponsor_official_id=bill.sponsor_official_id if bill else None,
            signer_official_id=signer,
            vote_margin=h_margin, member_on_winning_side=bool(h_ids), decisive_official_ids=h_ids,
            senate_vote_margin=s_margin, senate_decisive_official_ids=s_ids)
        attributions = build_attribution(actx)
        return _finalize(session, eu, action, tc.outcome, attributions, alignment=eu.alignment,
                         observations=[], metric=metric, sign_goal=eu.sign_goal,
                         event_period=event_period, s_outcome_override=tc.s_outcome, definitive=True)

    # native_series_id: "DEFC:<code>:<kind>" | "DEFCGEN:<code>:obligation" |
    #                    "AGENCYBUDGET:<toptier>:<fy>:<kind>"
    parts = metric.native_series_id.split(":")
    prefix = parts[0]
    prev = os.environ.get("DZ_HTTP_CACHE")
    os.environ["DZ_HTTP_CACHE"] = "1"
    try:
        if prefix == "AGENCYBUDGET":
            _, toptier, fy, realized_kind = parts
            rfetch = usaspending_adapter.fetch_agency_budget(toptier)
            realized = usaspending_adapter.parse_agency_budget(rfetch.content, int(fy))[realized_kind]
        elif prefix == "DEFCGEN":
            defc = parts[1]
            rfetch = usaspending_adapter.fetch_general_obligations(defc, action.action_date.year - 1, 2025)
            realized = usaspending_adapter.parse_general_obligation(rfetch.content)
        else:
            defc, realized_kind = parts[1], parts[2]
            rfetch = usaspending_adapter.fetch(defc)
            realized = usaspending_adapter.parse_amounts(rfetch.content)[realized_kind]
    finally:
        if prev is None:
            os.environ.pop("DZ_HTTP_CACHE", None)
        else:
            os.environ["DZ_HTTP_CACHE"] = prev
    event_period = f"{action.action_date.year}-{action.action_date.month:02d}-01"
    tc = compute_target_outcome(
        realized=realized, target=eu.target_value, sign_goal=eu.sign_goal,
        directly_attributable=bool(eu.directly_attributable), eval_period=event_period,
    )
    bill = session.get(Bill, action.id)
    law = session.get(Law, action.id)
    eo = session.get(ExecutiveOrder, action.id)
    signer = (law.signed_by_official_id if law else None) or (eo.signing_official_id if eo else None)
    if signer is None and action.type == "budget":
        # Budget actions store no Law/EO row; re-derive the executing president from the
        # action date (same as scoring) so the attribution reproduces exactly.
        from degreezeor.core.reference import president_on
        pres = president_on(session, action.action_date)
        signer = pres.id if pres else None
    # Reconstruct passage-vote attribution from STORED roll-call rows (delivery-scored
    # laws credit the legislators who passed them) so the re-run reproduces exactly.
    h_margin, h_ids, s_margin, s_ids = _reconstruct_passage_votes(session, action)
    actx = AttributionContext(
        eu_id=eu.id, action_type=action.type,
        sponsor_official_id=bill.sponsor_official_id if bill else None,
        signer_official_id=signer,
        vote_margin=h_margin, member_on_winning_side=bool(h_ids), decisive_official_ids=h_ids,
        senate_vote_margin=s_margin, senate_decisive_official_ids=s_ids,
    )
    attributions = build_attribution(actx)
    return _finalize(
        session, eu, action, tc.outcome, attributions,
        alignment=eu.alignment, observations=[], metric=metric, sign_goal=eu.sign_goal,
        event_period=event_period, extra_source_urls=[rfetch.source_url],
        s_outcome_override=tc.s_outcome,
    )


def rescore_eu(session: Session, eu_id: int) -> ScoreOutcome:
    """Deterministically RE-RUN an existing evaluation unit from stored inputs.

    Reads the EU's persisted objective/metric/observations/alignment and rebuilds the
    attribution context from the action — refetching outcome series ONLY from the URL
    replay cache (no new network calls). Donor series for state comparison designs are
    reconstructed (cache-first) from the policy spec. Produces a fresh, pinned ScoreRun;
    a faithful re-run yields the SAME reproducible_hash. This is the engine behind the
    dispute/appeal process: anyone can trigger an independent, reproducible re-run.
    """
    eu = session.get(EvaluationUnit, eu_id)
    if eu is None or eu.metric_id is None or eu.objective_id is None:
        raise ValueError(f"EU {eu_id} is not in a re-scoreable state")
    action = session.get(Action, eu.action_id)
    metric = session.get(Metric, eu.metric_id)

    # Target-relative EUs re-run by re-observing the directly-attributable realized
    # series (cache-first) and recomputing against the STORED target — no counterfactual.
    if eu.evaluation_mode == "target":
        return _rescore_target_eu(session, eu, action, metric)

    enacted = action.action_date
    lag = eu.lag_window_months
    observations = _windowed_observations(session, metric.id, enacted, lag)
    event_period = f"{enacted.year}-{enacted.month:02d}-01"

    # Reconstruct donor series (cache-first) for state comparison-design policies.
    donor_observations, donor_source_urls = _eu_donor_observations(action)

    comp = compute_outcome(
        observations, event_period=event_period, lag_window_months=lag,
        sign_goal=eu.sign_goal, seed=settings.deterministic_seed,
        donor_observations=donor_observations or None,
    )
    if comp is None:
        raise ValueError(f"EU {eu_id} has insufficient stored observations to re-run")

    bill = session.get(Bill, action.id)
    law = session.get(Law, action.id)
    eo = session.get(ExecutiveOrder, action.id)
    signer = (law.signed_by_official_id if law else None) or (eo.signing_official_id if eo else None)
    if signer is None and action.type == "regulation" and action.action_date:
        # Regulations carry no subtype row; re-derive the administration from the action
        # date (same as scoring) so attribution reproduces exactly.
        from degreezeor.core.reference import president_on
        pres = president_on(session, action.action_date)
        signer = pres.id if pres else None

    # Reconstruct decisive-vote attribution from STORED roll-call rows (no re-fetch),
    # so the re-run reproduces the original attribution exactly — for BOTH chambers.
    vote_margin, decisive_ids, senate_margin, senate_decisive_ids = _reconstruct_passage_votes(
        session, action
    )

    actx = AttributionContext(
        eu_id=eu.id, action_type=action.type,
        sponsor_official_id=bill.sponsor_official_id if bill else None,
        signer_official_id=signer, vote_margin=vote_margin,
        member_on_winning_side=bool(decisive_ids), decisive_official_ids=decisive_ids,
        senate_vote_margin=senate_margin, senate_decisive_official_ids=senate_decisive_ids,
    )
    attributions = build_attribution(actx)
    return _finalize(
        session, eu, action, comp, attributions,
        alignment=eu.alignment, observations=observations, metric=metric, sign_goal=eu.sign_goal,
        event_period=event_period, donor_observations=donor_observations,
        extra_source_urls=donor_source_urls,
    )


def score_target(session: Session, spec: TargetSpec) -> ScoreOutcome:
    """Target-relative ('promise-keeping') scoring: did the law DELIVER its committed
    number, measured by its own directly-attributable USAspending DEFC spending?"""
    from degreezeor.ingestion.adapters.usaspending import usaspending_adapter
    from degreezeor.scoring.target_outcome import compute_target_outcome

    action = load_law(session, spec.congress, spec.law_number, spec.law_type)
    usa_src = ensure_source(
        session, name=usaspending_adapter.name, tier=usaspending_adapter.tier,
        base_url=usaspending_adapter.base_url,
    )

    is_general = spec.realized_source == "general"
    metric_code = (f"obligation_delivery_{spec.defc}" if is_general
                   else f"relief_delivery_{spec.defc}")
    native_series = (f"DEFCGEN:{spec.defc}:obligation" if is_general
                     else f"DEFC:{spec.defc}:{spec.realized_kind}")
    metric = session.execute(
        select(Metric).where(Metric.code == metric_code)
    ).scalar_one_or_none()
    if metric is None:
        metric = Metric(
            code=metric_code,
            name=(f"Realized federal {'obligations' if is_general else spec.realized_kind + 's'}, "
                  f"DEFC {spec.defc} (USAspending)"),
            unit="USD", direction_good="up", source_id=usa_src.id,
            native_series_id=native_series, domain="Economics and Public Finance",
        )
        session.add(metric)
        session.flush()

    existing = _existing_outcome_for_metric(session, action.id, metric.id)
    if existing is not None:  # idempotent: already scored (cron-safe)
        return existing
    obj = Objective(action_id=action.id, text=spec.objective_text, source_id=usa_src.id,
                    source_url=spec.target_source_url, objective_level="operational")
    session.add(obj)
    session.flush()
    target_kind = "curated" if spec.target_value is not None else "committed_obligation"
    eu = EvaluationUnit(
        action_id=action.id, objective_id=obj.id, metric_id=metric.id,
        lag_window_months=0, sign_goal=spec.sign_goal, status="pending",
        evaluation_mode="target", target_value=None,
        directly_attributable=spec.directly_attributable,
    )
    session.add(eu)
    session.flush()

    # Pre-register the RULE (metric, mode, target_kind, goal) BEFORE observing spending.
    # For committed_obligation the target NUMBER is the committed amount observed at fetch,
    # but the evaluation rule is fixed in advance (analogous to a pre-registered baseline).
    preregister(
        session, eu, action_native_id=action.native_identifier, metric_code=metric.code,
        objective_level="operational", sign_goal=spec.sign_goal, lag_window_months=0,
        masked_objective=(f"target_mode target_kind={target_kind} directly_attributable="
                          f"{spec.directly_attributable} :: {spec.objective_text}")[:280],
    )

    # Observe realized (directly-attributable) spending.
    if is_general:
        # Start at the fiscal year containing enactment (FY begins Oct 1 of year-1).
        rfetch = usaspending_adapter.fetch_general_obligations(
            spec.defc, action.action_date.year - 1, 2025)
        land(session, rfetch)
        realized = usaspending_adapter.parse_general_obligation(rfetch.content)
        target_amount = spec.target_value  # curated appropriation (required for 'general')

        # INTEGRITY GUARDS for non-COVID obligation totals (no stable outlay series):
        # (1) window-stability — the total must not change materially with the query window;
        # (2) commensurability — obligations must not exceed the appropriation (else the DEFC
        # total isn't a clean delivery measure). Fragile cases are rejected, not published.
        wide = usaspending_adapter.fetch_general_obligations(spec.defc, action.action_date.year - 5, 2025)
        land(session, wide)
        realized_wide = usaspending_adapter.parse_general_obligation(wide.content)
        denom = max(realized, realized_wide, 1.0)
        if abs(realized - realized_wide) / denom > 0.05:
            eu.status = "non_scoreable_no_metric"
            eu.non_scoreable_reason = (
                "Obligation total is not stable across query windows, so a reliable "
                "delivery share can't be computed (integrity guard)."
            )
            session.flush()
            return ScoreOutcome(action.id, eu.id, eu.status, None, None)
        if target_amount and realized > target_amount * 1.1:
            eu.status = "non_scoreable_no_metric"
            eu.non_scoreable_reason = (
                "DEFC obligations exceed the law's appropriation, so the total isn't "
                "commensurable with a clean delivery measure (integrity guard)."
            )
            session.flush()
            return ScoreOutcome(action.id, eu.id, eu.status, None, None)
    else:
        rfetch = usaspending_adapter.fetch(spec.defc)
        land(session, rfetch)
        amounts = usaspending_adapter.parse_amounts(rfetch.content)
        realized = amounts[spec.realized_kind]
        target_amount = spec.target_value if spec.target_value is not None else amounts["obligation"]
    if not target_amount:
        eu.status = "non_scoreable_no_metric"
        eu.non_scoreable_reason = (
            f"No award-level spending is tracked for DEFC {spec.defc} via the USAspending "
            "disaster endpoint (e.g. non-COVID supplementals), so delivery isn't measurable here."
        )
        session.flush()
        return ScoreOutcome(action.id, eu.id, eu.status, None, None)
    eu.target_value = D(str(target_amount))

    event_period = f"{action.action_date.year}-{action.action_date.month:02d}-01"
    tc = compute_target_outcome(
        realized=realized, target=target_amount, sign_goal=spec.sign_goal,
        directly_attributable=spec.directly_attributable, eval_period=event_period,
    )

    bill = session.get(Bill, action.id)
    law = session.get(Law, action.id)
    # Credit the legislators who PASSED the law (final-passage roll-calls), not just the
    # sponsor/signer — so a delivery-scored law connects to the members who enacted it.
    h_margin, h_ids, s_margin, s_ids = _ingest_passage_votes(session, action, bill)
    actx = AttributionContext(
        eu_id=eu.id, action_type=action.type,
        sponsor_official_id=bill.sponsor_official_id if bill else None,
        signer_official_id=law.signed_by_official_id if law else None,
        vote_margin=h_margin, member_on_winning_side=bool(h_ids), decisive_official_ids=h_ids,
        senate_vote_margin=s_margin, senate_decisive_official_ids=s_ids,
    )
    attributions = build_attribution(actx)
    return _finalize(
        session, eu, action, tc.outcome, attributions,
        alignment=D("0.95"), observations=[], metric=metric, sign_goal=spec.sign_goal,
        event_period=event_period, extra_source_urls=[rfetch.source_url],
        s_outcome_override=tc.s_outcome,
    )


def _isolated(session: Session, fn, *, label: str):
    """Run a single scorer inside a SAVEPOINT so one item's failure rolls back ONLY that
    item — keeping the session usable and prior work intact — instead of poisoning the
    whole refresh pass (a duplicate-key / network error on one unit must not abort the rest)."""
    sp = session.begin_nested()
    try:
        result = fn()
        sp.commit()  # release the savepoint; the work stays in the outer transaction
        return result
    except Exception as exc:  # noqa: BLE001 - isolate per-item failures
        if sp.is_active:
            sp.rollback()
        log.warning("%s failed: %s", label, exc)
        return None


def ingest_defc_delivery(session: Session, limit: int | None = None) -> list[ScoreOutcome]:
    """#1 — batch verifiable 'delivery' scores: for every law with DEFC-tagged spending,
    score realized USAspending outlays vs the funds it committed (directly attributable)."""
    from degreezeor.ingestion.adapters.usaspending import usaspending_adapter

    results: list[ScoreOutcome] = []
    for entry in usaspending_adapter.def_codes():
        if limit is not None and len(results) >= limit:
            break
        spec = TargetSpec(
            key=f"DEFC-{entry['code']}",
            congress=entry["congress"], law_number=entry["law_number"], law_type="pub",
            objective_text=(
                f"Disburse the funds committed under {entry['title']} "
                f"(DEFC {entry['code']}) — delivery of obligated emergency/supplemental funding."
            ),
            defc=entry["code"], realized_kind="outlay", target_value=None,
            target_source_url=(
                f"https://api.usaspending.gov/api/v2/disaster/award/amount/?def_codes={entry['code']}"
            ),
        )
        r = _isolated(session, lambda spec=spec: score_target(session, spec),
                      label=f"DEFC {entry['code']} delivery")
        if r is not None:
            results.append(r)
    return results


def _existing_outcome_for_metric(session: Session, action_id: int, metric_id: int) -> ScoreOutcome | None:
    """Idempotency for target/court/state scorers: if this (action, metric) is already
    scored, return its outcome so a nightly cron can re-run safely without duplicates."""
    eu = session.execute(
        select(EvaluationUnit).where(
            EvaluationUnit.action_id == action_id, EvaluationUnit.metric_id == metric_id
        ).order_by(EvaluationUnit.id.desc()).limit(1)
    ).scalar_one_or_none()
    if eu is None:
        return None
    run = session.execute(
        select(ScoreRun).where(ScoreRun.eu_id == eu.id).order_by(ScoreRun.id.desc()).limit(1)
    ).scalar_one_or_none()
    return ScoreOutcome(action_id, eu.id, eu.status,
                        run.id if run else None, run.reproducible_hash if run else None)


def _existing_outcome(session: Session, action_id: int) -> ScoreOutcome | None:
    """If this action already has an evaluation unit, return its outcome (idempotency)."""
    eu = session.execute(
        select(EvaluationUnit).where(EvaluationUnit.action_id == action_id)
        .order_by(EvaluationUnit.id.desc()).limit(1)
    ).scalar_one_or_none()
    if eu is None:
        return None
    run = session.execute(
        select(ScoreRun).where(ScoreRun.eu_id == eu.id).order_by(ScoreRun.id.desc()).limit(1)
    ).scalar_one_or_none()
    return ScoreOutcome(action_id, eu.id, eu.status,
                        run.id if run else None, run.reproducible_hash if run else None)


def batch_score_laws(session: Session, congress: int, limit: int = 25) -> list[ScoreOutcome]:
    """#2 — breadth: ingest + score enacted laws for a congress (bounded by ``limit``).
    Most land as insufficient-evidence / non-scoreable — the honest denominator that makes
    the scored subset interpretable. Idempotent (skips laws already scored)."""
    import json as _json

    from degreezeor.ingestion.adapters.congress import congress_adapter

    results: list[ScoreOutcome] = []
    offset = 0
    while len(results) < limit:
        page = _json.loads(congress_adapter.fetch_law_list(congress, 250, offset).content)
        bills = page.get("bills", [])
        if not bills:
            break
        for b in bills:
            if len(results) >= limit:
                break
            laws = b.get("laws") or []
            if not laws:
                continue
            m = re.match(r"\d+-(\d+)", laws[0].get("number", ""))
            if not m:
                continue
            law_number = int(m.group(1))
            law_type = "pub" if "Public" in (laws[0].get("type") or "") else "priv"
            r = _isolated(session, lambda c=congress, n=law_number, t=law_type: score_law(session, c, n, t),
                          label=f"batch law {congress}-{law_number}")
            if r is not None:
                results.append(r)
        offset += 250
    return results


def batch_score_executive_orders(session: Session, limit: int = 25) -> list[ScoreOutcome]:
    """#2 — breadth: ingest + score recent executive orders (Federal Register, keyless)."""
    import json as _json

    from degreezeor.ingestion.adapters.federalregister import federal_register_adapter
    from degreezeor.ingestion.http import client as _client

    url = f"{federal_register_adapter.base_url}/documents.json"
    params = {
        "conditions[type][]": "PRESDOCU",
        "conditions[presidential_document_type][]": "executive_order",
        "order": "newest", "per_page": str(min(limit, 100)),
    }
    content = _client.get_bytes(url, params=params)
    docs = _json.loads(content).get("results", [])
    results: list[ScoreOutcome] = []
    for d in docs[:limit]:
        doc_number = d.get("document_number")
        if not doc_number:
            continue
        r = _isolated(session, lambda dn=doc_number: score_executive_order(session, dn),
                      label=f"batch EO {doc_number}")
        if r is not None:
            results.append(r)
    return results


def ingest_executive_actions(
    session: Session, *, new_limit: int = 400, max_pages: int = 40, per_page: int = 100,
) -> int:
    """Activity/record layer for the executive: ingest recent executive orders (Federal
    Register, keyless) as Action + ExecutiveOrder rows attributed to the signing president,
    WITHOUT scoring. This makes a president's full term visible (what they acted on) — the
    analogue of sponsored bills for members. Recency-first, idempotent, capped per run."""
    import json as _json

    from degreezeor.ingestion.adapters.federalregister import federal_register_adapter
    from degreezeor.ingestion.http import client as _client

    url = f"{federal_register_adapter.base_url}/documents.json"
    inserted = 0
    for page in range(1, max_pages + 1):
        if inserted >= new_limit:
            break
        params = {
            "conditions[type][]": "PRESDOCU",
            "conditions[presidential_document_type][]": "executive_order",
            "order": "newest", "per_page": str(per_page), "page": str(page),
            "fields[]": ["document_number", "executive_order_number"],
        }
        try:
            content = _client.get_bytes(url, params=params)
        except Exception:  # noqa: BLE001 - never fatal
            break
        results = _json.loads(content).get("results", [])
        if not results:
            break
        for r in results:
            if inserted >= new_limit:
                break
            doc_num = r.get("document_number")
            eo_num = r.get("executive_order_number")
            if not doc_num:
                continue
            native = f"EO{eo_num}" if eo_num else f"FR{doc_num}"
            if session.execute(
                select(Action.id).where(Action.native_identifier == native, Action.type == "eo")
            ).scalar_one_or_none():
                continue  # already stored (idempotent; cheap — no per-doc fetch)
            try:
                load_executive_order(session, doc_num)
                inserted += 1
            except Exception:  # noqa: BLE001 - one bad doc must not abort the batch
                continue
        session.flush()
    return inserted


def purge_empty_officials(session: Session) -> int:
    """Remove official records that carry NO record anywhere — no scored attribution, no
    sponsored/cosponsored bills, no recorded votes, no signed laws/EOs. These are data
    artifacts (e.g. vote-derived stubs that never linked). Officials who genuinely held office
    are kept by design: their record stands regardless of whether they're still in office."""
    from degreezeor.core.models import (
        BillCosponsor,
        ExecutiveOrder,
        Law,
        OfficeTerm,
        VotePosition,
    )

    referenced: set[int] = set()
    referenced |= {x for (x,) in session.execute(
        select(AttributionWeight.official_id).distinct()).all() if x is not None}
    referenced |= {x for (x,) in session.execute(
        select(Bill.sponsor_official_id).where(Bill.sponsor_official_id.is_not(None)).distinct()).all()}
    referenced |= {x for (x,) in session.execute(
        select(BillCosponsor.official_id).distinct()).all() if x is not None}
    referenced |= {x for (x,) in session.execute(
        select(VotePosition.official_id).distinct()).all() if x is not None}
    referenced |= {x for (x,) in session.execute(
        select(ExecutiveOrder.signing_official_id).where(
            ExecutiveOrder.signing_official_id.is_not(None)).distinct()).all()}
    referenced |= {x for (x,) in session.execute(
        select(Law.signed_by_official_id).where(Law.signed_by_official_id.is_not(None)).distinct()).all()}

    all_ids = {x for (x,) in session.execute(select(Official.id)).all()}
    orphans = all_ids - referenced
    if not orphans:
        return 0
    session.execute(sa_delete(OfficeTerm).where(OfficeTerm.official_id.in_(orphans)))
    session.execute(sa_delete(Official).where(Official.id.in_(orphans)))
    session.flush()
    return len(orphans)


def backfill_eo_source_urls(session: Session, *, limit: int = 8000) -> int:
    """Repair executive-order links that point at the bare /documents/<doc> form (which can
    404) by switching to the Federal Register's guaranteed /d/<doc> permalink."""
    rows = session.execute(
        select(Action.id, ExecutiveOrder.fr_doc_number)
        .join(ExecutiveOrder, ExecutiveOrder.action_id == Action.id)
        .where(Action.type == "eo", ExecutiveOrder.fr_doc_number.is_not(None),
               Action.source_url.notlike("%federalregister.gov/d/%"),
               Action.source_url.notlike("%/documents/2%/%/%"))  # leave canonical date-path URLs
        .limit(limit)
    ).all()
    fixed = 0
    for aid, doc in rows:
        session.execute(sa_update(Action).where(Action.id == aid).values(
            source_url=f"https://www.federalregister.gov/d/{doc}"))
        fixed += 1
    session.flush()
    return fixed


def backfill_eo_categories(session: Session, *, limit: int = 6000) -> int:
    """Re-classify executive orders that still carry the legacy uniform domain (or none) so
    the executive record groups by topic. Deterministic; uses the EO's title + objective text."""
    from degreezeor.categories import classify_executive_domain

    rows = session.execute(
        select(Action.id, Action.title, Action.domain, Objective.text)
        .join(ExecutiveOrder, ExecutiveOrder.action_id == Action.id)
        .join(Objective, Objective.action_id == Action.id, isouter=True)
        .where(Action.type == "eo",
               Action.domain.in_(["Economics and Public Finance", "Government Operations and Politics"])
               | Action.domain.is_(None))
        .limit(limit)
    ).all()
    fixed = 0
    seen: set[int] = set()
    for aid, title, _domain, otext in rows:
        if aid in seen:
            continue
        seen.add(aid)
        new_domain = classify_executive_domain(f"{title}. {otext or ''}")
        if new_domain:
            session.execute(
                sa_update(Action).where(Action.id == aid).values(domain=new_domain)
            )
            fixed += 1
    session.flush()
    return fixed


def backfill_vote_categories(session: Session, *, limit: int = 20000) -> int:
    """Fill in the topic for roll-call votes that were ingested before the bill they reference
    was in our record. Looks up the bill (congress + bill_number) and applies its category."""
    from degreezeor.categories import category_for
    from degreezeor.core.models import Vote

    bill_idx: dict[tuple[int, str], str | None] = {}
    for congress, bn, domain in session.execute(
        select(Bill.congress, Bill.bill_number, Action.domain)
        .join(Action, Action.id == Bill.action_id)
    ).all():
        if congress and bn:
            bill_idx[(congress, bn)] = domain
    rows = session.execute(
        select(Vote.id, Vote.congress, Vote.bill_number)
        .where(Vote.category.is_(None), Vote.bill_number.is_not(None),
               Vote.roll_call.is_not(None))
        .limit(limit)
    ).all()
    fixed = 0
    for vid, congress, bn in rows:
        key = (congress, bn)
        if key not in bill_idx:
            continue
        cat = category_for(bill_idx[key], "bill")
        session.execute(sa_update(Vote).where(Vote.id == vid).values(category=cat))
        fixed += 1
    session.flush()
    return fixed


def backfill_eo_signers(session: Session, *, limit: int = 5000) -> int:
    """Repair executive orders scored before their president's term was on record (e.g. EOs
    signed after the last known term started): set the signer from the action date and
    re-derive the (deterministic) attribution so the EO appears on that president's record.

    The EO composite is signer-independent — only the official rollup changes — so this
    produces exactly what a fresh scoring run would, without re-scoring or touching the audit
    chain of scores. Idempotent: once a signer is set the EO is no longer selected."""
    from degreezeor.core.reference import president_on

    rows = session.execute(
        select(ExecutiveOrder, Action)
        .join(Action, Action.id == ExecutiveOrder.action_id)
        .where(ExecutiveOrder.signing_official_id.is_(None), Action.action_date.is_not(None))
        .limit(limit)
    ).all()
    fixed = 0
    for eo, action in rows:
        pres = president_on(session, action.action_date)
        if pres is None:
            continue
        eo.signing_official_id = pres.id
        for eu in session.execute(
            select(EvaluationUnit).where(EvaluationUnit.action_id == action.id)
        ).scalars().all():
            # Only re-derive for FINALIZED EUs (those already carry attribution rows); leave
            # insufficient/non-scoreable EUs untouched so nothing un-scored gains attribution.
            if session.execute(
                select(AttributionWeight.id).where(AttributionWeight.eu_id == eu.id).limit(1)
            ).scalar() is None:
                continue
            actx = AttributionContext(
                eu_id=eu.id, action_type=action.type, sponsor_official_id=None,
                signer_official_id=pres.id, vote_margin=None, member_on_winning_side=None,
            )
            attributions = build_attribution(actx)
            session.execute(sa_delete(AttributionWeight).where(AttributionWeight.eu_id == eu.id))
            for a in attributions:
                session.add(AttributionWeight(
                    eu_id=eu.id, official_id=a.official_id, role=a.role,
                    authority=q_score(a.authority), pivotality=q_score(a.pivotality),
                    attribution=q_score(a.attribution), attr_ci_low=q_score(a.attr_ci_low),
                    attr_ci_high=q_score(a.attr_ci_high), is_residual=a.is_residual,
                ))
        fixed += 1
    session.flush()
    return fixed


def batch_score_regulations(session: Session, limit: int = 25) -> list[ScoreOutcome]:
    """Breadth: ingest + score recent final agency rules (Federal Register, keyless).
    Most land insufficient-evidence (a single federal series can't isolate one rule) —
    the honest denominator. Idempotent (skips rules already scored)."""
    import json as _json

    from degreezeor.ingestion.adapters.federalregister import federal_register_adapter
    from degreezeor.ingestion.http import client as _client

    url = f"{federal_register_adapter.base_url}/documents.json"
    params = {
        "conditions[type][]": "RULE",
        "order": "newest", "per_page": str(min(limit, 100)),
    }
    content = _client.get_bytes(url, params=params)
    docs = _json.loads(content).get("results", [])
    results: list[ScoreOutcome] = []
    for d in docs[:limit]:
        doc_number = d.get("document_number")
        if not doc_number:
            continue
        r = _isolated(session, lambda dn=doc_number: score_regulation(session, dn),
                      label=f"batch regulation {doc_number}")
        if r is not None:
            results.append(r)
    return results


@dataclass
class CourtSurvivalSpec:
    """A curated, source-linked judicial-review outcome for an executive order.

    The disposition is a curated public FACT (not NLP-inferred); CourtListener supplies
    case metadata for provenance. Survival index: upheld=100, partial=50, struck=0.
    Ambiguous/ongoing cases are marked 'pending' and left non-scoreable.
    """

    key: str
    eo_document_number: str  # Federal Register doc number of the EO
    disposition: str  # upheld | partial | struck | pending
    case_query: str  # CourtListener search query for provenance
    note: str


_SURVIVAL_INDEX = {"upheld": 100.0, "partial": 50.0, "struck": 0.0}

# Curated set — only unambiguous, well-documented final outcomes are scored.
COURT_SURVIVAL_SPECS: dict[str, CourtSurvivalSpec] = {
    "EO13780-TRAVELBAN": CourtSurvivalSpec(
        key="EO13780-TRAVELBAN", eo_document_number="2017-04837", disposition="upheld",
        case_query="Trump v. Hawaii travel ban",
        note="Proclamation 9645 / EO 13780 travel restrictions UPHELD by the Supreme Court "
             "in Trump v. Hawaii (2018).",
    ),
    "EO14042-CONTRACTOR-VAX": CourtSurvivalSpec(
        key="EO14042-CONTRACTOR-VAX", eo_document_number="2021-19924", disposition="struck",
        case_query="Georgia v. Biden federal contractor vaccine mandate",
        note="EO 14042 federal-contractor vaccine mandate was nationally enjoined, never "
             "enforced, and later revoked.",
    ),
}


def score_court_survival(session: Session, spec: CourtSurvivalSpec) -> ScoreOutcome:
    """Court-survival vertical: how much of an executive order survived judicial review?
    Curated disposition (source-linked) -> survival index; attributed to the issuing
    president with a large residual (judicial composition is exogenous)."""
    from degreezeor.ingestion.adapters.courtlistener import courtlistener_adapter

    if spec.disposition == "pending" or spec.disposition not in _SURVIVAL_INDEX:
        # Not final -> honest non-scoreable (don't score ongoing litigation).
        action = load_executive_order(session, spec.eo_document_number)
        eu = EvaluationUnit(action_id=action.id, status="insufficient_evidence",
                            non_scoreable_reason="Litigation not final (disposition pending).",
                            evaluation_mode="target")
        session.add(eu)
        session.flush()
        return ScoreOutcome(action.id, eu.id, eu.status, None, None)

    action = load_executive_order(session, spec.eo_document_number)
    cl_src = ensure_source(session, name=courtlistener_adapter.name, tier=courtlistener_adapter.tier,
                           base_url=courtlistener_adapter.base_url)
    # Provenance: fetch + land the case metadata (NOT used to infer the disposition).
    cfetch = courtlistener_adapter.fetch(spec.case_query)
    land(session, cfetch)
    case = courtlistener_adapter.top_case(cfetch.content) or {}
    case_url = case.get("url") or cfetch.source_url

    survival = _SURVIVAL_INDEX[spec.disposition]
    metric = session.execute(
        select(Metric).where(Metric.code == "legal_survival")
    ).scalar_one_or_none()
    if metric is None:
        metric = Metric(code="legal_survival", name="Legal survival index (judicial review)",
                        unit="index", direction_good="up", source_id=cl_src.id,
                        native_series_id="CURATED:court_survival", domain="Law")
        session.add(metric)
        session.flush()
    existing = _existing_outcome_for_metric(session, action.id, metric.id)
    if existing is not None:  # idempotent (cron-safe)
        return existing
    obj = Objective(action_id=action.id, source_id=cl_src.id, source_url=case_url,
                    objective_level="executive",
                    text=(f"Survive judicial review: {spec.note} (disposition: {spec.disposition}; "
                          f"source: {case.get('case_name') or spec.case_query})."))
    session.add(obj)
    session.flush()
    eu = EvaluationUnit(action_id=action.id, objective_id=obj.id, metric_id=metric.id,
                        lag_window_months=0, sign_goal=1, status="pending",
                        evaluation_mode="target", target_value=D("100"),
                        realized_value=D(str(survival)), directly_attributable=True)
    session.add(eu)
    session.flush()
    preregister(session, eu, action_native_id=action.native_identifier, metric_code=metric.code,
                objective_level="executive", sign_goal=1, lag_window_months=0,
                masked_objective=f"court_survival disposition={spec.disposition}"[:280])

    from degreezeor.scoring.target_outcome import compute_target_outcome
    event_period = f"{action.action_date.year}-{action.action_date.month:02d}-01"
    tc = compute_target_outcome(realized=survival, target=100.0, sign_goal=1,
                                directly_attributable=True, eval_period=event_period)
    eo = session.get(ExecutiveOrder, action.id)
    actx = AttributionContext(eu_id=eu.id, action_type="eo", sponsor_official_id=None,
                              signer_official_id=eo.signing_official_id if eo else None,
                              vote_margin=None, member_on_winning_side=None)
    attributions = build_attribution(actx)
    return _finalize(session, eu, action, tc.outcome, attributions, alignment=D("0.95"),
                     observations=[], metric=metric, sign_goal=1, event_period=event_period,
                     extra_source_urls=[case_url], s_outcome_override=tc.s_outcome, definitive=True)


def score_budget_execution(
    session: Session, toptier_code: str, agency_name: str, fiscal_year: int,
    realized_kind: str = "obligated",
) -> ScoreOutcome:
    """#2 — account-level budget execution: did the agency obligate/outlay the budgetary
    resources available to it in a fiscal year? Stable + commensurable by construction
    (obligated/outlayed <= resources). Directly attributable to the administration (with a
    large residual, since execution is diffuse across the agency and career staff)."""
    from degreezeor.core.reference import ensure_us_federal, president_on
    from degreezeor.ingestion.adapters.usaspending import usaspending_adapter
    from degreezeor.scoring.target_outcome import compute_target_outcome

    usa_src = ensure_source(session, name=usaspending_adapter.name, tier=usaspending_adapter.tier,
                            base_url=usaspending_adapter.base_url)
    jur = ensure_us_federal(session)
    native_id = f"BUDGET:{toptier_code}:{fiscal_year}"
    fy_end = date(fiscal_year, 9, 30)

    bfetch = usaspending_adapter.fetch_agency_budget(toptier_code)
    amounts = usaspending_adapter.parse_agency_budget(bfetch.content, fiscal_year)

    existing = session.execute(
        select(Action).where(Action.native_identifier == native_id, Action.type == "budget")
    ).scalar_one_or_none()
    if existing is not None:
        out = _existing_outcome(session, existing.id)
        if out is not None:
            return out
        action = existing
    else:
        action = Action(
            type="budget", title=f"{agency_name} — FY{fiscal_year} budget execution",
            action_date=fy_end, jurisdiction_id=jur.id, source_id=usa_src.id,
            source_url=bfetch.source_url, native_identifier=native_id,
            content_hash=bfetch.content_hash, domain="Economics and Public Finance",
            implemented=True,
        )
        session.add(action)
        session.flush()
    land(session, bfetch)

    eu = EvaluationUnit(action_id=action.id, status="pending", evaluation_mode="target",
                        sign_goal=1, lag_window_months=0, directly_attributable=True)
    if amounts is None or amounts["resources"] <= 0:
        eu.status = "non_scoreable_no_metric"
        eu.non_scoreable_reason = f"No budgetary-resources data for {agency_name} FY{fiscal_year}."
        session.add(eu)
        session.flush()
        return ScoreOutcome(action.id, eu.id, eu.status, None, None)

    metric = session.execute(
        select(Metric).where(Metric.code == f"agency_budget_{toptier_code}_{fiscal_year}_{realized_kind}")
    ).scalar_one_or_none()
    if metric is None:
        metric = Metric(
            code=f"agency_budget_{toptier_code}_{fiscal_year}_{realized_kind}",
            name=f"{agency_name} FY{fiscal_year} {realized_kind} (USAspending)",
            unit="USD", direction_good="up", source_id=usa_src.id,
            native_series_id=f"AGENCYBUDGET:{toptier_code}:{fiscal_year}:{realized_kind}",
            domain="Economics and Public Finance",
        )
        session.add(metric)
        session.flush()
    obj = Objective(action_id=action.id, source_id=usa_src.id, source_url=bfetch.source_url,
                    objective_level="operational",
                    text=(f"Obligate/outlay the budgetary resources available to {agency_name} "
                          f"in FY{fiscal_year} (execution of appropriated funds)."))
    session.add(obj)
    session.flush()
    eu.objective_id = obj.id
    eu.metric_id = metric.id
    eu.target_value = D(str(amounts["resources"]))
    session.add(eu)
    session.flush()

    preregister(session, eu, action_native_id=native_id, metric_code=metric.code,
                objective_level="operational", sign_goal=1, lag_window_months=0,
                masked_objective=f"budget_execution {agency_name} FY{fiscal_year} {realized_kind}"[:280])

    realized = amounts[realized_kind]
    tc = compute_target_outcome(realized=realized, target=amounts["resources"], sign_goal=1,
                                directly_attributable=True, eval_period=f"{fiscal_year}-09-01")
    signer = president_on(session, fy_end)
    actx = AttributionContext(
        eu_id=eu.id, action_type="budget", sponsor_official_id=None,
        signer_official_id=signer.id if signer else None,
        vote_margin=None, member_on_winning_side=None,
    )
    attributions = build_attribution(actx)
    return _finalize(session, eu, action, tc.outcome, attributions, alignment=D("0.95"),
                     observations=[], metric=metric, sign_goal=1,
                     event_period=f"{fiscal_year}-09-01", extra_source_urls=[bfetch.source_url],
                     s_outcome_override=tc.s_outcome)


def ingest_budget_execution(
    session: Session, fiscal_year: int, agencies: list[tuple[str, str]] | None = None,
    realized_kind: str = "obligated", limit: int | None = None, all_agencies: bool = False,
) -> list[ScoreOutcome]:
    """Batch budget-execution scores. ``agencies`` = list of (toptier_code, name); if None,
    use the major cabinet departments — or, with ``all_agencies=True``, EVERY toptier agency
    (independent agencies like EPA/NASA/SSA included). Execution rate is commensurable by
    construction (obligated/outlayed <= resources), so this is reliable verifiable coverage,
    not fragile volume."""
    import json as _json

    from degreezeor.ingestion.adapters.usaspending import usaspending_adapter

    if agencies is None:
        allag = _json.loads(usaspending_adapter.fetch_toptier_agencies()).get("results", [])
        if all_agencies:
            agencies = [(a["toptier_code"], a["agency_name"]) for a in allag]
        else:
            wanted = {"Department of"}  # cabinet departments
            agencies = [(a["toptier_code"], a["agency_name"]) for a in allag
                        if any(a["agency_name"].startswith(w) for w in wanted)]
    results: list[ScoreOutcome] = []
    for code, name in agencies:
        if limit is not None and len(results) >= limit:
            break
        r = _isolated(session,
                      lambda c=code, n=name: score_budget_execution(session, c, n, fiscal_year, realized_kind),
                      label=f"budget execution {name} FY{fiscal_year}")
        if r is not None:
            results.append(r)
    return results


def ingest_state_policies(session: Session, keys: list[str] | None = None) -> list[ScoreOutcome]:
    """Tier-4 batch: score curated state policies via synthetic control. The pre-fit gate
    decides which are scoreable (poor donor fit -> honest non-scoreable)."""
    results: list[ScoreOutcome] = []
    for key in (keys or list(STATE_POLICIES)):
        spec = STATE_POLICIES.get(key)
        if spec is None:
            continue
        r = _isolated(session, lambda spec=spec: score_state_policy(session, spec),
                      label=f"state policy {key}")
        if r is not None:
            results.append(r)
    return results


def ingest_member_bills(
    session: Session, congress: int, *, new_limit: int = 1000, max_pages: int = 30,
    page_size: int = 250, with_cosponsors: bool = True,
) -> int:
    """Record the bills members SPONSORED in a Congress as categorized, unscored 'recorded'
    actions, so an official's full record of WHAT THEY ACTED ON (by topic) is visible even
    when nothing can be scored. This is the activity/record layer, distinct from the scored
    outcome layer; it never produces a composite.

    Idempotent + incremental: skips bills already stored and inserts up to ``new_limit`` new
    ones per call (bounding per-bill detail fetches). The list is newest-updated first, so
    recent activity is ingested first; repeated cron runs fill in the rest. Sponsors are
    resolved to officials with their real names (so this also repairs vote-derived stubs)."""
    from degreezeor.core.reference import ensure_party_term, ensure_us_federal
    from degreezeor.ingestion.adapters.congress import congress_adapter

    ensure_source(session, name=congress_adapter.name, tier=congress_adapter.tier,
                  base_url=congress_adapter.base_url)
    src_id = session.execute(
        select(DataSource.id).where(DataSource.name == congress_adapter.name)
    ).scalar_one()
    jur = ensure_us_federal(session)

    def _resolve_member(member: dict) -> Official | None:
        """Get-or-create an official from a Congress.gov member dict (sponsor or cosponsor),
        upgrading a vote-derived name stub to a real name and recording party (audit only)."""
        bio = member.get("bioguideId")
        if not bio:
            return None
        name = " ".join(x for x in [member.get("firstName"), member.get("lastName")] if x).strip() \
            or member.get("fullName") or bio
        official = session.execute(
            select(Official).where(Official.bioguide_id == bio)
        ).scalar_one_or_none()
        if official is None:
            official = Official(full_name=name, bioguide_id=bio)
            session.add(official)
            session.flush()
        elif official.full_name and (
            len(official.full_name.split()) <= 1 or "(" in official.full_name or "[" in official.full_name
        ):
            official.full_name = name
        if member.get("party"):
            ensure_party_term(session, official, member["party"])
        return official

    inserted = 0
    for page in range(max_pages):
        if inserted >= new_limit:
            break
        lf = congress_adapter.fetch_bill_list(congress, limit=page_size, offset=page * page_size)
        items = json.loads(lf.content).get("bills", [])
        if not items:
            break
        for it in items:
            if inserted >= new_limit:
                break
            btype = (it.get("type") or "").lower()
            num = it.get("number")
            if not btype or num is None:
                continue
            native = f"bill/{congress}/{btype}/{num}"
            if session.execute(
                select(Action.id).where(Action.native_identifier == native)
            ).scalar_one_or_none():
                continue  # already recorded (idempotent)
            try:
                df = congress_adapter.fetch_bill(congress, btype, int(num))
            except Exception:  # noqa: BLE001 - one bad bill must not abort the batch
                continue
            bill = json.loads(df.content).get("bill", {})
            sponsors = bill.get("sponsors") or []
            if not sponsors or not sponsors[0].get("bioguideId"):
                continue
            land(session, df)
            official = _resolve_member(sponsors[0])
            if official is None:
                continue
            intro = bill.get("introducedDate")
            action = Action(
                type="bill", title=bill.get("title") or native,
                action_date=date.fromisoformat(intro) if intro else None,
                jurisdiction_id=jur.id, source_id=src_id, source_url=df.source_url,
                native_identifier=native, content_hash=df.content_hash,
                domain=(bill.get("policyArea") or {}).get("name"),
            )
            session.add(action)
            session.flush()
            session.add(Bill(action_id=action.id, congress=congress,
                             bill_number=f"{btype.upper()}{num}", sponsor_official_id=official.id,
                             status="introduced"))
            # Cosponsors: who else backed it (best-effort; the bill is recorded either way).
            if with_cosponsors:
                try:
                    cf = congress_adapter.fetch_bill_cosponsors(congress, btype, int(num))
                    seen_co: set[int] = set()
                    for co in json.loads(cf.content).get("cosponsors", []):
                        cofficial = _resolve_member(co)
                        if cofficial is None or cofficial.id == official.id or cofficial.id in seen_co:
                            continue
                        seen_co.add(cofficial.id)
                        session.add(BillCosponsor(action_id=action.id, official_id=cofficial.id))
                except Exception:  # noqa: BLE001 - cosponsors are best-effort
                    pass
            inserted += 1
        session.flush()
    return inserted


def _norm_legis_num(legis_num: str) -> str | None:
    """Normalize a Clerk ``legis-num`` ('H R 1', 'H RES 5') to our bill_number ('HR1', 'HRES5').
    Returns None for non-bill votes (QUORUM, MOTION, journal, etc.)."""
    norm = (legis_num or "").replace(" ", "").replace(".", "").upper()
    if not norm or not any(ch.isdigit() for ch in norm) or not norm[0].isalpha():
        return None
    return norm


def ingest_house_votes(
    session: Session, year: int, *, new_limit: int = 250, max_roll: int = 900,
) -> int:
    """Activity/record layer: every House recorded (roll-call) vote for a calendar year, as
    Vote + VotePosition rows (the full member record). Keyless (clerk.house.gov), idempotent
    on the roll-call URL, resumable (skips rolls already stored), capped per run. Votes are
    categorized via the bill they reference when that bill is in our record."""
    import re

    from degreezeor.categories import category_for
    from degreezeor.core.models import Vote, VotePosition
    from degreezeor.ingestion.adapters.house_clerk import house_clerk_adapter, parse_house_vote

    congress = (year - 1789) // 2 + 1
    prefix = f"https://clerk.house.gov/evs/{year}/roll"
    done: set[int] = set()
    for (q,) in session.execute(select(Vote.question).where(Vote.question.like(prefix + "%"))).all():
        m = re.search(r"/roll(\d+)\.xml", q or "")
        if m:
            done.add(int(m.group(1)))

    # Bill (congress, bill_number) -> (action_id, domain) for categorizing votes by topic.
    bill_idx: dict[str, tuple[int, str | None]] = {}
    for bn, aid, domain in session.execute(
        select(Bill.bill_number, Bill.action_id, Action.domain)
        .join(Action, Action.id == Bill.action_id)
        .where(Bill.congress == congress)
    ).all():
        if bn:
            bill_idx[bn] = (aid, domain)

    inserted = 0
    misses = 0
    for roll in range(1, max_roll + 1):
        if inserted >= new_limit:
            break
        if roll in done:
            continue
        url = f"{prefix}{roll:03d}.xml"
        try:
            vf = house_clerk_adapter.fetch(url)
        except Exception:  # noqa: BLE001 - a 404 means we've passed the last roll of the year
            misses += 1
            if misses >= 2:
                break
            continue
        misses = 0
        try:
            hv = parse_house_vote(vf.content)
        except Exception:  # noqa: BLE001 - one malformed file must not abort the batch
            continue
        if not hv.positions:
            continue
        land(session, vf)
        bn = _norm_legis_num(hv.legis_num)
        linked = bill_idx.get(bn) if bn else None
        category = category_for(linked[1], "bill") if linked else None
        vote = Vote(
            action_id=linked[0] if linked else None, chamber="house", question=url,
            vote_date=hv.vote_date, yea=hv.yea, nay=hv.nay, present=hv.present,
            not_voting=hv.not_voting, result=hv.result or None,
            congress=hv.congress or congress, roll_call=hv.rollcall_num or roll,
            bill_number=bn, category=category,
        )
        session.add(vote)
        session.flush()
        seen_v: set[int] = set()
        for mv in hv.positions:
            if not mv.bioguide_id:
                continue
            official = session.execute(
                select(Official).where(Official.bioguide_id == mv.bioguide_id)
            ).scalar_one_or_none()
            if official is None:
                official = Official(full_name=mv.name or mv.bioguide_id, bioguide_id=mv.bioguide_id)
                session.add(official)
                session.flush()
            if official.id in seen_v:
                continue
            seen_v.add(official.id)
            session.add(VotePosition(vote_id=vote.id, official_id=official.id, position=mv.position))
        inserted += 1
    session.flush()
    return inserted


def ingest_senate_votes(
    session: Session, year: int, *, new_limit: int = 250, max_vote: int = 800,
) -> int:
    """Activity/record layer: every Senate recorded (roll-call) vote for a calendar year, as
    Vote + VotePosition rows. Keyless (senate.gov); senators key on ``lis_member_id`` so we
    resolve each to a Bioguide-keyed Official via the crosswalk. Idempotent on the roll-call
    URL, resumable, capped per run, categorized via the referenced bill when in our record."""
    import re

    from degreezeor.categories import category_for
    from degreezeor.core.models import Vote, VotePosition
    from degreezeor.ingestion.adapters.congress_legislators import congress_legislators_adapter
    from degreezeor.ingestion.adapters.senate import parse_senate_vote, senate_rollcall_adapter

    congress = (year - 1789) // 2 + 1
    sess = year - (1789 + 2 * (congress - 1)) + 1  # 1 = first (odd) year, 2 = second (even)
    base = "https://www.senate.gov/legislative/LIS/roll_call_votes"
    prefix = f"{base}/vote{congress}{sess}/vote_{congress}_{sess}_"
    done: set[int] = set()
    for (q,) in session.execute(select(Vote.question).where(Vote.question.like(prefix + "%"))).all():
        m = re.search(r"_(\d+)\.xml", q or "")
        if m:
            done.add(int(m.group(1)))

    bill_idx: dict[str, tuple[int, str | None]] = {}
    for bn, aid, domain in session.execute(
        select(Bill.bill_number, Bill.action_id, Action.domain)
        .join(Action, Action.id == Bill.action_id)
        .where(Bill.congress == congress)
    ).all():
        if bn:
            bill_idx[bn] = (aid, domain)

    lis_map = congress_legislators_adapter.lis_to_bioguide()
    inserted = 0
    misses = 0
    for n in range(1, max_vote + 1):
        if inserted >= new_limit:
            break
        if n in done:
            continue
        url = f"{prefix}{n:05d}.xml"
        try:
            vf = senate_rollcall_adapter.fetch(url)
            sv = parse_senate_vote(vf.content)
        except Exception:  # noqa: BLE001 - a missing vote (301/redirect) ends the session
            misses += 1
            if misses >= 2:
                break
            continue
        if not sv.positions:
            misses += 1
            if misses >= 2:
                break
            continue
        misses = 0
        land(session, vf)
        bn = _norm_legis_num(sv.document)
        linked = bill_idx.get(bn) if bn else None
        category = category_for(linked[1], "bill") if linked else None
        vote = Vote(
            action_id=linked[0] if linked else None, chamber="senate", question=url,
            vote_date=sv.vote_date, yea=sv.yea, nay=sv.nay, present=sv.present,
            not_voting=sv.not_voting, result=sv.result,
            congress=sv.congress or congress, roll_call=sv.vote_number or n,
            bill_number=bn, category=category,
        )
        session.add(vote)
        session.flush()
        seen_v: set[int] = set()
        for mv in sv.positions:
            bio = lis_map.get(mv.lis_member_id)
            if not bio:
                continue
            official = session.execute(
                select(Official).where(Official.bioguide_id == bio)
            ).scalar_one_or_none()
            name = " ".join(x for x in [mv.first_name, mv.last_name] if x).strip() or bio
            if official is None:
                official = Official(full_name=name, bioguide_id=bio)
                session.add(official)
                session.flush()
            elif official.full_name and (
                len(official.full_name.split()) <= 1 or "(" in official.full_name
            ):
                official.full_name = name
            if official.id in seen_v:
                continue
            seen_v.add(official.id)
            session.add(VotePosition(vote_id=vote.id, official_id=official.id, position=mv.position))
        inserted += 1
    session.flush()
    return inserted


def refresh_all(
    session: Session, *, budget_fiscal_year: int = 2024, congress: int = 117,
    law_limit: int = 25, eo_limit: int = 15,
) -> dict[str, int]:
    """Idempotent full ingestion/scoring pass — the production CRON entrypoint.

    Every scorer skips already-scored units, so this can run on a schedule without
    creating duplicates. Returns a per-stage count of evaluation units produced.
    """
    counts: dict[str, int] = {}

    def _safe_rollback() -> None:
        # Reset a session left broken by a dropped/recycled DB connection (e.g. a free-tier
        # AdminShutdown), so the next stage can start a fresh transaction instead of cascading.
        with contextlib.suppress(Exception):
            session.rollback()

    def _safe_commit(name: str) -> None:
        try:
            session.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("commit after %s failed: %s", name, exc)
            _safe_rollback()

    # Commit after each stage so partial progress is DURABLE and VISIBLE during a long run,
    # and a dropped connection / late failure / cron time limit never discards earlier work.
    def _stage(name: str, fn) -> None:
        try:
            counts[name] = fn()
            _safe_commit(name)
        except Exception as exc:  # noqa: BLE001 - a stage failure must not abort the whole run
            log.warning("refresh stage %s failed: %s", name, exc)
            counts.setdefault(name, 0)
            _safe_rollback()

    # Activity/record layer FIRST so the recent, broad record of what members sponsored +
    # cosponsored appears early each run. Recency-first, capped per run (sponsor + cosponsor
    # calls per bill respect the per-key budget); repeated runs fill in the rest.
    def _member_bills() -> int:
        total = 0
        for c in (119, 118, 117, 116):
            cap = 700 if c >= 118 else 400
            try:
                total += ingest_member_bills(session, c, new_limit=cap)
                _safe_commit(f"member bills {c}")
            except Exception as exc:  # noqa: BLE001 - never fatal
                log.warning("member-bill ingestion for congress %s skipped: %s", c, exc)
                _safe_rollback()
        return total
    _stage("member_bills", _member_bills)

    # Roll-call votes: how members voted, recency-first, capped per run. Keyless.
    def _chamber_votes(fn, label: str) -> int:
        total = 0
        this_year = date.today().year
        for y in range(this_year, this_year - 4, -1):
            try:
                total += fn(session, y, new_limit=200)
                _safe_commit(f"{label} votes {y}")
            except Exception as exc:  # noqa: BLE001 - never fatal
                log.warning("%s-vote ingestion for %s skipped: %s", label, y, exc)
                _safe_rollback()
        return total
    _stage("house_votes", lambda: _chamber_votes(ingest_house_votes, "house"))
    _stage("senate_votes", lambda: _chamber_votes(ingest_senate_votes, "senate"))

    _stage("defc_delivery", lambda: len(ingest_defc_delivery(session)))

    # All toptier agencies, across the last few fiscal years: execution rate is reliable and
    # commensurable by construction (obligated/outlayed <= resources). Each agency-year is distinct.
    def _budget() -> int:
        be = 0
        for fy in (budget_fiscal_year, budget_fiscal_year - 1, budget_fiscal_year - 2):
            try:
                be += len(ingest_budget_execution(session, fy, all_agencies=True))
                _safe_commit(f"budget FY{fy}")
            except Exception as exc:  # noqa: BLE001
                log.warning("budget execution FY%s skipped: %s", fy, exc)
                _safe_rollback()
        return be
    _stage("budget_execution", _budget)

    _stage("state_policies", lambda: len(ingest_state_policies(session)))
    _stage("court_survival", lambda: sum(
        1 for spec in COURT_SURVIVAL_SPECS.values()
        if _isolated(session, lambda spec=spec: score_court_survival(session, spec),
                     label=f"court survival {spec.key}")))
    _stage("curated_targets", lambda: sum(
        1 for key in ("CARES-DELIVERY", "IIJA-DELIVERY", "UKRAINE-2022-DELIVERY")
        if _isolated(session, lambda key=key: score_target(session, TARGET_SPECS[key]),
                     label=f"curated target {key}")))
    _stage("laws", lambda: len(batch_score_laws(session, congress, limit=law_limit)))
    _stage("executive_actions", lambda: ingest_executive_actions(session))
    _stage("executive_orders", lambda: len(batch_score_executive_orders(session, limit=eo_limit)))
    _stage("eo_signers_backfilled", lambda: backfill_eo_signers(session))
    _stage("eo_sources_backfilled", lambda: backfill_eo_source_urls(session))
    _stage("eo_categories_backfilled", lambda: backfill_eo_categories(session))
    _stage("vote_categories_backfilled", lambda: backfill_vote_categories(session))
    _stage("regulations", lambda: len(batch_score_regulations(session, limit=eo_limit)))

    # Best-effort name enrichment (bounded by Congress.gov throughput).
    def _enrich() -> int:
        from degreezeor.ingestion.loader import enrich_official_names
        return enrich_official_names(session)
    _stage("names_enriched", _enrich)

    # Keep the directory clean: drop official records that carry no record anywhere (artifacts).
    _stage("officials_purged", lambda: purge_empty_officials(session))

    # Self-validate: the nightly pass must leave the append-only audit chain intact. Guarded so a
    # dropped connection at the very end reports unknown rather than crashing the whole command.
    _safe_rollback()
    try:
        chain_ok, broken_id = audit.verify_chain(session)
    except Exception as exc:  # noqa: BLE001
        log.warning("audit chain verification skipped (session/connection issue): %s", exc)
        counts["audit_chain_ok"] = 0
        return counts
    counts["audit_chain_ok"] = 1 if chain_ok else 0
    if not chain_ok:
        log.error("AUDIT CHAIN BROKEN after refresh (first broken record id=%s)", broken_id)
    return counts


@dataclass(frozen=True)
class ReproCheck:
    eu_id: int
    status: str  # reproduced | mismatch | error
    stored_hash: str | None
    recomputed_hash: str | None
    detail: str | None = None


@dataclass(frozen=True)
class ReproAudit:
    total: int  # scored EUs checked (those with a pinned reproducible hash)
    reproduced: int
    mismatched: int
    errored: int
    checks: list[ReproCheck]

    @property
    def all_reproduced(self) -> bool:
        # An audit "passes" only if every checkable score reproduced AND none mismatched.
        # Errors (e.g. a cold cache that can't re-fetch a series) are inconclusive, not
        # failures, but are reported so an operator can investigate.
        return self.mismatched == 0 and self.errored == 0 and self.total > 0


def verify_all_reproducible(session: Session) -> ReproAudit:
    """Platform-wide reproducibility self-audit (PLAN §9.9 / §16).

    Independently RE-RUNS every published score from its stored inputs and asserts each
    one reproduces its pinned ``reproducible_hash`` bit-for-bit — the operational proof
    that scores are deterministic and untampered. Each re-run happens inside a SAVEPOINT
    that is rolled back, so the audit never mutates the database (no extra score runs).

    A mismatch means the stored score does not regenerate from its recorded inputs +
    methodology — i.e. non-determinism or tampering — and is a hard failure. An error
    (e.g. a cold replay cache) is inconclusive and reported separately.
    """
    checks: list[ReproCheck] = []
    eus = session.execute(select(EvaluationUnit)).scalars().all()
    for eu in eus:
        run = session.execute(
            select(ScoreRun).where(ScoreRun.eu_id == eu.id).order_by(ScoreRun.id.desc()).limit(1)
        ).scalar_one_or_none()
        if run is None or run.reproducible_hash is None:
            continue  # not a scored EU
        stored = run.reproducible_hash
        sp = session.begin_nested()
        status, recomputed, detail = "error", None, None
        try:
            result = rescore_eu(session, eu.id)
            recomputed = result.reproducible_hash
            status = "reproduced" if recomputed == stored else "mismatch"
        except Exception as exc:  # noqa: BLE001 - inconclusive (e.g. cold cache), not a failure
            detail = str(exc)[:200]
        finally:
            if sp.is_active:
                sp.rollback()
            # Drop any stale identity-map state from the rolled-back re-run before the
            # next EU, so each check reads fresh persisted rows.
            session.expire_all()
        checks.append(ReproCheck(eu.id, status, stored, recomputed, detail))

    return ReproAudit(
        total=len(checks),
        reproduced=sum(1 for c in checks if c.status == "reproduced"),
        mismatched=sum(1 for c in checks if c.status == "mismatch"),
        errored=sum(1 for c in checks if c.status == "error"),
        checks=checks,
    )


def _ensure_named_official(session: Session, name: str) -> Official:
    o = session.execute(select(Official).where(Official.full_name == name)).scalar_one_or_none()
    if o is None:
        o = Official(full_name=name)
        session.add(o)
        session.flush()
    return o


def _ensure_state_jurisdiction(session: Session, fips: str, name: str) -> Jurisdiction:
    j = session.execute(
        select(Jurisdiction).where(Jurisdiction.type == "state", Jurisdiction.fips == fips)
    ).scalar_one_or_none()
    if j is None:
        j = Jurisdiction(type="state", name=name, fips=fips)
        session.add(j)
        session.flush()
    return j


def score_state_policy(session: Session, spec: StatePolicySpec) -> ScoreOutcome:
    """Score a state policy via comparison-design baselines (synthetic control / DiD)
    on real BLS state employment data, with treated-vs-donor structure.

    This is the path that can legitimately clear the confidence gate and produce a
    composite, because a donor pool addresses the confounding a single series cannot.
    """
    from degreezeor.ingestion.loader import ensure_census_source, ensure_eia_source

    ensure_bls_source(session)
    kind = spec.metric_kind
    descr = STATE_METRIC_KINDS.get(kind, STATE_METRIC_KINDS["employment"])
    # sign_goal is fixed at pre-registration and depends on the metric: more jobs/wages/income
    # is toward the goal (+1); less poverty/uninsured/CO2 is toward the goal (-1).
    sign_goal = int(descr["sign"])

    # --- Tier-0 action provenance: fetch the official state source URL ---
    fetch = generic_url_adapter.fetch(spec.source_url, label=spec.key)
    land(session, fetch)
    jur = _ensure_state_jurisdiction(session, spec.state_fips, spec.state_name)

    existing = session.execute(
        select(Action).where(Action.native_identifier == spec.key, Action.type == "law")
    ).scalar_one_or_none()
    if existing is None:
        action = Action(
            type="law", title=spec.title, action_date=date(spec.enacted_year, spec.enacted_month, 1),
            jurisdiction_id=jur.id, source_id=fetch_source_id(session), source_url=spec.source_url,
            native_identifier=spec.key, content_hash=fetch.content_hash,
            domain="Economics and Public Finance", implemented=True,
        )
        session.add(action)
        session.flush()
    else:
        action = existing

    signer = _ensure_named_official(session, spec.signer_name) if spec.signer_name else None
    sponsor = _ensure_named_official(session, spec.sponsor_name) if spec.sponsor_name else None
    from degreezeor.core.reference import ensure_party_term
    if signer and spec.signer_party:
        ensure_party_term(session, signer, spec.signer_party)
    if sponsor and spec.sponsor_party:
        ensure_party_term(session, sponsor, spec.sponsor_party)

    # Treated-state outcome metric (get-or-create), per the policy's comparison-design kind.
    nsid = state_series_id(spec.state_fips, kind)
    if nsid.startswith("CENSUS|"):
        src_id = ensure_census_source(session).id
    elif nsid.startswith("EIA|"):
        src_id = ensure_eia_source(session).id
    else:
        src_id = session.execute(select(DataSource.id).where(DataSource.name == "BLS")).scalar_one()
    metric = session.execute(
        select(Metric).where(Metric.code == f"{descr['code']}_{spec.state_fips}")
    ).scalar_one_or_none()
    if metric is None:
        metric = Metric(
            code=f"{descr['code']}_{spec.state_fips}",
            name=f"{spec.state_name} {descr['name']}",
            unit=str(descr["unit"]), direction_good=str(descr["direction_good"]),
            source_id=src_id, native_series_id=nsid, domain=str(descr["domain"]),
        )
        session.add(metric)
        session.flush()

    # Idempotent (cron-safe): if already scored, return BEFORE creating any child rows.
    # (A prior run already created this action's Law/Bill/Objective; re-inserting them
    # would violate the laws/bills primary key — this was the duplicate-key cron failure.)
    existing_out = _existing_outcome_for_metric(session, action.id, metric.id)
    if existing_out is not None:
        return existing_out

    # Create the action's child records once; guarded so a partial prior run can't duplicate.
    if session.get(Law, action.id) is None:
        session.add(Law(action_id=action.id, public_law_number=spec.key,
                        enacted_date=action.action_date, signed_by_official_id=signer.id if signer else None))
    if sponsor and session.get(Bill, action.id) is None:
        session.add(Bill(action_id=action.id, sponsor_official_id=sponsor.id, status="enacted",
                         became_law_action_id=action.id))
    obj = session.execute(
        select(Objective).where(Objective.action_id == action.id)
    ).scalars().first()
    if obj is None:
        obj = Objective(action_id=action.id, text=spec.objective_text, source_id=fetch_source_id(session),
                        source_url=spec.source_url, objective_level="statutory")
        session.add(obj)
        session.flush()

    eu = EvaluationUnit(action_id=action.id, objective_id=obj.id, metric_id=metric.id,
                        lag_window_months=spec.lag_window_months, sign_goal=sign_goal, status="pending")
    session.add(eu)
    session.flush()

    preregister(
        session, eu, action_native_id=spec.key, metric_code=metric.code,
        objective_level="statutory", sign_goal=sign_goal,
        lag_window_months=spec.lag_window_months, masked_objective=mask_party_and_name(spec.objective_text)[:280],
    )

    pre_years = _pre_years_for(nsid)
    start_year = spec.enacted_year - pre_years
    end_year = spec.enacted_year + (spec.lag_window_months // 12) + 2
    load_observations(session, metric, start_year, end_year)
    observations = _windowed_observations(
        session, metric.id, date(spec.enacted_year, spec.enacted_month, 1), spec.lag_window_months
    )
    event_period = f"{spec.enacted_year}-{spec.enacted_month:02d}-01"

    # Donor (control) states: land for provenance + build in-memory series for the design,
    # via the adapter that matches this metric kind (BLS / Census / EIA).
    donor_observations: dict[str, list[tuple[str, object]]] = {}
    donor_source_urls: list[str] = []
    for dfips in spec.donor_fips:
        dfetch, pts = _fetch_state_series_points(state_series_id(dfips, kind), start_year, end_year)
        land(session, dfetch)
        donor_source_urls.append(dfetch.source_url)
        donor_observations[dfips] = pts

    comp = compute_outcome(
        observations, event_period=event_period, lag_window_months=spec.lag_window_months,
        sign_goal=sign_goal, seed=settings.deterministic_seed, donor_observations=donor_observations,
    )
    if comp is None:
        eu.status = "insufficient_evidence"
        eu.non_scoreable_reason = "Insufficient outcome observations around the evaluation window."
        session.flush()
        return ScoreOutcome(action.id, eu.id, eu.status, None, None)

    actx = AttributionContext(
        eu_id=eu.id, action_type=action.type,
        sponsor_official_id=sponsor.id if sponsor else None,
        signer_official_id=signer.id if signer else None,
        vote_margin=None, member_on_winning_side=None,
    )
    attributions = build_attribution(actx)
    return _finalize(
        session, eu, action, comp, attributions,
        alignment=D("0.90"), observations=observations, metric=metric, sign_goal=sign_goal,
        event_period=event_period, donor_observations=donor_observations,
        extra_source_urls=donor_source_urls,
    )


def fetch_source_id(session: Session) -> int:
    return session.execute(
        select(DataSource.id).where(DataSource.name == generic_url_adapter.name)
    ).scalar_one()
