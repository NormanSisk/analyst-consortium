"""The picker's scoreboard: reading the picks log, measuring each stock against the S&P 500, and saying so honestly.

Prices are made up with a fixed daily return, so every expected number can be worked out by hand (no network)."""

import csv
import io
import json
from datetime import date

import pytest

from tests import picker_world as w
from tradingagents.picker import scoreboard as sb
from tradingagents.picker.quicklook import PickerError

MARKET_DAY, UP_DAY, DOWN_DAY = 0.001, 0.003, -0.002
LAST = w.INDEX[-1].date()


def shown_days_ago(sessions: int) -> str:
    return w.INDEX[-1 - sessions].date().isoformat()


def row(symbol="UP", ago=70, recipe="beaten-down", met=5, rank=1, name="", price="100", as_of=None):
    return {"logged_at": "2026-11-08T10:00:00+00:00", "as_of": as_of or shown_days_ago(ago), "recipe": recipe, "rank": str(rank), "symbol": symbol,
            "name": name or f"{symbol} Corp", "price": price, "conditions_met": str(met)}


def world(**extra) -> w.FakeProvider:
    closes = {"^GSPC": w.levels([MARKET_DAY] * w.DAYS), "UP": w.levels([UP_DAY] * w.DAYS), "DOWN": w.levels([DOWN_DAY] * w.DAYS)}
    closes.update(extra)
    return w.FakeProvider(closes=closes)


def grow(daily: float, days: int) -> float:
    return (1 + daily) ** days - 1


@pytest.mark.unit
class TestReadingTheLog:
    def test_the_same_stock_shown_twice_on_one_day_counts_once(self):
        picks, bad = sb.parse_rows([row("UP"), row("UP", rank=3), row("UP", recipe="list"), row("UP", ago=50), row("DOWN")])
        assert bad == 0 and [(p.recipe, p.symbol) for p in picks] == [("beaten-down", "UP"), ("list", "UP"), ("beaten-down", "UP"), ("beaten-down", "DOWN")]
        assert picks[0].rank == 1                                      # the first row is the one kept

    @pytest.mark.parametrize("bad_row", [
        {**row(), "as_of": "last tuesday"}, {**row(), "as_of": ""}, {**row(), "symbol": "../etc"}, {**row(), "symbol": ""},
        {**row(), "rank": "first"}, {**row(), "price": "cheap"},
    ])
    def test_a_row_that_cannot_be_read_is_counted_not_guessed(self, bad_row):
        picks, bad = sb.parse_rows([bad_row, row("DOWN")])
        assert bad == 1 and [p.symbol for p in picks] == ["DOWN"]

    def test_a_missing_price_is_allowed_and_a_blank_recipe_is_named(self):
        picks, bad = sb.parse_rows([{**row(), "price": "", "recipe": ""}])
        assert bad == 0 and picks[0].price_then is None and picks[0].recipe == "unknown"


@pytest.mark.unit
class TestMeasuring:
    def measured(self, symbol="UP", ago=70, provider=None):
        provider = provider or world()
        pick = sb.parse_rows([row(symbol, ago)])[0][0]
        closes = provider.closes([symbol, "^GSPC"])
        return sb.measure(pick, closes.get(symbol), closes["^GSPC"])

    def test_the_returns_are_the_stocks_and_the_markets_over_the_same_days(self):
        p = self.measured("UP", 70)
        assert p.sessions == 70 and p.note == ""
        assert p.to_date.stock == pytest.approx(grow(UP_DAY, 70)) and p.to_date.market == pytest.approx(grow(MARKET_DAY, 70))
        assert p.to_date.excess == pytest.approx(grow(UP_DAY, 70) - grow(MARKET_DAY, 70))
        for h in (5, 21, 63):
            assert p.horizons[h].stock == pytest.approx(grow(UP_DAY, h)) and p.horizons[h].market == pytest.approx(grow(MARKET_DAY, h))

    def test_a_falling_stock_has_a_negative_excess(self):
        p = self.measured("DOWN", 30)
        assert p.to_date.stock == pytest.approx(grow(DOWN_DAY, 30)) and p.to_date.excess < 0
        assert p.horizons[21] is not None and p.horizons[63] is None            # 63 sessions have not passed yet

    def test_a_recent_pick_has_a_return_to_date_but_no_horizon_yet(self):
        p = self.measured("UP", 3)
        assert p.sessions == 3 and p.to_date.stock == pytest.approx(grow(UP_DAY, 3)) and p.horizons == {5: None, 21: None, 63: None}

    def test_a_pick_shown_on_the_last_day_has_nothing_to_compare_yet(self):
        p = self.measured("UP", 0)
        assert p.to_date is None and p.note == "" and p.sessions == 0

    def test_a_weekend_date_uses_the_friday_close(self):
        days = [d.date() for d in w.INDEX]
        friday = next(d for d in reversed(days[:-10]) if d.weekday() == 4)
        saturday = date.fromordinal(friday.toordinal() + 1)
        pick = sb.parse_rows([row(as_of=saturday.isoformat())])[0][0]
        closes = world().closes(["UP", "^GSPC"])
        sb.measure(pick, closes["UP"], closes["^GSPC"])
        assert pick.sessions == len(days) - 1 - days.index(friday) and pick.note == ""

    def test_no_prices_at_all_says_so(self):
        p = self.measured("GONE", 70)
        assert p.to_date is None and "no prices from Yahoo" in p.note

    def test_prices_that_start_after_the_day_it_was_shown_are_explained(self):
        provider = world(NEWBIE=w.levels([UP_DAY] * 20))
        p = self.measured("NEWBIE", 70, provider)
        assert p.to_date is None and "do not reach back" in p.note

    def test_prices_that_stopped_long_ago_are_explained(self):
        stopped = w.levels([UP_DAY] * 100)
        stopped = stopped.iloc[:-60]                                            # ends 60 sessions before the last day
        p = self.measured("OLD", 20, world(OLD=stopped))
        assert p.to_date is None and "end on" in p.note

    def test_a_zero_price_is_not_a_starting_price(self):
        bad = w.levels([UP_DAY] * w.DAYS)
        bad.iloc[:] = 0.0
        p = self.measured("ZERO", 70, world(ZERO=bad))
        assert p.to_date is None and p.note


