"""Objective, deterministic grouping of actions into user-facing categories.

Categories are derived only from objective inputs already attached to an action:
its ``domain`` (for enacted laws this is the Congress.gov ``policyArea`` controlled
vocabulary; for other action types it is set at ingestion), the metric ``domain``
that the objective was mapped to, and the action ``type``. No opinion, no ideology,
no value judgment enters the mapping. The mapping is a fixed lookup table so it is
fully auditable and reproducible. Anything that does not match a known domain falls
into "other" rather than being forced into a bucket.

This module is presentation only. It never feeds scoring or attribution.
"""

from __future__ import annotations

# Ordered category catalog: stable key, plain label, short neutral description.
CATEGORIES: list[tuple[str, str, str]] = [
    ("jobs_economy", "Jobs and economy",
     "Actions about employment, wages, growth, commerce, and public finance."),
    ("cost_spending", "Cost and spending",
     "Actions about taxes and how public money is budgeted and spent."),
    ("health", "Health",
     "Actions about public health and health outcomes."),
    ("public_safety", "Public safety",
     "Actions about crime, law enforcement, emergencies, defense, and the courts."),
    ("education", "Education",
     "Actions about schools and education."),
    ("energy_environment", "Energy and environment",
     "Actions about energy, emissions, land, water, and the environment."),
    ("poverty_income", "Poverty and income",
     "Actions about poverty, household income, and social welfare."),
    ("housing", "Housing",
     "Actions about housing and community development."),
    ("immigration", "Immigration",
     "Actions about immigration and citizenship."),
    ("civil_rights", "Civil rights",
     "Actions about civil rights, liberties, and minority issues."),
    ("transportation", "Transportation",
     "Actions about transportation and public works."),
    ("agriculture", "Agriculture and food",
     "Actions about farming, food, and rural issues."),
    ("foreign_affairs", "Foreign affairs",
     "Actions about international affairs, trade, and diplomacy."),
    ("government", "Government",
     "Actions about how government itself operates."),
    ("other", "Other",
     "Actions whose subject area does not map to one of the named categories."),
]

CATEGORY_KEYS: tuple[str, ...] = tuple(k for k, _, _ in CATEGORIES)
CATEGORY_LABEL: dict[str, str] = {k: label for k, label, _ in CATEGORIES}
CATEGORY_DESCRIPTION: dict[str, str] = {k: desc for k, _, desc in CATEGORIES}
_CATEGORY_ORDER: dict[str, int] = {k: i for i, (k, _, _) in enumerate(CATEGORIES)}

# Fixed lookup from a known ``domain`` string to a category key. Keys are matched
# case-insensitively. Domains absent here resolve to "other".
DOMAIN_TO_CATEGORY: dict[str, str] = {
    # Congress.gov policyArea controlled vocabulary + metric-catalog domains.
    "economics and public finance": "jobs_economy",
    "labor and employment": "jobs_economy",
    "commerce": "jobs_economy",
    "finance and financial sector": "jobs_economy",
    "taxation": "cost_spending",
    "appropriations": "cost_spending",
    "health": "health",
    "crime and law enforcement": "public_safety",
    "armed forces and national security": "public_safety",
    "emergency management": "public_safety",
    "law": "public_safety",
    "energy": "energy_environment",
    "energy and environment": "energy_environment",
    "environmental protection": "energy_environment",
    "public lands and natural resources": "energy_environment",
    "water resources development": "energy_environment",
    "income and poverty": "poverty_income",
    "social welfare": "poverty_income",
    "education": "education",
    "housing and community development": "housing",
    "immigration": "immigration",
    "civil rights and liberties, minority issues": "civil_rights",
    "transportation and public works": "transportation",
    "agriculture and food": "agriculture",
    "international affairs": "foreign_affairs",
    "foreign trade and international finance": "foreign_affairs",
    "government operations and politics": "government",
    "congress": "government",
}


def _lookup(domain: str | None) -> str | None:
    if not domain:
        return None
    return DOMAIN_TO_CATEGORY.get(domain.strip().lower())


