"""Where the picker keeps its files (a local addition): next to the results folder, in ``picker/``."""

from __future__ import annotations

from pathlib import Path

from tradingagents.default_config import build_default_config


def picker_folder() -> Path:
    """The folder for the picker's day cache and its record of lists shown (never inside the project)."""
    return Path(build_default_config()["results_dir"]).parent / "picker"
