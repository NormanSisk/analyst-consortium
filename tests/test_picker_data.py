"""The picker's Yahoo downloads, with stand-ins for yfinance: paging, filtering, the shapes Yahoo can return, and a missing
piece of data. No network."""

import numpy as np
import pandas as pd
import pytest

from tradingagents.dataflows.vendors.yahoo import picker_data as pdata


def quote(symbol, cap=5e9, quote_type="EQUITY", forward_pe=15.0):
    return {"symbol": symbol, "shortName": f"{symbol} Inc", "marketCap": cap, "regularMarketPrice": 10.0, "quoteType": quote_type,
            "averageDailyVolume3Month": 1e6, "forwardPE": forward_pe, "trailingPE": 20.0}


@pytest.mark.unit
class TestScreen:
    def test_pages_are_followed_and_odd_symbols_and_funds_are_left_out(self, monkeypatch):
        pages = {0: {"quotes": [quote("AAA"), quote("BBB", quote_type="ETF"), quote("BRK-B"), quote("AB.C"), quote("TOOLONGSYM")], "total": 7},
                 5: {"quotes": [quote("CCC"), quote("DDD")], "total": 7}}
        calls = []

        def fake_screen(query, sortField, sortAsc, size, offset):
            calls.append(offset)
            return pages.get(offset, {"quotes": [], "total": 7})

        monkeypatch.setattr(pdata.yf, "screen", fake_screen)
        out = pdata.screen_universe(2e9, 5e5, 100)
        assert [r["symbol"] for r in out] == ["AAA", "BRK-B", "CCC", "DDD"] and calls == [0, 5]
        assert out[0]["name"] == "AAA Inc" and out[0]["forward_pe"] == 15.0 and out[0]["sector"] == ""

    def test_the_limit_caps_the_list(self, monkeypatch):
        monkeypatch.setattr(pdata.yf, "screen", lambda *a, **k: {"quotes": [quote(f"S{chr(65 + i)}") for i in range(10)], "total": 10})
        assert len(pdata.screen_universe(2e9, 5e5, 4)) == 4

    def test_an_empty_answer_ends_the_loop(self, monkeypatch):
        monkeypatch.setattr(pdata.yf, "screen", lambda *a, **k: {"quotes": [], "total": 0})
        assert pdata.screen_universe(2e9, 5e5, 50) == []
        monkeypatch.setattr(pdata.yf, "screen", lambda *a, **k: None)
        assert pdata.screen_universe(2e9, 5e5, 50) == []

    def test_each_sector_is_asked_for_and_the_result_is_merged_largest_first(self, monkeypatch):
        asked = []

        def fake(min_cap, min_volume, limit, region="us", sector=None):
            asked.append(sector)
            return [{"symbol": "TECH" if sector == "Technology" else "NRG" if sector == "Energy" else f"X{len(asked)}", "market_cap": 9e9 if sector == "Energy" else 3e9,
                     "sector": sector}] + ([{"symbol": "DUP", "market_cap": 1e9, "sector": sector}] if sector in ("Technology", "Energy") else [])

        monkeypatch.setattr(pdata, "screen_universe", fake)
        out = pdata.screen_universe_with_sectors(2e9, 5e5, 3)
        assert asked == list(pdata.SECTORS) and len(out) == 3 and out[0]["symbol"] == "NRG"
        assert [r["symbol"] for r in out].count("DUP") <= 1 and [r["market_cap"] for r in out] == sorted((r["market_cap"] for r in out), reverse=True)

    def test_a_sector_is_part_of_the_query(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(pdata.yf, "screen", lambda query, **k: seen.update(q=query.to_dict()) or {"quotes": [], "total": 0})
        pdata.screen_universe(2e9, 5e5, 10, sector="Technology")
        assert "Technology" in str(seen["q"]) and "region" in str(seen["q"])


@pytest.mark.unit
class TestPrices:
    def idx(self, n=5):
        return pd.bdate_range("2026-10-26", periods=n)

    def test_a_download_of_several_symbols(self, monkeypatch):
        idx = self.idx()
        cols = pd.MultiIndex.from_product([["AAA", "BBB"], ["Close", "Open"]])
        frame = pd.DataFrame(np.arange(20, dtype=float).reshape(5, 4) + 1, index=idx, columns=cols)
        monkeypatch.setattr(pdata.yf, "download", lambda *a, **k: frame)
        out = pdata.fetch_closes(["AAA", "BBB", "ZZZ"])
        assert sorted(out) == ["AAA", "BBB"] and len(out["AAA"]) == 5 and out["AAA"].index.tz is None

    @pytest.mark.parametrize("layout", ["ticker-level", "field-then-ticker", "flat"])
    def test_a_single_symbol_comes_back_in_any_of_yahoos_layouts(self, monkeypatch, layout):
        idx = self.idx()
        values = np.linspace(10, 14, 5)
        if layout == "ticker-level":
            frame = pd.DataFrame({("AAA", "Close"): values, ("AAA", "Open"): values}, index=idx)
        elif layout == "field-then-ticker":
            frame = pd.DataFrame({("Close", "AAA"): values, ("Open", "AAA"): values}, index=idx)
        else:
            frame = pd.DataFrame({"Close": values, "Open": values}, index=idx)
        monkeypatch.setattr(pdata.yf, "download", lambda *a, **k: frame)
        out = pdata.fetch_closes(["AAA"])
        assert list(out) == ["AAA"] and out["AAA"].iloc[-1] == 14.0

    def test_empty_downloads_and_all_nan_columns_are_skipped(self, monkeypatch):
        monkeypatch.setattr(pdata.yf, "download", lambda *a, **k: pd.DataFrame())
        assert pdata.fetch_closes(["AAA"]) == {}
        idx = self.idx()
        frame = pd.DataFrame({("AAA", "Close"): [np.nan] * 5, ("BBB", "Close"): np.arange(5.0) + 1}, index=idx)
        monkeypatch.setattr(pdata.yf, "download", lambda *a, **k: frame)
        assert list(pdata.fetch_closes(["AAA", "BBB"])) == ["BBB"]

    def test_symbols_are_downloaded_in_batches(self, monkeypatch):
        batches = []
        monkeypatch.setattr(pdata.yf, "download", lambda chunk, **k: batches.append(len(chunk)) or pd.DataFrame())
        pdata.fetch_closes([f"S{i}" for i in range(250)])
        assert batches == [100, 100, 50]


class FakeTicker:
    def __init__(self, symbol, broken=()):
        self.symbol, self._broken = symbol, set(broken)
        quarters = pd.to_datetime(["2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"])
        self.quarterly_cashflow = pd.DataFrame([[30.0, 29, 28, 27, 26], [20.0, 18, 17, 16, np.nan]], index=["Operating Cash Flow", "Free Cash Flow"], columns=quarters)
        self.quarterly_income_stmt = pd.DataFrame([[100.0, 95, 90, 88, 80]], index=["Total Revenue"], columns=quarters)
        self.quarterly_balance_sheet = pd.DataFrame([[500.0, 480, 460, 450, 440]], index=["Cash Cash Equivalents And Short Term Investments"], columns=quarters)
        self.eps_trend = pd.DataFrame({"current": [10.0, 12.0], "30daysAgo": [9.8, 11.5], "90daysAgo": [9.5, 11.0]}, index=["0y", "+1y"])
        self.eps_revisions = pd.DataFrame({"upLast30days": [5.0, 6.0], "downLast30days": [1.0, 1.0]}, index=["0y", "+1y"])
        self.earnings_estimate = pd.DataFrame({"growth": [0.1, 0.2], "numberOfAnalysts": [20.0, 19.0]}, index=["0y", "+1y"])

    @property
    def info(self):
        if "info" in self._broken:
            raise OSError("no profile")
        return {"shortName": "Alpha", "sector": "Technology", "forwardPE": 12.0, "marketCap": 5e10, "ignored": "x", "trailingPE": None}


@pytest.mark.unit
class TestFundamentals:
    def test_the_pieces_are_cut_down_to_plain_dicts(self, monkeypatch):
        monkeypatch.setattr(pdata.yf, "Ticker", lambda s: FakeTicker(s))
        out = pdata.fetch_fundamentals("AAA")
        assert out["symbol"] == "AAA" and out["errors"] == [] and "error" not in out
        assert out["info"] == {"shortName": "Alpha", "sector": "Technology", "forwardPE": 12.0, "marketCap": 5e10}
        assert out["cashflow"]["Operating Cash Flow"]["2026-09-30"] == 30.0
        assert "2025-09-30" not in out["cashflow"]["Free Cash Flow"]              # a missing value is left out, not stored as NaN
        assert out["income"] == {"Total Revenue": {"2026-09-30": 100.0, "2026-06-30": 95.0, "2026-03-31": 90.0, "2025-12-31": 88.0, "2025-09-30": 80.0}}
        assert "Operating Income" not in out["income"]                              # a label the statement lacks is simply absent
        assert out["eps_trend"]["+1y"] == {"current": 12.0, "30daysAgo": 11.5, "90daysAgo": 11.0}
        assert out["eps_revisions"]["0y"] == {"upLast30days": 5.0, "downLast30days": 1.0}
        assert out["estimate"]["+1y"] == {"growth": 0.2, "numberOfAnalysts": 19.0}
        import json

        json.dumps(out)                                                              # it can be saved as JSON as it is

    def test_one_failed_piece_does_not_lose_the_others(self, monkeypatch):
        monkeypatch.setattr(pdata.yf, "Ticker", lambda s: FakeTicker(s, broken=("info",)))
        out = pdata.fetch_fundamentals("AAA")
        assert out["errors"] == ["info: VendorUnavailableError"] and out["info"] == {} and out["income"]["Total Revenue"] and "error" not in out

    def test_a_symbol_with_nothing_is_marked_as_an_error(self, monkeypatch):
        class Empty:
            info = {}
            quarterly_cashflow = quarterly_income_stmt = quarterly_balance_sheet = pd.DataFrame()
            eps_trend = eps_revisions = earnings_estimate = pd.DataFrame()

        monkeypatch.setattr(pdata.yf, "Ticker", lambda s: Empty())
        assert pdata.fetch_fundamentals("ZZZ")["error"] == "Yahoo has no data for this symbol"


@pytest.mark.unit
class TestOthers:
    @pytest.mark.parametrize("close, expected", [(48.4, 4.84), (4.84, 4.84), (20.0, 20.0), (52.0, 5.2)])
    def test_the_ten_year_yield_whichever_way_yahoo_quotes_it(self, monkeypatch, close, expected):
        class T:
            def history(self, period):
                return pd.DataFrame({"Close": [close]})

        monkeypatch.setattr(pdata.yf, "Ticker", lambda s: T())
        assert pdata.fetch_ten_year_yield() == pytest.approx(expected)

    def test_no_yield_data_is_none(self, monkeypatch):
        class T:
            def history(self, period):
                return pd.DataFrame({"Close": []})

        monkeypatch.setattr(pdata.yf, "Ticker", lambda s: T())
        assert pdata.fetch_ten_year_yield() is None

    def test_industry_peers_keep_only_positive_forward_pes(self, monkeypatch):
        quotes = [{"forwardPE": 10.0}, {"forwardPE": -4.0}, {"forwardPE": None}, {"forwardPE": 0}, {}, {"forwardPE": 18.5}]
        monkeypatch.setattr(pdata.yf, "screen", lambda *a, **k: {"quotes": quotes, "total": 6})
        assert pdata.industry_forward_pes("Banks - Diversified", 2e9, 5e5) == [10.0, 18.5]         # the dash spelling is accepted

    def test_an_industry_the_screener_does_not_know_gives_an_empty_list(self, monkeypatch):
        monkeypatch.setattr(pdata.yf, "screen", lambda *a, **k: pytest.fail("the screener must not be asked"))
        assert pdata.industry_forward_pes("Nonsense Industry", 2e9, 5e5) == []

    def test_the_symbol_filter(self):
        assert all(pdata._PLAIN_SYMBOL.match(s) for s in ("A", "AAPL", "BRK-B", "ABCDE"))
        assert not any(pdata._PLAIN_SYMBOL.match(s) for s in ("", "AB.C", "TOOLONG", "aapl", "^GSPC", "A-BC"))
