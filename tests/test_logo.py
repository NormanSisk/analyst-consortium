"""The Analyst Consortium logo: the author's file, the symbol made from it, and where the symbol appears beside the name."""

import importlib.util
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "tradingagents" / "assets"
LOGO, EMBLEM = ASSETS / "analyst-consortium-logo.svg", ASSETS / "analyst-consortium-emblem.svg"
SVG = "{http://www.w3.org/2000/svg}"


def load_script():
    spec = importlib.util.spec_from_file_location("make_logo_emblem", ROOT / "scripts" / "make_logo_emblem.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
class TestTheFiles:
    def test_the_full_logo_is_kept_as_the_author_drew_it_with_its_lettering(self):
        root = ET.parse(LOGO).getroot()
        assert root.tag == SVG + "svg" and root.get("viewBox") == "170 135 490 625"
        words = [t.text for t in root.iter(SVG + "text")]
        assert words == ["ANALYST", "CONSORTIUM", "AI STOCK INTELLIGENCE"]

    def test_the_symbol_is_the_logo_without_its_lettering_and_nothing_else_changed(self):
        script = load_script()
        emblem_text = EMBLEM.read_text(encoding="utf-8")
        assert script.derive(LOGO.read_text(encoding="utf-8")) == emblem_text            # the file on disk is exactly what the script makes
        emblem, logo = ET.fromstring(emblem_text), ET.parse(LOGO).getroot()
        assert not list(emblem.iter(SVG + "text")) and emblem.get("viewBox") == "165 122 500 460"
        def drawn(root, name):
            element = root.find(f".//{SVG}g[@id='{name}']")
            element.tail = None                                                          # the spaces after an element are not part of the drawing
            return ET.tostring(element)

        for name in ("logo-emblem", "circuit-traces", "candlesticks", "trendlines"):
            assert drawn(emblem, name) == drawn(logo, name), name                        # the drawing itself is untouched
        assert [d.get("id") for d in emblem.iter(SVG + "linearGradient")] == [d.get("id") for d in logo.iter(SVG + "linearGradient")]

    def test_the_script_refuses_a_logo_it_does_not_understand(self):
        script = load_script()
        with pytest.raises(ValueError, match="brand-typography"):
            script.derive('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><g id="logo-emblem"/></svg>')

    @pytest.mark.parametrize("path", [LOGO, EMBLEM])
    def test_both_files_hold_only_inert_drawing_shapes(self, path):
        text = path.read_text(encoding="utf-8")
        tags = set(re.findall(r"<\s*([a-zA-Z][\w:-]*)", text))
        assert tags <= {"svg", "defs", "linearGradient", "stop", "filter", "feDropShadow", "g", "path", "line", "rect", "circle", "polygon", "text"}, tags
        assert not re.search(r"\son\w+\s*=|href|<script|<!entity|<!doctype|@import|javascript:|<image|<use", text, re.IGNORECASE)
        assert all(ref.startswith("#") for ref in re.findall(r"url\(([^)]*)\)", text))

    def test_the_files_ship_with_the_package(self):
        assert "assets/*.svg" in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


@pytest.mark.unit
class TestOnThePage:
    def test_the_symbol_sits_to_the_left_of_the_name_in_the_heading_and_is_not_read_aloud(self):
        html = (ROOT / "tradingagents" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
        heading = re.search(r'<h1 class="brand">(.*?)</h1>', html, re.DOTALL).group(1)
        assert re.fullmatch(r'<img class="brand-logo" src="/logo-emblem.svg" alt="" width="\d+" height="\d+"><span>Analyst Consortium</span>', heading)
        css = (ROOT / "tradingagents" / "ui" / "static" / "app.css").read_text(encoding="utf-8")
        assert "h1.brand { display:flex; align-items:center;" in css
        assert 'html[data-theme="dark"] .brand-logo, html[data-theme="contrast"] .brand-logo { background:#ffffff;' in css     # white chip on dark themes

    def test_the_only_brand_headings_are_the_page_and_the_report(self):
        # the name is shown with its symbol where it heads a page; text that merely mentions it is left as text
        html = (ROOT / "tradingagents" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
        assert html.count('class="brand-logo"') == 1 and html.count("<h1") == 1
