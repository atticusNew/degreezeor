#!/usr/bin/env python3
"""Render the white paper + methodology Markdown into clean, branded, letterhead-style PDFs.

Reproducible: `python scripts/build_docs_pdf.py` regenerates
`web/DegreeZero-Whitepaper.pdf` and `web/DegreeZero-Methodology.pdf`. Uses fpdf2 + the
bundled DejaVu fonts (full Unicode) and the project logo for a stationery look.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import XPos, YPos

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
FONTS = ROOT / "src" / "degreezeor" / "api" / "assets"
LOGO = WEB / "logo.png"

LEFT, RIGHT = 18, 192  # page margins (A4 width 210)
INK, MUTED, ACCENT, LINE = (32, 28, 38), (110, 116, 130), (122, 96, 160), (210, 205, 215)


def _break_long(token: str, n: int = 46) -> str:
    return token if (len(token) <= n or " " in token) else \
        " ".join(token[i:i + n] for i in range(0, len(token), n))


def _clean(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", text)  # [label](url) -> label
    text = text.replace("`", "")
    return " ".join(_break_long(t) for t in text.split(" "))


class Doc(FPDF):
    title = ""
    subtitle = ""

    def header(self) -> None:
        if self.page_no() == 1:
            if LOGO.exists():
                self.image(str(LOGO), x=LEFT, y=14, h=11)
            self.set_xy(LEFT, 34)
            self.set_text_color(*INK)
            self.set_font("DejaVu", "B", 21)
            self.multi_cell(RIGHT - LEFT, 9, self.title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            if self.subtitle:
                self.set_x(LEFT)
                self.set_font("DejaVu", "", 10.5)
                self.set_text_color(*MUTED)
                self.multi_cell(RIGHT - LEFT, 6, self.subtitle, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.set_draw_color(*LINE)
            self.set_line_width(0.4)
            self.line(LEFT, self.get_y() + 2, RIGHT, self.get_y() + 2)
            self.ln(7)
        else:
            self.set_xy(LEFT, 12)
            self.set_font("DejaVu", "", 8)
            self.set_text_color(*MUTED)
            self.cell(0, 5, "DegreeZero — " + self.title, align="L")
            self.ln(8)

    def footer(self) -> None:
        self.set_y(-14)
        self.set_font("DejaVu", "", 8)
        self.set_text_color(*MUTED)
        self.cell((RIGHT - LEFT) / 2, 5,
                  "DegreeZero · degree0.org · nonpartisan · open · reproducible", align="L")
        self.cell((RIGHT - LEFT) / 2, 5, str(self.page_no()), align="R")

    def block(self, text: str, *, size: float, x: float = LEFT, bold: bool = False,
              color=INK, lh: float = 5.8, md: bool = False) -> None:
        self.set_x(x)
        self.set_font("DejaVu", "B" if bold else "", size)
        self.set_text_color(*color)
        self.multi_cell(RIGHT - x, lh, text, markdown=md, new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def render(md_path: Path, out_path: Path, title: str, subtitle: str) -> None:
    pdf = Doc()
    pdf.title, pdf.subtitle = title, subtitle
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(LEFT, 16, 210 - RIGHT)
    pdf.add_font("DejaVu", "", str(FONTS / "DejaVuSans.ttf"))
    pdf.add_font("DejaVu", "B", str(FONTS / "DejaVuSans-Bold.ttf"))
    pdf.add_page()

    for raw in md_path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line.strip():
            pdf.ln(2.4)
        elif line.startswith("# "):
            continue  # title is in the letterhead
        elif line.startswith("## "):
            pdf.ln(2)
            pdf.block(_clean(line[3:]), size=15, bold=True, color=ACCENT, lh=7.5)
            pdf.ln(1)
        elif line.startswith("### "):
            pdf.block(_clean(line[4:]), size=12, bold=True, lh=6.5)
        elif line.strip() in ("---", "***", "___"):
            pdf.ln(1)
            pdf.set_draw_color(*LINE)
            pdf.line(LEFT, pdf.get_y(), RIGHT, pdf.get_y())
            pdf.ln(3)
        elif line.lstrip().startswith(("- ", "* ")):
            indent = (len(line) - len(line.lstrip())) // 2
            pdf.block("•  " + _clean(line.lstrip()[2:]), size=10.5, x=LEFT + 5 + indent * 5,
                      lh=5.6, md=True)
        elif re.match(r"^\d+\.\s+", line.strip()):
            m = re.match(r"^(\d+)\.\s+(.*)", line.strip())
            pdf.block(f"{m.group(1)}.  " + _clean(m.group(2)), size=10.5, x=LEFT + 4, lh=5.6, md=True)
        elif line.startswith("> "):
            pdf.block(_clean(line[2:]), size=10, x=LEFT + 4, color=MUTED, lh=5.6, md=True)
        else:
            pdf.block(_clean(line), size=10.5, lh=5.8, md=True)

    pdf.output(str(out_path))
    print("wrote", out_path.relative_to(ROOT))


def main() -> None:
    today = date.today().isoformat()
    render(ROOT / "docs" / "WHITEPAPER.md", WEB / "DegreeZero-Whitepaper.pdf",
           "DegreeZero — White Paper",
           f"Empirical, source-anchored scoring of public actions · v1.0 · {today}")
    render(ROOT / "docs" / "METHODOLOGY.md", WEB / "DegreeZero-Methodology.pdf",
           "DegreeZero — Methodology",
           f"Scoring philosophy, formulas, and bias controls · {today}")


if __name__ == "__main__":
    main()
