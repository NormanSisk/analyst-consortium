"""What each agent says and asks for while an analysis runs, one JSON line per event (a local addition).

The run already writes ``message_tool.log``, but that log does not say which analyst a line came from, and the four
analysts work at the same time, so their lines are interleaved. This keeps a second, structured log for the page's
live view: who spoke, what kind of event it was, and the text. It records only what the agents visibly say and the data
they ask for. The AI providers do not expose a model's private reasoning, so none is, or could be, recorded here.

Writing is best effort: a full disk or a locked file must never end an analysis.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

FILE_NAME = "events.jsonl"
TEXT_KEEP = 4000        # an agent's message is kept up to this many characters
REQUEST_KEEP = 400      # a data request (tool name and arguments)
DATA_KEEP = 500         # the start of a data reply; its full length is recorded as ``size``
KINDS = {"Agent": "thought", "Tool Call": "request", "Data": "data", "System": "system"}
TEAM = "Team"           # events that come from the run as a whole, not from one analyst


def source_name(namespace) -> str | None:
    """The agent that owns a streamed step, from the stream's namespace (``("Market Analyst:3f2a...",)``)."""
    if not namespace:
        return None
    return str(namespace[0]).split(":")[0] or None


class EventLog:
    """Appends events to ``path``, starting the file afresh so a second run the same day does not mix with the first."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._n = 0
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("", encoding="utf-8")
        except OSError:
            pass

    def record(self, source: str | None, message_type: str, text: str | None) -> None:
        """Add one event. ``message_type`` is the run's own label (Agent, Tool Call, Data, System); others are skipped."""
        kind = KINDS.get(message_type)
        body = (text or "").strip()
        if kind is None or not body:
            return
        size = len(body)
        keep = {"thought": TEXT_KEEP, "request": REQUEST_KEEP, "data": DATA_KEEP, "system": TEXT_KEEP}[kind]
        event = {"t": datetime.now().strftime("%H:%M:%S"), "source": source or TEAM, "kind": kind, "text": body[:keep]}
        if kind == "data":
            event["size"] = size
        with self._lock:
            self._n += 1
            event["n"] = self._n
            try:
                with open(self.path, "a", encoding="utf-8", newline="\n") as handle:      # one line is one event on every system
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            except OSError:
                pass


def read_events(path: str | Path, after: int = 0, limit_bytes: int = 1_000_000) -> tuple[list[dict], int]:
    """The events written after byte ``after``, and the offset to ask from next time.

    Only whole lines are returned: a line still being written is left for the next call. A damaged line is skipped.
    """
    try:
        with open(path, "rb") as handle:
            handle.seek(max(0, int(after)))
            chunk = handle.read(limit_bytes)
    except (OSError, ValueError):
        return [], max(0, int(after) if isinstance(after, int) else 0)
    end = chunk.rfind(b"\n")
    if end < 0:
        return [], max(0, int(after))
    events = []
    for line in chunk[:end + 1].splitlines():
        try:
            item = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events, max(0, int(after)) + end + 1
