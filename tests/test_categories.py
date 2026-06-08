"""Tests for the objective domain -> category taxonomy (presentation only)."""

from __future__ import annotations

from degreezeor.categories import (
    CATEGORY_KEYS,
    category_catalog,
    category_for,
    category_label,
    category_sort_key,
)


def test_known_domains_map_to_expected_categories() -> None:
    assert category_for("Economics and Public Finance") == "jobs_economy"
    assert category_for("Labor and Employment") == "jobs_economy"
    assert category_for("Taxation") == "cost_spending"
    assert category_for("Health") == "health"
    assert category_for("Crime and Law Enforcement") == "public_safety"
    assert category_for("Armed Forces and National Security") == "public_safety"
    assert category_for("Energy") == "energy_environment"
    assert category_for("Environmental Protection") == "energy_environment"
    assert category_for("Income and Poverty") == "poverty_income"
    assert category_for("Social Welfare") == "poverty_income"
    assert category_for("Education") == "education"


def test_case_insensitive_and_unknown_falls_back_to_other() -> None:
    assert category_for("economics AND public finance") == "jobs_economy"
    assert category_for("Some Unknown Area") == "other"
    assert category_for(None) == "other"


def test_budget_action_type_overrides_to_cost_spending() -> None:
    # Budget execution carries the generic economic domain but is objectively spending.
    assert category_for("Economics and Public Finance", action_type="budget") == "cost_spending"


def test_metric_domain_is_most_specific_signal() -> None:
    # A law with the generic economic subject domain, scored on a poverty metric, is poverty.
    assert category_for("Economics and Public Finance", action_type="law",
                        metric_domain="Income and Poverty") == "poverty_income"
    # Court survival reuses an EO action; its legal metric domain reads as public safety.
    assert category_for("Economics and Public Finance", action_type="eo",
                        metric_domain="Law") == "public_safety"


def test_catalog_is_consistent_and_ordered() -> None:
    cat = category_catalog()
    keys = [c["key"] for c in cat]
    assert keys == list(CATEGORY_KEYS)
    assert keys[-1] == "other"
    for c in cat:
        assert category_label(c["key"]) == c["label"]
        assert c["description"]
    # Sort key is monotonic with catalog order; unknown sorts last.
    assert category_sort_key("jobs_economy") < category_sort_key("other")
    assert category_sort_key("nonexistent") >= category_sort_key("other")
