"""The live event log: what is recorded, what is left out, and how a reader follows it without missing or doubling a line."""

import json

import pytest

from tradingagents import live_events as le


@pytest.mark.unit
class TestRecording:
    def test_each_kind_is_recorded_with_its_source_and_a_running_number(self, tmp_path):
        log = le.EventLog(tmp_path / "run" / le.FILE_NAME)
        log.record("Market Analyst", "Agent", "I'll look at the price history first.")
        log.record("Market Analyst", "Tool Call", "get_stock_data(start_date=2025-11-08)")
        log.record("Market Analyst", "Data", "Date,Open\n2025-11-10,68.1")
        log.record(None, "System", "Selected ticker: KO")
        events, _ = le.read_events(tmp_path / "run" / le.FILE_NAME)
        assert [(e["n"], e["source"], e["kind"]) for e in events] == [
            (1, "Market Analyst", "thought"), (2, "Market Analyst", "request"), (3, "Market Analyst", "data"), (4, "Team", "system")]
        assert events[2]["size"] == len("Date,Open\n2025-11-10,68.1") and "size" not in events[0]
        assert all(len(e["t"]) == 8 for e in events)

    @pytest.mark.parametrize("kind", ["User", "Control", "Other"])
    def test_prompts_and_controls_are_not_recorded(self, tmp_path, kind):
        log = le.EventLog(tmp_path / le.FILE_NAME)
        log.record("News Analyst", kind, "KO")
        assert le.read_events(tmp_path / le.FILE_NAME)[0] == []

    @pytest.mark.parametrize("text", ["", "   \n", None])
    def test_an_empty_message_is_not_recorded(self, tmp_path, text):
        log = le.EventLog(tmp_path / le.FILE_NAME)
        log.record("News Analyst", "Agent", text)
        assert le.read_events(tmp_path / le.FILE_NAME)[0] == []

    def test_long_text_is_cut_and_a_data_reply_keeps_its_full_length(self, tmp_path):
        log = le.EventLog(tmp_path / le.FILE_NAME)
        log.record("A", "Agent", "x" * 10_000)
        log.record("A", "Tool Call", "y" * 10_000)
        log.record("A", "Data", "z" * 10_000)
        thought, request, data = le.read_events(tmp_path / le.FILE_NAME)[0]
        assert len(thought["text"]) == le.TEXT_KEEP and len(request["text"]) == le.REQUEST_KEEP
        assert len(data["text"]) == le.DATA_KEEP and data["size"] == 10_000

    def test_a_new_run_starts_a_new_log(self, tmp_path):
        path = tmp_path / le.FILE_NAME
        le.EventLog(path).record("A", "Agent", "first run")
        second = le.EventLog(path)
        second.record("A", "Agent", "second run")
        events, _ = le.read_events(path)
        assert [e["text"] for e in events] == ["second run"] and events[0]["n"] == 1

    def test_non_latin_text_survives(self, tmp_path):
        le.EventLog(tmp_path / le.FILE_NAME).record("A", "Agent", "株価は上昇しています €")
        assert le.read_events(tmp_path / le.FILE_NAME)[0][0]["text"] == "株価は上昇しています €"

    def test_a_write_that_fails_never_raises(self, tmp_path, monkeypatch):
        log = le.EventLog(tmp_path / le.FILE_NAME)
        monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        log.record("A", "Agent", "still fine")            # no exception

    def test_a_log_that_cannot_be_started_never_raises(self, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("x", encoding="utf-8")
        le.EventLog(blocker / "inside" / le.FILE_NAME).record("A", "Agent", "no folder to write to")

    def test_threads_never_share_a_number(self, tmp_path):
        import threading

        log = le.EventLog(tmp_path / le.FILE_NAME)
        threads = [threading.Thread(target=lambda: [log.record("A", "Agent", "m") for _ in range(25)]) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        numbers = [e["n"] for e in le.read_events(tmp_path / le.FILE_NAME)[0]]
        assert sorted(numbers) == list(range(1, 151))


@pytest.mark.unit
class TestReading:
    def test_a_reader_picks_up_only_what_is_new(self, tmp_path):
        path = tmp_path / le.FILE_NAME
        log = le.EventLog(path)
        log.record("A", "Agent", "one")
        first, offset = le.read_events(path)
        log.record("A", "Agent", "two")
        log.record("B", "Agent", "three")
        second, offset2 = le.read_events(path, offset)
        assert [e["text"] for e in first] == ["one"] and [e["text"] for e in second] == ["two", "three"]
        assert le.read_events(path, offset2) == ([], offset2)

    def test_a_half_written_line_waits_for_the_next_look(self, tmp_path):
        path = tmp_path / le.FILE_NAME
        whole = json.dumps({"n": 1, "text": "done"}) + "\n"
        path.write_bytes((whole + '{"n": 2, "text": "unfini').encode("utf-8"))
        events, offset = le.read_events(path)
        assert [e["n"] for e in events] == [1] and offset == len(whole.encode("utf-8"))
        with open(path, "ab") as handle:
            handle.write(b'shed"}\n')
        later, _ = le.read_events(path, offset)
        assert [e["n"] for e in later] == [2]

    def test_a_damaged_line_is_skipped_and_the_rest_still_read(self, tmp_path):
        path = tmp_path / le.FILE_NAME
        path.write_bytes(b'{"n": 1}\nnot json at all\n[1, 2]\n{"n": 4}\n')
        events, _ = le.read_events(path)
        assert [e["n"] for e in events] == [1, 4]

    def test_a_missing_file_gives_nothing_and_keeps_the_offset(self, tmp_path):
        assert le.read_events(tmp_path / "none.jsonl", 40) == ([], 40)

    def test_a_bad_offset_does_not_raise(self, tmp_path):
        path = tmp_path / le.FILE_NAME
        le.EventLog(path).record("A", "Agent", "x")
        assert le.read_events(path, -5)[0][0]["text"] == "x"

    def test_the_offset_counts_bytes_not_characters(self, tmp_path):
        path = tmp_path / le.FILE_NAME
        log = le.EventLog(path)
        log.record("A", "Agent", "株" * 20)
        log.record("A", "Agent", "second")
        _, offset = le.read_events(path, 0, limit_bytes=10_000)
        assert offset == path.stat().st_size


@pytest.mark.unit
def test_the_source_is_the_node_name_before_the_task_id():
    assert le.source_name(("Market Analyst:3f2a9c",)) == "Market Analyst"
    assert le.source_name(("News Analyst:1b2c", "agent:9d8e")) == "News Analyst"
    assert le.source_name(()) is None and le.source_name(None) is None
    assert le.source_name((":only-a-task",)) is None
