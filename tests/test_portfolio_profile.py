"""The investor profile carried in the portfolio file (a local addition)."""

import json

import pytest

from tradingagents.portfolio import PortfolioContext, load_portfolio

BOOK = {
    "cash": 7499.0,
    "currency": "USD",
    "positions": [{"ticker": "WQTM", "quantity": 17.7493, "average_price": 28.226}],
    "profile": "Beginner with a small real-money account. Does not want to sell on normal volatility.",
}


@pytest.mark.unit
class TestProfile:
    def test_the_profile_is_rendered_after_the_holdings_with_a_framing_line(self):
        text = PortfolioContext.model_validate(BOOK).render("GOOG")
        assert "No current position in GOOG" in text
        assert "Cash available: 7,499.00 USD" in text
        assert "Investor profile, in the caller's own words" in text
        assert "must not change what the data shows" in text
        assert text.index("Cash available") < text.index("Beginner with a small real-money account")

    def test_without_a_profile_the_block_is_unchanged(self):
        without = {k: v for k, v in BOOK.items() if k != "profile"}
        assert "Investor profile" not in PortfolioContext.model_validate(without).render("GOOG")
        assert "Investor profile" not in PortfolioContext.model_validate({**BOOK, "profile": "   "}).render("GOOG")

    def test_a_held_instrument_and_the_profile_both_appear(self):
        text = PortfolioContext.model_validate(BOOK).render("WQTM")
        assert "Current position in WQTM: 17.75 units, average price 28.23" in text
        assert "Does not want to sell on normal volatility" in text

    def test_no_position_adds_the_entry_reading_of_the_five_ratings(self):
        text = PortfolioContext.model_validate(BOOK).render("GOOG")
        assert "Reading the five ratings with no position in GOOG" in text
        for rating in ("Buy:", "Overweight:", "Hold:", "Underweight:", "Sell:"):
            assert rating in text
        assert "Underweight as a Sell" in text            # the three-way shorthand is named
        # It sits with the holdings, before the investor's own words.
        assert text.index("Cash available") < text.index("Reading the five ratings") < text.index("Investor profile")

    def test_a_held_instrument_gets_no_such_reading(self):
        text = PortfolioContext.model_validate(BOOK).render("WQTM")
        assert "Reading the five ratings" not in text

    def test_the_reading_does_not_lean_toward_an_outcome(self):
        # It defines the steps by strength of evidence; it must not tell the agents to prefer one.
        from tradingagents.portfolio import NO_POSITION_READING
        low = NO_POSITION_READING.lower()
        assert "should buy" not in low and "prefer" not in low and "default to" not in low
        assert "how strong the evidence is" in low

    def test_the_file_loader_reads_the_profile_and_a_change_alters_the_fingerprint(self, tmp_path):
        path = tmp_path / "book.json"
        path.write_text(json.dumps(BOOK), encoding="utf-8")
        loaded = load_portfolio(path)
        assert loaded.profile == BOOK["profile"]
        changed = PortfolioContext.model_validate({**BOOK, "profile": "Something else."})
        assert loaded.fingerprint() != changed.fingerprint()
