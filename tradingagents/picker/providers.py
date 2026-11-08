"""Where the picker's data comes from (a local addition).

The picker asks a *provider* for five things, so a test can hand it made-up data and a different data source can be added
later without touching the logic. ``YahooProvider`` is the default and needs no key.
"""

from __future__ import annotations

from typing import Protocol

import pandas as pd

from tradingagents.picker.settings import PickerSettings


class Provider(Protocol):
    def universe(self, settings: PickerSettings) -> list[dict]:
        """Stocks to screen: dicts with symbol, name, sector, market_cap, forward_pe."""

    def closes(self, symbols: list[str]) -> dict[str, pd.Series]:
        """Adjusted daily closes by symbol (oldest first); includes ``^GSPC`` and ``^VIX`` when asked for them."""

    def fundamentals(self, symbol: str) -> dict:
        """Profile, quarterly statements and estimates as plain dicts (see ``picker_data.fetch_fundamentals``)."""

    def ten_year_yield(self) -> float | None:
        """The 10-year Treasury yield in percent."""

    def industry_forward_pes(self, industry: str, settings: PickerSettings) -> list[float]:
        """Forward P/Es of the stocks in one industry, for a peer median."""


class YahooProvider:
    """Yahoo Finance through yfinance (the downloads live in the data layer)."""

    def universe(self, settings: PickerSettings) -> list[dict]:
        from tradingagents.dataflows.vendors.yahoo import picker_data

        return picker_data.screen_universe_with_sectors(settings.min_market_cap, settings.min_avg_volume,
                                                        settings.universe_limit, settings.region)

    def closes(self, symbols: list[str]) -> dict[str, pd.Series]:
        from tradingagents.dataflows.vendors.yahoo import picker_data

        return picker_data.fetch_closes(symbols)

    def fundamentals(self, symbol: str) -> dict:
        from tradingagents.dataflows.vendors.yahoo import picker_data

        return picker_data.fetch_fundamentals(symbol)

    def ten_year_yield(self) -> float | None:
        from tradingagents.dataflows.vendors.yahoo import picker_data

        return picker_data.fetch_ten_year_yield()

    def industry_forward_pes(self, industry: str, settings: PickerSettings) -> list[float]:
        from tradingagents.dataflows.vendors.yahoo import picker_data

        return picker_data.industry_forward_pes(industry, settings.min_market_cap, settings.min_avg_volume, settings.region)
