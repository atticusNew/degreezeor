"""Controlled metric catalog (official series only).

Each entry binds a stated-objective concept to a single official, recurring
statistical series with an explicit ``direction_good`` (which way moves the metric
*toward* the stated goal). Keywords drive the deterministic, party-masked
objective->metric matcher. New metrics are added here; the matcher needs no change.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricSpec:
    code: str
    name: str
    unit: str
    direction_good: str  # up|down|context
    source_name: str
    native_series_id: str
    domain: str
    # Lowercased objective keywords that select this metric.
    keywords: tuple[str, ...]
    # Default lag window (months) from enactment before the outcome is evaluated.
    default_lag_months: int = 24


CATALOG: list[MetricSpec] = [
    MetricSpec(
        code="nonfarm_employment",
        name="Total Nonfarm Employment (seasonally adjusted)",
        unit="thousands of jobs",
        direction_good="up",
        source_name="BLS",
        native_series_id="CES0000000001",
        domain="Economics and Public Finance",
        keywords=("create jobs", "preserve", "jobs", "employment", "payroll", "hire", "work"),
        default_lag_months=24,
    ),
    MetricSpec(
        code="unemployment_rate",
        name="Unemployment Rate (U-3, seasonally adjusted)",
        unit="percent",
        direction_good="down",
        source_name="BLS",
        native_series_id="LNS14000000",
        domain="Economics and Public Finance",
        keywords=("unemployment", "jobless", "joblessness", "out of work", "recovery"),
        default_lag_months=24,
    ),
    MetricSpec(
        code="cpi_all_items",
        name="CPI-U, All Items (1982-84=100, NSA)",
        unit="index",
        direction_good="down",  # for objectives that aim to *reduce* inflation/prices
        source_name="BLS",
        native_series_id="CUUR0000SA0",
        domain="Economics and Public Finance",
        keywords=("inflation", "prices", "cost of living", "price stability"),
        default_lag_months=18,
    ),
    MetricSpec(
        code="avg_hourly_earnings",
        name="Average Hourly Earnings, Total Private (SA)",
        unit="dollars/hour",
        direction_good="up",
        source_name="BLS",
        native_series_id="CES0500000003",
        domain="Economics and Public Finance",
        keywords=("minimum wage", "hourly wage", "hourly earnings", "wage rate", "raise wages"),
        default_lag_months=24,
    ),
    MetricSpec(
        code="job_openings",
        name="Job Openings, Total Nonfarm (JOLTS, SA)",
        unit="thousands",
        direction_good="up",
        source_name="BLS",
        native_series_id="JTS000000000000000JOL",
        domain="Economics and Public Finance",
        keywords=("job openings", "vacancies", "unfilled jobs", "hiring demand"),
        default_lag_months=24,
    ),
    MetricSpec(
        code="employment_population_ratio",
        name="Employment-Population Ratio (SA)",
        unit="percent",
        direction_good="up",
        source_name="BLS",
        native_series_id="LNS12300000",
        domain="Economics and Public Finance",
        keywords=("employment-population", "employment rate", "put people to work", "back to work"),
        default_lag_months=24,
    ),
    MetricSpec(
        code="labor_underutilization",
        name="Labor Underutilization, U-6 (SA)",
        unit="percent",
        direction_good="down",
        source_name="BLS",
        native_series_id="LNS13327709",
        domain="Economics and Public Finance",
        keywords=("underemployment", "underutilization", "discouraged workers", "marginally attached"),
        default_lag_months=24,
    ),
    MetricSpec(
        code="labor_force_participation",
        name="Labor Force Participation Rate (seasonally adjusted)",
        unit="percent",
        direction_good="up",
        source_name="BLS",
        native_series_id="LNS11300000",
        domain="Economics and Public Finance",
        keywords=("labor force", "participation", "workforce"),
        default_lag_months=24,
    ),
    # --- Health domain (CDC/NCHS, Tier-1, keyless Socrata) ---
    MetricSpec(
        code="life_expectancy",
        name="Life Expectancy at Birth, U.S. (NCHS)",
        unit="years",
        direction_good="up",
        source_name="CDC",
        native_series_id="CDC|w9j2-ggv5|year|average_life_expectancy|race=All Races;sex=Both Sexes",
        domain="Health",
        keywords=("life expectancy", "longevity", "premature death", "mortality reduction"),
        default_lag_months=60,
    ),
    MetricSpec(
        code="age_adjusted_death_rate",
        name="Age-adjusted Death Rate, U.S. (NCHS, per 100k)",
        unit="deaths per 100,000",
        direction_good="down",
        source_name="CDC",
        native_series_id="CDC|w9j2-ggv5|year|mortality|race=All Races;sex=Both Sexes",
        domain="Health",
        keywords=("death rate", "mortality rate", "reduce deaths", "public health", "save lives"),
        default_lag_months=60,
    ),
    # --- Socioeconomic domain (Census SAIPE, Tier-1; needs DZ_CENSUS_API_KEY) ---
    MetricSpec(
        code="poverty_rate",
        name="Poverty Rate, All Ages, U.S. (Census SAIPE)",
        unit="percent",
        direction_good="down",
        source_name="Census",
        native_series_id="CENSUS|timeseries/poverty/saipe|SAEPOVRTALL_PT",
        domain="Income and Poverty",
        keywords=("poverty", "poor", "low-income", "lift out of poverty", "reduce poverty"),
        default_lag_months=36,
    ),
    MetricSpec(
        code="median_household_income",
        name="Median Household Income, U.S. (Census SAIPE)",
        unit="dollars",
        direction_good="up",
        source_name="Census",
        native_series_id="CENSUS|timeseries/poverty/saipe|SAEMHI_PT",
        domain="Income and Poverty",
        keywords=("median income", "household income", "raise incomes", "middle class", "earnings of families"),
        default_lag_months=36,
    ),
    # --- Energy domain (EIA, Tier-1; needs DZ_EIA_API_KEY) ---
    MetricSpec(
        code="energy_co2_emissions",
        name="Total U.S. Energy CO2 Emissions (EIA, million metric tons)",
        unit="million metric tons CO2",
        direction_good="down",
        source_name="EIA",
        native_series_id="EIA|total-energy|TETCEUS",
        domain="Energy and Environment",
        keywords=("carbon", "co2", "emissions", "greenhouse gas", "decarbonize", "climate"),
        default_lag_months=48,
    ),
]

BY_CODE = {m.code: m for m in CATALOG}
