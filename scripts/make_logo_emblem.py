"""Makes two files from the full Analyst Consortium logo: the app's emblem, and a copy on a white card for the README.

The emblem is the symbol without its lettering. The page and the reports put it to the left of the typed name "Analyst
Consortium", so the lettering inside the full logo would only repeat it. This script keeps everything else exactly as drawn: it
removes the brand-typography group and crops the view box to the symbol (with room for its glows and shadows).

The logo has no background and its lines are dark navy, so on a dark page (GitHub's dark theme) it would nearly vanish. The
README therefore shows a copy with a white rounded card drawn behind it, and nothing else changed. Run this after changing the
full logo:

    python scripts/make_logo_emblem.py
"""

import re
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
ASSETS = PROJECT / "tradingagents" / "assets"
SOURCE, TARGET = ASSETS / "analyst-consortium-logo.svg", ASSETS / "analyst-consortium-emblem.svg"
README_COPY = PROJECT / "assets" / "analyst-consortium-logo-on-white.svg"
CARD_NOTE = "<!-- analyst-consortium-logo.svg with a white card behind it, so it reads on a dark page. Made by scripts/make_logo_emblem.py. -->"
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


def on_white(logo: str) -> str:
    """The full logo with a white rounded card drawn first, covering its whole view box (ValueError without a root tag or view box)."""
    root = ROOT.search(logo)
    box = re.search(r'viewBox="([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+)"', root.group(0)) if root else None
    if not box:
        raise ValueError("the logo has no <svg> root tag with a viewBox")
    x, y, w, h = box.groups()
    card = f'<rect id="white-card" x="{x}" y="{y}" width="{w}" height="{h}" rx="28" fill="#ffffff" />'
    return logo[:root.end()] + "\n  " + CARD_NOTE + "\n  " + card + logo[root.end():]


if __name__ == "__main__":
    logo = SOURCE.read_text(encoding="utf-8")
    TARGET.write_text(derive(logo), encoding="utf-8")
    print("wrote", TARGET)
    README_COPY.parent.mkdir(exist_ok=True)
    README_COPY.write_text(on_white(logo), encoding="utf-8")
    print("wrote", README_COPY)
