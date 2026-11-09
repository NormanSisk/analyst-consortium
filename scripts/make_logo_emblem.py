"""Makes the app's emblem from the full Analyst Consortium logo: the symbol without its lettering.

The page and the reports put the emblem to the left of the typed name "Analyst Consortium", so the lettering inside the full logo
would only repeat it. This script keeps everything else exactly as drawn: it removes the brand-typography group and crops the view
box to the symbol (with room for its glows and shadows). Run it after changing the full logo:

    python scripts/make_logo_emblem.py
"""

import re
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "tradingagents" / "assets"
SOURCE, TARGET = ASSETS / "analyst-consortium-logo.svg", ASSETS / "analyst-consortium-emblem.svg"
VIEW_BOX = (165, 122, 500, 460)                    # x, y, width, height: the symbol plus its shadows, measured from the full logo
TYPOGRAPHY = re.compile(r"\s*<!--[^>]*BRAND TYPOGRAPHY[^>]*-->\s*<g id=\"brand-typography\".*?</g>(?=\s*</svg>)", re.DOTALL)
ROOT = re.compile(r"<svg\b[^>]*>")
NOTE = "<!-- The symbol of analyst-consortium-logo.svg without its lettering, made by scripts/make_logo_emblem.py. -->"


def derive(logo: str) -> str:
    """The emblem SVG text from the full logo text (ValueError when the lettering or the root tag cannot be found)."""
    without, count = TYPOGRAPHY.subn("", logo)
    if count != 1:
        raise ValueError("expected exactly one brand-typography group in the logo")
    x, y, w, h = VIEW_BOX
    root = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x} {y} {w} {h}" width="{w}" height="{h}">'
    if not ROOT.search(without):
        raise ValueError("the logo has no <svg> root tag")
    return ROOT.sub(root + "\n  " + NOTE, without, count=1)


if __name__ == "__main__":
    TARGET.write_text(derive(SOURCE.read_text(encoding="utf-8")), encoding="utf-8")
    print("wrote", TARGET)