# Keyword -> Congress.gov-style policyArea for executive orders, which carry no policy area
# of their own. Ordered most-specific first; the first list with a hit wins. Deterministic
# and auditable (a fixed table), exactly like DOMAIN_TO_CATEGORY. Presentation only.
_EO_KEYWORD_DOMAINS: list[tuple[str, tuple[str, ...]]] = [
    ("immigration", ("immigration", "immigrant", "border", "asylum", "alien", "visa",
                     "deportation", "naturalization", "refugee", "migrant")),
    ("crime and law enforcement", ("crime", "criminal", "law enforcement", "police", "cartel",
                                   "trafficking", "firearm", "gun ", "gang", "drug")),
    ("armed forces and national security", ("military", "defense", "armed forces",
                                            "national security", "veteran", "troop", "warfight",
                                            "missile", "nuclear weapon", "homeland")),
    ("immigration", ("citizenship",)),
    ("health", ("health", "medicare", "medicaid", "hospital", "disease", "pandemic", "vaccine",
                "opioid", "prescription drug", "mental health")),
    ("education", ("education", "school", "student", "university", "college", "teacher")),
    ("energy", ("energy", "oil", "natural gas", "pipeline", "drilling", "electric", "power grid",
                "petroleum", "coal", "lng")),
    ("environmental protection", ("environment", "climate", "emission", "pollution", "clean water",
                                  "clean air", "conservation", "wildlife", "endangered")),
    ("housing and community development", ("housing", "mortgage", "homeless", "rent ", "eviction")),
    ("transportation and public works", ("transportation", "highway", "aviation", "airport",
                                         "railroad", "infrastructure", "bridge", "transit")),
    ("agriculture and food", ("agriculture", "farm", "food ", "crop", "rural")),
    ("foreign trade and international finance", ("tariff", "import duty", "customs", "trade deficit")),
    ("international affairs", ("foreign", "sanction", "diplomacy", "treaty", "embassy", "ukraine",
                              "russia", "china", "israel", "gaza", "iran", "venezuela", "nato",
                              "alliance", "overseas")),
    ("taxation", ("tax ", "taxation", "taxes", "tariff")),
    ("civil rights and liberties, minority issues", ("civil right", "discrimination", "voting right",
                                                     "diversity", "equity", "gender", "disab")),
    ("labor and employment", ("labor", "worker", "wage", "employment", "workforce", "union",
                              "apprentice", "job ", "jobs")),
    ("government operations and politics", ("federal agency", "federal workforce", "civil service",
                                            "procurement", "regulation", "regulatory", "deregulat",
                                            "executive branch", "schedule f", "government efficiency",
                                            "accountability")),
    ("economics and public finance", ("econom", "inflation", "financial", "bank", "commerce",
                                      "business", "small business", "crypto", "digital asset")),
]


def classify_executive_domain(text: str | None) -> str | None:
    """Best-effort topic for an executive order from its title/abstract (it has no policy area).
    Returns a policyArea string understood by ``category_for``; None if nothing matches."""
    if not text:
        return None
    t = f" {text.lower()} "
    for domain, keywords in _EO_KEYWORD_DOMAINS:
        if any(kw in t for kw in keywords):
            return domain
    return None


def category_for(
    domain: str | None,
    action_type: str | None = None,
    metric_domain: str | None = None,
) -> str:
    """Return the category key for an action.

    Resolution order, most-specific objective signal first:
    1. Budget-execution actions are inherently about spending, regardless of the
       generic economic ``domain`` they carry, so ``type == "budget"`` -> cost_spending.
    2. The metric ``domain`` the objective was actually mapped to (e.g. a poverty or
       energy series) is the most specific evidence of what was measured.
    3. The action ``domain`` (the subject area of the action itself).
    4. Fallback "other".
    """
    if action_type == "budget":
        return "cost_spending"
    return _lookup(metric_domain) or _lookup(domain) or "other"


def category_label(key: str | None) -> str:
    return CATEGORY_LABEL.get(key or "other", "Other")


def category_sort_key(key: str | None) -> int:
    return _CATEGORY_ORDER.get(key or "other", len(CATEGORIES))


def category_catalog() -> list[dict[str, str]]:
    """Public catalog for the UI: ordered keys, labels, and descriptions."""
    return [{"key": k, "label": label, "description": desc} for k, label, desc in CATEGORIES]