@pytest.mark.unit
class TestStatistics:
    def test_the_average_median_and_count_of_those_that_beat_the_market(self):
        moves = [sb.Move(0.10, 0.02), sb.Move(-0.05, 0.02), sb.Move(0.04, 0.02)]
        s = sb.stat(moves)
        assert s.n == 3 and s.beat == 2
        assert s.avg_stock == pytest.approx(0.03) and s.avg_market == pytest.approx(0.02) and s.avg_excess == pytest.approx(0.01)
        assert s.median_excess == pytest.approx(0.02) and sb.stat([]) is None

    @pytest.mark.parametrize("met, label", [(5, "5 of 5 conditions"), (4, "4 of 5 conditions"), (3, "3 or fewer conditions"), (0, "3 or fewer conditions")])
    def test_the_buckets(self, met, label):
        assert sb.bucket_of(met) == label


@pytest.mark.unit
class TestTheScoreboard:
    def test_lists_are_grouped_newest_first_and_each_method_is_summarized(self):
        rows = [row("UP", 70, met=5), row("DOWN", 70, met=4), row("UP", 30, met=5), row("UP", 10, recipe="list", met=3)]
        board = sb.build(rows, world())
        assert [(b.shown.isoformat(), b.recipe, b.recorded) for b in board.batches] == [
            (shown_days_ago(10), "list", 1), (shown_days_ago(30), "beaten-down", 1), (shown_days_ago(70), "beaten-down", 2)]
        old = board.batches[-1]
        assert old.sessions == 70 and old.stats["to_date"].n == 2 and old.stats[63].n == 2 and old.stats[5].n == 2
        down = board.recipes["beaten-down"]
        assert down["stocks"] == 3 and down["lists"] == 2 and down["first"] == w.INDEX[-71].date()
        assert down["stats"][63].n == 2 and down["stats"][21].n == 3 and board.recipes["list"]["stats"][21] is None
        assert board.as_of == LAST

    def test_buckets_split_a_method_by_conditions_met(self):
        board = sb.build([row("UP", 70, met=5), row("DOWN", 70, met=4), row("UP", 60, met=3)], world())
        buckets = board.buckets["beaten-down"]
        assert buckets["5 of 5 conditions"]["stocks"] == 1 and buckets["4 of 5 conditions"]["stats"]["to_date"].avg_excess < 0
        assert buckets["3 or fewer conditions"]["stocks"] == 1

    def test_stocks_are_ordered_by_the_day_shown_whatever_order_the_log_was_written_in(self):
        rows = [row("UP", 10, rank=1), row("DOWN", 70, rank=2), row("UP", 70, rank=1), row("UP", 40, recipe="list", rank=1)]
        board = sb.build(rows, world())
        assert [(p.shown.isoformat(), p.symbol) for p in board.picks] == [
            (shown_days_ago(70), "UP"), (shown_days_ago(70), "DOWN"), (shown_days_ago(40), "UP"), (shown_days_ago(10), "UP")]

    def test_nothing_recorded(self):
        board = sb.build([], world())
        assert board.picks == [] and "Nothing is recorded yet" in board.verdict and board.as_of is None

    def test_unreadable_rows_are_reported(self):
        board = sb.build([{**row(), "as_of": "soon"}, row("UP", 70)], world())
        assert any("1 row of the picks log could not be read" in x for x in board.warnings) and len(board.picks) == 1

    def test_a_stock_that_could_not_be_measured_is_left_out_of_the_averages_and_reported(self):
        board = sb.build([row("UP", 70), row("GONE", 70)], world())
        assert board.recipes["beaten-down"]["stats"]["to_date"].n == 1
        assert any("1 recorded stock could not be measured" in x for x in board.warnings)
        assert [p.note for p in board.picks if p.symbol == "GONE"][0].startswith("no prices")

    def test_a_pick_shown_today_is_reported_as_waiting(self):
        board = sb.build([row("UP", 0)], world())
        assert any("shown on the latest day that has prices" in x for x in board.warnings) and "None of the recorded stocks" in board.verdict

    def test_no_market_prices_is_a_plain_error(self):
        provider = world()
        del provider._closes["^GSPC"]
        with pytest.raises(PickerError, match="S&P 500 prices"):
            sb.build([row("UP", 70)], provider)

    def test_only_one_download_is_made_for_all_the_stocks(self):
        provider = world()
        sb.build([row("UP", 70), row("DOWN", 70), row("UP", 30)], provider)
        assert len(provider.closes_calls) == 1 and sorted(provider.closes_calls[0]) == ["DOWN", "UP", "^GSPC"]


