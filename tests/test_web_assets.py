"""Static guard: the front-end bundle must parse.

The explainability UI is plain (zero-build) JS, so a stray syntax error silently
breaks the whole SPA in the browser without failing any Python test. This runs a
real JS parser (`node --check`) over app.js when Node is available, catching the
class of bug (e.g. an unbalanced paren) that screenshot review used to catch.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "web" / "app.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_app_js_parses() -> None:
    result = subprocess.run(
        ["node", "--check", str(APP_JS)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, f"app.js syntax error:\n{result.stderr}"


def test_app_js_parens_balanced() -> None:
    """Pure-python fallback (always runs): parens/brackets/braces are balanced
    outside of strings/comments/template literals."""
    src = APP_JS.read_text()
    pairs = {")": "(", "]": "[", "}": "{"}
    opens = set("([{")
    stack: list[str] = []
    i, n = 0, len(src)
    mode = None  # None | "'" | '"' | '`' | "//" | "/*"
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if mode in ("'", '"', "`"):
            if c == "\\":
                i += 2
                continue
            if c == mode:
                mode = None
            i += 1
            continue
        if mode == "//":
            if c == "\n":
                mode = None
            i += 1
            continue
        if mode == "/*":
            if c == "*" and nxt == "/":
                mode = None
                i += 2
                continue
            i += 1
            continue
        # not in a string/comment
        if c == "/" and nxt == "/":
            mode = "//"
            i += 2
            continue
        if c == "/" and nxt == "*":
            mode = "/*"
            i += 2
            continue
        if c in ("'", '"', "`"):
            mode = c
            i += 1
            continue
        if c in opens:
            stack.append(c)
        elif c in pairs:
            assert stack and stack[-1] == pairs[c], f"unbalanced {c!r} at offset {i}"
            stack.pop()
        i += 1
    assert not stack, f"unclosed {stack!r}"
