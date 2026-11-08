"""Every number the picker uses, in one place, each with a default and a plain meaning (a local addition).

Nothing is required: leave a setting alone and its default applies. The market-explained thresholds (60% and 45%) are the
same ones the analysis uses for its second deal condition, so the picker and the analysis agree on what "mostly
market-driven" means.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace


@dataclass(frozen=True)
class PickerSettings:
    # the universe (for a screen of the market)
    min_market_cap: float = 2e9          # dollars; smaller companies are left out
    min_avg_volume: float = 5e5          # shares a day (3-month average); thinner stocks are left out
    universe_limit: int = 800            # the largest this many stocks are screened
    region: str = "us"

    # "beaten down" and "the market explains it"
    below_high: float = 0.20             # at least this far below the 52-week high (0.20 = 20%)
    share_63: float = 0.60               # the market must explain at least this share of a 63-session fall
    share_21: float = 0.45               # ...and of a 21-session fall
    carry: int = 60                      # at most this many stocks go on to the fundamentals pass

    # the fundamentals checks
    revision_min_analysts: int = 3       # this many more analysts cutting than raising an estimate counts as a cut
    estimate_cut_pct: float = 3.0        # a consensus EPS down this much in 90 days counts as a cut
    margin_drop_points: float = 3.0      # operating margin this many points below a year ago is deterioration
    cash_flow_drop: float = 0.25         # operating cash flow this far below a year ago is deterioration
    debt_multiple: float = 2.0           # total debt this many times a year ago is deterioration
    dilution_pct: float = 5.0            # share count up this much in a year is deterioration
    heavy_dilution_pct: float = 10.0     # share count up this much in a year fails the survival test (a run of equity sales)
    runway_months: float = 12.0          # cash must cover this many months of burn when free cash flow is negative
    max_pe_vs_peers: float = 1.25        # forward P/E may be at most this multiple of the peers' median
    min_peers: int = 5                   # fewer peers than this and the sector median is used instead
    low_pe_flag: float = 6.0             # a forward P/E below this is flagged: it often means the market expects earnings to fall

    # the answer
    top: int = 15                        # how many to show

    def validated(self) -> "PickerSettings":
        """This settings object, or ValueError naming the first setting that is out of range."""
        rules = {
            "min_market_cap": (0, 1e13), "min_avg_volume": (0, 1e10), "universe_limit": (10, 3000), "below_high": (0, 0.95),
            "share_63": (0, 1), "share_21": (0, 1), "carry": (1, 500), "revision_min_analysts": (1, 50),
            "estimate_cut_pct": (0, 100), "margin_drop_points": (0, 100), "cash_flow_drop": (0, 1), "debt_multiple": (1, 100),
            "dilution_pct": (0, 1000), "heavy_dilution_pct": (0, 1000), "runway_months": (0, 240), "max_pe_vs_peers": (0.1, 20),
            "min_peers": (1, 100), "low_pe_flag": (0, 100), "top": (1, 500),
        }
        for name, (low, high) in rules.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise ValueError(f"{name} must be a number from {low:g} to {high:g}.")
        if self.region not in ("us",):
            raise ValueError("region must be 'us': the market-context data the checks use is US-only.")
        return self

    @classmethod
    def with_overrides(cls, **overrides) -> "PickerSettings":
        """The defaults with the given settings replaced; a value of None leaves the default; an unknown name is an error."""
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(overrides) - known)
        if unknown:
            raise ValueError(f"unknown picker setting: {', '.join(unknown)}")
        return replace(cls(), **{k: v for k, v in overrides.items() if v is not None}).validated()
