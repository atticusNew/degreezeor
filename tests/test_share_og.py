"""Per-page social previews: the dependency-free OG image + crawler-readable share HTML."""

from __future__ import annotations

from degreezeor.api.app import _PIL_OK, _og_png, _og_svg, _share_html


def test_og_png_renders_valid_image() -> None:
    if not _PIL_OK:  # Pillow optional; SVG fallback covers this case
        return
    png = _og_png("Jane Doe", "Senator")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic
    assert len(png) > 1000


def test_og_svg_is_well_formed_and_escaped() -> None:
    svg = _og_svg("Nancy Pelosi & Co <test>", "Representative").decode("utf-8")
    assert svg.startswith("<svg") and 'width="1200"' in svg and 'height="630"' in svg
    assert "DegreeZero" in svg and "Representative" in svg
    # XML special characters must be escaped, never injected raw.
    assert "&amp;" in svg and "&lt;test&gt;" in svg
    assert "& Co <test>" not in svg


def test_share_html_has_per_page_meta_and_redirect() -> None:
    resp = _share_html(
        title="Jane Doe", description="Senator. 12 recorded votes.",
        hash_path="/official/42", image="/og.svg?title=Jane%20Doe",
    )
    body = resp.body.decode("utf-8")
    assert '<meta property="og:title" content="Jane Doe"' in body
    assert 'og:description" content="Senator. 12 recorded votes."' in body
    assert 'twitter:card" content="summary_large_image"' in body
    assert 'url=/#/official/42' in body  # humans bounce into the SPA
    assert "og:image" in body