@pytest.mark.unit
class TestTheVerdict:
    def test_too_early_when_no_stock_has_reached_the_first_reading(self):
        board = sb.build([row("UP", 10), row("DOWN", 10)], world())
        assert board.verdict.startswith("Too early to say anything") and "10 trading sessions" in board.verdict

    def test_too_early_when_there_are_few_stocks_or_lists(self):
        board = sb.build([row("UP", 70), row("DOWN", 70), row("UP", 40)], world())
        assert board.verdict.startswith("Too early to judge") and "3 stocks from 2 lists" in board.verdict and "independent results" in board.verdict

    def test_enough_lists_give_the_numbers_but_never_call_it_proof(self):
        symbols = [f"S{i}" for i in range(8)]
        provider = world(**{s: w.levels([0.002 + i * 0.0005] * w.DAYS) for i, s in enumerate(symbols)})
        rows = [row(s, ago) for s in symbols for ago in (70, 60, 50, 40)]
        board = sb.build(rows, provider)
        assert board.verdict.startswith("Worth a look, but not proof") and "32 stocks from 4 lists" in board.verdict and "of 32 beat it" in board.verdict
        assert "advice" not in board.verdict.lower() and "buy" not in board.verdict.lower()


@pytest.mark.unit
class TestShowingIt:
    @pytest.fixture
    def board(self):
        return sb.build([row("UP", 70, met=5, name="=cmd|' /C calc'!A0"), row("DOWN", 70, met=4), row("UP", 4, recipe="list", met=3), row("GONE", 70)], world())

    def test_the_text_names_the_yardstick_the_methods_and_the_lists(self, board):
        text = sb.render_text(board)
        assert "HOW THE RECORDED PICKS DID" in text and "S&P 500" in text and "BEATEN-DOWN QUALITY" in text and "YOUR OWN LISTS" in text
        assert "EACH LIST, NEWEST FIRST" in text and "After 21 sessions" in text and "not a forecast and not advice" in text
        assert "more volatile than the index" in text

    def test_json_is_complete_and_plain(self, board):
        data = sb.to_json(board)
        json.dumps(data)                                                        # nothing that cannot be sent to the page
        assert data["horizons"] == [5, 21, 63] and data["as_of"] == LAST.isoformat() and data["verdict"] == board.verdict
        beaten = next(r for r in data["recipes"] if r["id"] == "beaten-down")
        assert beaten["label"] == "Beaten-down quality" and beaten["stats"]["21"]["n"] == 2
        assert [b["label"] for b in beaten["buckets"]] == ["5 of 5 conditions", "4 of 5 conditions"]       # an empty bucket is not listed
        up = next(p for p in data["picks"] if p["symbol"] == "UP" and p["recipe"] == "beaten-down")
        assert up["horizons"]["21"]["stock"] == pytest.approx(grow(UP_DAY, 21)) and up["horizons"]["63"]["excess"] > 0
        gone = next(p for p in data["picks"] if p["symbol"] == "GONE")
        assert gone["to_date"] is None and gone["note"]

    def test_the_csv_has_one_row_per_stock_with_the_reason_when_it_could_not_be_measured(self, board):
        data = sb.to_csv(board)
        assert data.startswith(b"\xef\xbb\xbf")
        rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
        assert len(rows) == 4 and list(rows[0]) == sb.csv_columns()
        up = next(r for r in rows if r["symbol"] == "UP" and r["recipe"] == "beaten-down")
        assert float(up["return_21d_pct"]) == pytest.approx(round(grow(UP_DAY, 21) * 100, 2)) and up["sessions_since"] == "70"
        assert next(r for r in rows if r["symbol"] == "GONE")["note"].startswith("no prices") and next(r for r in rows if r["symbol"] == "GONE")["return_to_date_pct"] == ""
        assert up["name"].startswith("'=")                                      # a name that looks like a formula is defused
