"""Text that is safe to put in a spreadsheet cell (a local addition).

A spreadsheet reads a cell that starts with ``=``, ``+``, ``-``, ``@`` (or a tab or carriage return) as a formula, so text
that comes from a website or an AI model could run one when the file is opened. The usual defence is to put an apostrophe
in front. Both the analysis export and the stock picker use this one helper.
"""

FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value) -> str:
    """Text for a spreadsheet cell, defused if a spreadsheet would read it as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(FORMULA_STARTS) else text
