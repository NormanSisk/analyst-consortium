"""Tests for the public-records vendor (a local addition): federal awards, clinical studies, FDA recalls."""

import pytest

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors import public_records as pr

AWARDS = {"contracts": 3, "top": [
    {"Award ID": "W1", "Recipient Name": "KOPIN TARGETING CORPORATION", "Award Amount": 249722.22,
     "Awarding Agency": "Department of Defense", "Start Date": "2026-03-02", "Description": "SBIR PHASE I | TOPIC A254-035"},
    {"Award ID": "W2", "Recipient Name": "KOPIN CORPORATION", "Award Amount": 100000.0,
     "Awarding Agency": "Department of the Army", "Start Date": "2026-01-15", "Description": None}]}
CLINICAL = {"recruiting": 242, "phase3": 74, "phase2": 70, "recalls": 3, "class_one": 0}


class _Response:
    def __init__(self, status=200, payload=None):
        self.status_code, self._payload = status, payload or {}

    def json(self):
        return self._payload


@pytest.mark.unit
class TestNames:
    @pytest.mark.parametrize("title, expected", [
        ("KOPIN CORP", "Kopin"), ("QUALCOMM INC/DE", "Qualcomm"), ("Bloom Energy Corp", "Bloom Energy"),
        ("Greenland Mines Ltd", "Greenland Mines"), ("PFIZER INC", "Pfizer"), ("Alphabet Inc. Class A", "Alphabet"),
        ("8x8 Inc", "8x8")])
    def test_a_registered_name_becomes_a_search_string(self, title, expected):
        assert pr.company_search_name(title) == expected


@pytest.mark.unit
class TestReport:
    def test_awards_are_tabled_with_the_amount_called_obligated_and_pipes_escaped(self):
        text = pr.build_report("KOPN", "Kopin", AWARDS, None, "Not searched.", "2026-11-07")
        assert text.startswith("# Public records for KOPN (searched as 'Kopin', through 2026-11-07)")
        assert "3 new federal contract awards in the last 12 months to recipients matching 'Kopin'; the 2 largest, $349,722 in all:" in text
        assert "| 2026-03-02 | Department of Defense | $249,722 | KOPIN TARGETING CORPORATION | SBIR PHASE I / TOPIC A254-035 |" in text
        assert "obligated value so far, not revenue recognized" in text and "Not searched." in text

    def test_no_awards_is_said_with_the_limits_of_a_name_search(self):
        text = pr.build_report("TEST", "Test", {"contracts": 0, "top": []}, None, "", "2026-11-07")
        assert "No new federal contract awards were found" in text and "will not appear" in text

    def test_clinical_counts_and_recalls_are_given_and_a_class_one_recall_is_flagged(self):
        text = pr.build_report("PFE", "Pfizer", AWARDS, CLINICAL, "", "2026-11-07")
        assert "recruiting now: 242 (phase 3: 74, phase 2: 70)." in text
        assert "recalls) naming 'Pfizer' in the last 12 months: 3 (Class I, the most serious: 0)." in text
        assert "No rule-based flags." in text
        flagged = pr.build_report("PFE", "Pfizer", AWARDS, dict(CLINICAL, class_one=2), "", "2026-11-07")
        assert "- 2 Class I FDA recall(s) naming the company were reported in the last 12 months." in flagged

    def test_unreachable_sources_are_said_not_filled_in(self):
        text = pr.build_report("PFE", "Pfizer", None, {"recruiting": None, "recalls": None}, "", "2026-11-07")
        assert "USAspending could not be reached this time." in text
        assert "ClinicalTrials.gov could not be reached." in text and "openFDA could not be reached." in text


