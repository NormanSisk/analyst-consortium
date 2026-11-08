"""Lists what this project changed or added compared with the TradingAgents release it started from.

    python scripts/list_changes.py                 print the lists
    python scripts/list_changes.py --write FILE    rewrite the part of FILE between the inventory markers
    python scripts/list_changes.py --check FILE    exit 1 when that part of FILE is out of date
    python scripts/list_changes.py --base REF      compare with another commit (the default is the release we started from)

The comparison needs the original project's history. If it is missing, add it first:
    git remote add upstream https://github.com/TauricResearch/TradingAgents.git && git fetch upstream
"""

import argparse
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

BASE = "c454491"                      # TradingAgents v0.7.1, 2026-10-27: the release this project started from
BEGIN = "<!-- inventory:begin (written by scripts/list_changes.py; do not edit by hand) -->"
END = "<!-- inventory:end -->"
# Where an added file belongs, by the start of its path (the first match wins).
AREAS = (
    ("tests/", "Tests"),
    ("tradingagents/ui/", "Setup screen and run form (the local web page)"),
    ("tradingagents/picker/", "Stock picker"),
    ("tradingagents/research/", "Research studies"),
    ("tradingagents/dataflows/", "Data sources"),
    ("tradingagents/agents/", "Analysis engine"),
    ("tradingagents/graph/", "Analysis engine"),
    ("cli/", "Command line"),
    ("scripts/", "Scripts, icon and design notes"),
    ("docs/", "Scripts, icon and design notes"),
    ("tradingagents/assets/", "Scripts, icon and design notes"),
)
OTHER = "Other program files"
STATUS_WORDS = {"A": "added", "M": "modified", "D": "removed"}


def parse_name_status(text: str) -> list[tuple[str, str]]:
    """[(A, M or D, path)] from ``git diff --name-status --no-renames``."""
    rows = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0][:1] in STATUS_WORDS:
            rows.append((parts[0][:1], parts[-1]))
    return rows


def parse_numstat(text: str) -> dict[str, tuple[str, str]]:
    """{path: (lines added, lines removed)} from ``git diff --numstat --no-renames`` (a binary file shows as "-")."""
    stats = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            stats[parts[2]] = (parts[0], parts[1])
    return stats


def area_of(path: str) -> str:
    return next((name for prefix, name in AREAS if path.startswith(prefix)), OTHER)


def render(rows: list[tuple[str, str]], stats: dict[str, tuple[str, str]], base: str = BASE) -> str:
    by_status = {s: sorted(p for st, p in rows if st == s) for s in STATUS_WORDS}
    out = [f"Compared with commit `{base}` (TradingAgents v0.7.1). A file that is not listed here is unchanged.", ""]
    out += [f"#### Upstream files we modified ({len(by_status['M'])})", ""]
    for path in by_status["M"]:
        plus, minus = stats.get(path, ("?", "?"))
        out.append(f"- `{path}` (+{plus} -{minus})")
    out += ["", f"#### Files we added ({len(by_status['A'])})", ""]
    areas: dict[str, list[str]] = defaultdict(list)
    for path in by_status["A"]:
        areas[area_of(path)].append(path)
    order = [name for name in dict.fromkeys([n for _, n in AREAS] + [OTHER]) if name in areas]
    for name in order:
        out += [f"**{name}** ({len(areas[name])})", ""] + [f"- `{p}`" for p in areas[name]] + [""]
    out += [f"#### Upstream files we removed ({len(by_status['D'])})", ""]
    out += [f"- `{p}`" for p in by_status["D"]] if by_status["D"] else ["None."]
    return "\n".join(out).rstrip() + "\n"


def collect(base: str, run=subprocess.run) -> tuple[list[tuple[str, str]], dict[str, tuple[str, str]]]:
    def git(*args: str) -> str:
        done = run(["git", "diff", *args, base, "HEAD"], capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise SystemExit(f"git could not compare with {base}: {(done.stderr or '').strip()}\n"
                             "Add the original project first: git remote add upstream "
                             "https://github.com/TauricResearch/TradingAgents.git && git fetch upstream")
        return done.stdout

    return parse_name_status(git("--name-status", "--no-renames")), parse_numstat(git("--numstat", "--no-renames"))


def replace_section(document: str, section: str) -> str:
    """The document with the text between the markers replaced (ValueError when a marker is missing)."""
    if BEGIN not in document or END not in document:
        raise ValueError(f"the file needs these two marker lines:\n{BEGIN}\n{END}")
    head, rest = document.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    return f"{head}{BEGIN}\n\n{section}\n{END}{tail}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=BASE)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--write", type=Path, metavar="FILE")
    action.add_argument("--check", type=Path, metavar="FILE")
    args = parser.parse_args(argv)
    section = render(*collect(args.base), base=args.base)
    if args.write:
        args.write.write_text(replace_section(args.write.read_text(encoding="utf-8"), section), encoding="utf-8")
        print(f"Updated the file lists in {args.write}")
        return 0
    if args.check:
        current = args.check.read_text(encoding="utf-8")
        if replace_section(current, section) != current:
            print(f"The file lists in {args.check} are out of date. Run: python scripts/list_changes.py --write {args.check}")
            return 1
        print(f"The file lists in {args.check} are current.")
        return 0
    print(section)
    return 0


if __name__ == "__main__":
    sys.exit(main())
