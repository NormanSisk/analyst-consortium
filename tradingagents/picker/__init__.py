"""The stock picker: find candidates cheaply, check them in code, and leave the paid analysis for the few that survive (a local addition).

The funnel, from the free end to the paid end:

1. a *recipe* gives a list of stocks (a screen of the market, or your own list);
2. a price-only pass works out, for every stock at once, how far it is below its high and how much of its fall the market explains;
3. a fundamentals pass checks the survivors against the analysis's five "deal" conditions, in code, with the numbers and a
   plain-words reason for each, and ranks them;
4. you choose which few deserve the full analysis (about 0.35 dollars each).

The logic here works on plain numbers and needs no network; the downloads are in
``tradingagents.dataflows.vendors.yahoo.picker_data``. Every recorded list is also appended to a ledger, so later we can see
which recipes turned up stocks that did well.
"""