@pytest.mark.unit
class TestFetchers:
    def test_awards_read_the_count_and_the_top_list_and_ask_for_the_twelve_months_to_the_date(self, monkeypatch):
        seen = []

        def fake(method, url, **kwargs):
            seen.append((method, url, kwargs))
            if url == pr._COUNT_URL:
                return _Response(200, {"results": {"contracts": 1}})
            return _Response(200, {"results": AWARDS["top"]})

        monkeypatch.setattr(pr.requests, "request", fake)
        result = pr._awards("Kopin", "2026-11-07")
        assert result == {"contracts": 1, "top": AWARDS["top"]}
        period = seen[0][2]["json"]["filters"]["time_period"][0]
        assert period == {"start_date": "2025-11-07", "end_date": "2026-11-07", "date_type": "new_awards_only"}
        assert seen[0][2]["json"]["filters"]["award_type_codes"] == ["A", "B", "C", "D"]

    def test_a_failed_or_unreachable_awards_search_is_none(self, monkeypatch):
        monkeypatch.setattr(pr.requests, "request", lambda *a, **k: _Response(500))
        assert pr._awards("Kopin", "2026-11-07") is None

        def down(*args, **kwargs):
            raise pr.requests.ConnectionError("down")

        monkeypatch.setattr(pr.requests, "request", down)
        assert pr._awards("Kopin", "2026-11-07") is None

    def test_clinical_counts_and_recalls_treat_an_openfda_404_as_zero(self, monkeypatch):
        def fake(method, url, **kwargs):
            if url == pr._TRIALS_URL:
                phase = kwargs["params"].get("aggFilters")
                return _Response(200, {"totalCount": {"phase:3": 74, "phase:2": 70}.get(phase, 242)})
            query = kwargs["params"]["search"]
            if "Class I" in query:
                return _Response(404)
            assert "report_date:[20251107 TO 20261107]" in query
            return _Response(200, {"meta": {"results": {"total": 3}}})

        monkeypatch.setattr(pr.requests, "request", fake)
        assert pr._clinical("Pfizer", "2026-11-07") == CLINICAL


@pytest.mark.unit
class TestEntryPoint:
    @pytest.fixture
    def world(self, monkeypatch):
        monkeypatch.setattr(pr, "_sec_title", lambda symbol: {"KOPN": "KOPIN CORP", "PFE": "PFIZER INC"}.get(symbol))
        monkeypatch.setattr(pr, "_awards", lambda name, as_of: AWARDS)
        monkeypatch.setattr(pr, "_clinical", lambda name, as_of: CLINICAL)
        profile = {"KOPN": {"sectorKey": "industrials"}, "PFE": {"sectorKey": "healthcare"}}
        monkeypatch.setattr(pr, "yf", type("FakeYF", (), {"Ticker": lambda symbol: type("T", (), {"info": profile[symbol]})()}))

    def test_a_health_care_company_gets_the_clinical_section_and_others_are_told_it_was_not_searched(self, world):
        assert "recruiting now: 242" in pr.get_public_records("pfe", None)
        other = pr.get_public_records("KOPN", None)
        assert "Not searched: Yahoo does not file this company under health care." in other and "recruiting now" not in other

    def test_a_past_run_gets_awards_as_of_its_date_and_no_clinical_section_or_profile_read(self, world, monkeypatch):
        monkeypatch.setattr(pr, "yf", type("NoYF", (), {"Ticker": lambda symbol: pytest.fail("the profile must not be read")}))
        monkeypatch.setattr(pr, "_awards", lambda name, as_of: (_ for _ in ()).throw(AssertionError) if as_of != "2025-06-30" else AWARDS)
        out = pr.get_public_records("PFE", "2025-06-30")
        assert "Not served to a run dated in the past" in out and "through 2025-06-30" in out

    def test_a_ticker_with_no_registered_name_is_no_data(self, world):
        with pytest.raises(NoMarketDataError):
            pr.get_public_records("7203.T", None)

    def test_the_fundamentals_analyst_is_offered_the_tool_and_the_router_knows_it(self):
        from tradingagents.agents.analysts import fundamentals_analyst, toolset
        from tradingagents.dataflows import router
        from tradingagents.default_config import DEFAULT_CONFIG

        assert "get_public_records" in [t.name for t in fundamentals_analyst.TOOLS]
        assert "get_public_records" in toolset.guidance(fundamentals_analyst.TOOLS, fundamentals_analyst._GUIDANCE)
        assert router.get_category_for_method("get_public_records") == "public_records"
        assert "public_records" in router.OPTIONAL_CATEGORIES and "public_data" in router.VENDOR_METHODS["get_public_records"]
        assert DEFAULT_CONFIG["data_vendors"]["public_records"] == "public_data"
