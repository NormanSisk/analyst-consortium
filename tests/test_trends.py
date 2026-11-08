"""Tests for the trend-facts vendor (a local addition): each trend both ways, plus the rule-based flags."""

import pandas as pd
import pytest

from tradingagents.dataflows.vendors.yahoo import trends

M = 1_000_000
QUARTERS = pd.to_datetime(["2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"])   # newest first


def _frame(rows: dict[str, list]) -> pd.DataFrame:
    return pd.DataFrame({label: [None if v is None else v * M for v in values] for label, values in rows.items()},
                        index=QUARTERS[:len(next(iter(rows.values())))]).T


def _alphabet_like(**overrides):
    """Margin 30.5, 31.6, 36.1, 34.0, 31.8 (oldest first): up on the year, down on the quarter."""
    income = _frame({
        "Total Revenue": [127577, 119796, 109896, 113829, 102346],
        "Operating Income": [40576, 40770, 39696, 35934, 31228],
        "Net Income": [26468, 112193, 62578, 34455, 34979],
        "Diluted Average Shares": [12368, 12309, 12238, 12228, 12203],
    })
    cash = _frame({
        "Operating Cash Flow": [42290, 39069, 45790, 52402, 48414],
        "Capital Expenditure": [-51355, -44924, -35674, -27851, -23953],
        "Free Cash Flow": [-9065, -5855, 10116, 24551, 24461],
        "Repurchase Of Capital Stock": [0, 0, 0, -5499, -11504],
    })
    balance = _frame({
        "Long Term Debt": [125102, 98165, 77501, 46547, 21607],
        "Cash Cash Equivalents And Short Term Investments": [265098, 242474, 126840, 126843, 98496],
    })
    frames = {"income": income, "cash": cash, "balance": balance, **overrides}
    return frames["income"], frames["cash"], frames["balance"]


@pytest.mark.unit
class TestTrendFacts:
    def _text(self, **overrides):
        income, cash, balance = _alphabet_like(**overrides)
        return trends.build_trend_facts("TGT", income, cash, balance)

    def test_the_table_runs_oldest_to_newest_with_the_margin_worked_out(self):
        text = self._text()
        rows = [line for line in text.splitlines() if line.startswith("| 20")]
        assert [r.split("|")[1].strip() for r in rows] == ["2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30"]
        assert "| 30.5% |" in rows[0] and "| 36.1% |" in rows[2] and "| 31.8% |" in rows[4]

    def test_each_trend_is_given_against_the_year_ago_quarter_and_the_prior_quarter(self):
        text = self._text()
        assert "Revenue: +24.7% versus the same quarter a year earlier; +6.5% versus the prior quarter." in text
        assert "Operating margin is 31.8%: +1.3 pts versus a year earlier (year-over-year view); " \
               "-2.2 pts versus the prior quarter and -4.3 pts versus two quarters earlier (sequential view)." in text
        assert "ranged from 30.5% (2025-09-30) to 36.1% (2026-03-31)" in text
        assert "4.3 points below the 5-quarter peak and 1.3 points above its low" in text

    def test_opposite_margin_views_and_a_falling_streak_are_flagged(self):
        flags = self._text().split("## Flags to state in the report")[1]
        assert "Operating margin has fallen 2 quarters in a row, by 4.3 points in total." in flags
        assert "The two views of operating margin disagree: +1.3 pts year over year but -2.2 pts from the prior quarter." in flags

    def test_cash_flow_dilution_debt_and_buyback_flags(self):
        flags = self._text().split("## Flags to state in the report")[1]
        assert "Free cash flow was negative in 2 of the last 5 quarters." in flags
        assert "Capital spending exceeded operating cash flow in the latest quarter (121%)." in flags
        assert "The diluted share count rose 1.4% in a year (dilution)." in flags
        assert "Long-term debt is 5.8 times its level a year earlier." in flags
        assert "Share repurchases stopped: none in the latest quarter against 11,504 a year earlier." in flags
        assert "Net income differed from operating income by more than 40% in 2 of the last 5 quarters" in flags

    def test_free_cash_flow_is_computed_when_the_statement_lacks_the_line(self):
        income, cash, balance = _alphabet_like()
        cash = cash.drop(index="Free Cash Flow")
        text = trends.build_trend_facts("TGT", income, cash, balance)
        assert "24,461, 24,551, 10,116, -5,855, -9,065" in text

    def test_a_steady_company_gets_no_flags(self):
        steady = _frame({
            "Total Revenue": [1050, 1040, 1030, 1020, 1000], "Operating Income": [210, 208, 206, 204, 200],
            "Net Income": [160, 158, 156, 154, 150], "Diluted Average Shares": [100, 100, 100, 100, 100]})
        cash = _frame({"Operating Cash Flow": [300, 290, 280, 270, 260], "Capital Expenditure": [-50, -50, -50, -50, -50],
                       "Repurchase Of Capital Stock": [-20, -20, -20, -20, -20]})
        text = trends.build_trend_facts("STD", steady, cash, None)
        assert "No rule-based flags in the latest quarter." in text

    def test_four_quarters_cannot_give_a_year_over_year_view_and_say_so(self):
        income = _frame({"Total Revenue": [120, 115, 110, 105], "Operating Income": [24, 22, 20, 18]})
        text = trends.build_trend_facts("NEW", income, None, None)
        assert "year-ago quarter not available" in text
        assert "versus a year earlier (year-over-year view)" in text and "n/a versus a year earlier" in text

    def test_a_percent_change_from_almost_nothing_says_so(self):
        income = _frame({"Total Revenue": [190, 185, 185, 184, 181], "Operating Income": [4.4, 3.3, 9.7, 5.3, 0.6]})
        text = trends.build_trend_facts("SML", income, None, None)
        assert "(from a very small base)" in text

    def test_missing_cash_flow_and_balance_sheet_skip_those_lines_only(self):
        income, _, _ = _alphabet_like()
        text = trends.build_trend_facts("TGT", income, None, None)
        assert "Operating margin is 31.8%" in text and "Free cash flow" not in text.split("## The latest quarter")[1]
        assert "Long-term debt" not in text

    def test_fewer_than_two_quarters_say_no_trend_can_be_worked_out(self):
        income = _frame({"Total Revenue": [100], "Operating Income": [10]})
        assert "no trend can be worked out" in trends.build_trend_facts("ONE", income, None, None)


@pytest.mark.unit
class TestEntryPoint:
    def test_a_run_dated_in_the_past_is_withheld(self):
        out = trends.get_trend_facts("GOOG", "2020-01-02")
        assert "withheld for this date" in out

    def test_the_vendor_reads_the_three_quarterly_statements(self, monkeypatch):
        income, cash, balance = _alphabet_like()

        class FakeTicker:
            def __init__(self, symbol):
                self.quarterly_income_stmt, self.quarterly_cashflow, self.quarterly_balance_sheet = income, cash, balance

        monkeypatch.setattr(trends, "yf", type("FakeYF", (), {"Ticker": FakeTicker}))
        assert "Operating margin is 31.8%" in trends.get_trend_facts("TGT", None)

    def test_the_fundamentals_analyst_is_offered_the_tool_and_the_router_knows_it(self):
        from tradingagents.agents.analysts import fundamentals_analyst
        from tradingagents.dataflows import router

        assert "get_trend_facts" in [t.name for t in fundamentals_analyst.TOOLS]
        assert router.get_category_for_method("get_trend_facts") == "fundamental_data"
        assert "yfinance" in router.VENDOR_METHODS["get_trend_facts"]
