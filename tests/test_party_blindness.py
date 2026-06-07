"""Adversarial-neutrality guard: scoring code must be party-blind.

Statically asserts that no module under ``degreezeor/scoring`` references party at
all (no import of the Party model, no 'party' attribute/keyword access). The yardstick
is always the action's OWN stated objective — never who proposed it.
"""

from __future__ import annotations

import pathlib
import re

SCORING_DIR = pathlib.Path(__file__).resolve().parents[1] / "src" / "degreezeor" / "scoring"

# Allow the *word* inside comments that explain blindness, but forbid real usage:
#   - importing Party
#   - attribute access like `.party`
#   - dict/keyword access like ["party"] or party=
_FORBIDDEN = [
    re.compile(r"\bimport\b.*\bParty\b"),
    re.compile(r"\bParty\b\s*\("),
    re.compile(r"\.party\b"),
    re.compile(r"""\[['"]party['"]\]"""),
    re.compile(r"\bparty\s*="),
]


def test_scoring_modules_never_touch_party() -> None:
    offenders: list[str] = []
    for path in SCORING_DIR.rglob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            code = line.split("#", 1)[0]  # ignore comments
            for pat in _FORBIDDEN:
                if pat.search(code):
                    offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, "scoring code must be party-blind, found:\n" + "\n".join(offenders)
