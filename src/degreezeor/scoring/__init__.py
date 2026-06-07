"""Scoring engine: catalog, objective mapping, pre-registration, baseline,
outcome, attribution, confidence, and the final pinned score run.

DESIGN INVARIANT (enforced by tests): no module in this package may read an
official's party. The yardstick is always the action's OWN stated objective.
"""
