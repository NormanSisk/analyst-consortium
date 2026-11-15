"""Tests for the SEC Form 4 insider vendor (a local addition): reading a filing, summarizing, point-in-time serving and routing.

The filings are invented. Nothing reaches the network: the fetches are replaced, and a test that forgot to would be refused by the
suite's network block."""

import pytest

from tradingagents.agents import scorecard
from tradingagents.dataflows import router
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors import sec_edgar, sec_insiders as si

CIK = "0000051143"
AS_OF = "2026-11-06"


def tx(date="2026-10-01", code="S", shares=1000, price=50.0, after=5000, acquired="D", held="D", planned_ref="", security="Common Stock"):
    ref = f'<footnoteId id="{planned_ref}"/>' if planned_ref else ""
    price_xml = f"<transactionPricePerShare><value>{price}</value></transactionPricePerShare>" if price is not None else ""
    return f"""<nonDerivativeTransaction><securityTitle><value>{security}</value></securityTitle>
      <transactionDate><value>{date}</value></transactionDate>
      <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>{code}</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>{shares}</value>{ref}</transactionShares>{price_xml}
        <transactionAcquiredDisposedCode><value>{acquired}</value></transactionAcquiredDisposedCode></transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>{after}</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>{held}</value></directOrIndirectOwnership></ownershipNature>
    </nonDerivativeTransaction>"""


def form4(trades, owner="Roe Richard", issuer=CIK, officer="", director=False, ten=False, footnotes="", aff="", derivative="",
          extra_owner=""):
    title = f"<officerTitle>{officer}</officerTitle>" if officer else ""
    return f"""<?xml version="1.0"?><ownershipDocument><schemaVersion>X0609</schemaVersion><documentType>4</documentType>
      <periodOfReport>2026-10-01</periodOfReport>{aff}
      <issuer><issuerCik>{issuer}</issuerCik><issuerName>TEST CORP</issuerName><issuerTradingSymbol>TEST</issuerTradingSymbol></issuer>
      <reportingOwner><reportingOwnerId><rptOwnerCik>1</rptOwnerCik><rptOwnerName>{owner}</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship><isDirector>{'true' if director else 'false'}</isDirector>
          <isOfficer>{'true' if officer else 'false'}</isOfficer><isTenPercentOwner>{'true' if ten else 'false'}</isTenPercentOwner>{title}
        </reportingOwnerRelationship></reportingOwner>{extra_owner}
      <nonDerivativeTable>{''.join(trades)}</nonDerivativeTable><derivativeTable>{derivative}</derivativeTable>{footnotes}
    </ownershipDocument>"""


def parsed(xml, filed="2026-10-03", accession="0001-26-000001", amended=False):
    out = si.parse_form4(xml)
    out.update(accession=accession, filed=filed, amended=amended)
    return out


@pytest.mark.unit
class TestReadingOneFiling:
    def test_a_sale_is_read_with_its_insider_role_and_holdings(self):
        f = si.parse_form4(form4([tx()], owner="Smith John", officer="Chief Financial Officer"))
        assert f["issuer_cik"] == 51143 and f["owners"] == ["Smith John"] and f["relation"]["title"] == "Chief Financial Officer"
        t = f["trades"][0]
        assert (t["traded"], t["code"], t["shares"], t["price"], t["after"], t["held"], t["planned"]) == ("2026-10-01", "S", 1000.0, 50.0, 5000.0, "D", False)

    def test_a_rule_10b5_1_plan_is_found_in_the_footnote_the_trade_points_to_and_not_in_others(self):
        notes = '<footnotes><footnote id="F1">Sold under a Rule 10b5-1 trading plan adopted on March 1.</footnote><footnote id="F2">Held in a trust.</footnote></footnotes>'
        f = si.parse_form4(form4([tx(planned_ref="F1"), tx(planned_ref="F2"), tx()], footnotes=notes))
        assert [t["planned"] for t in f["trades"]] == [True, False, False]

    def test_the_header_checkbox_marks_every_trade_as_planned(self):
        f = si.parse_form4(form4([tx(), tx(code="P")], aff="<aff10b5One>1</aff10b5One>"))
        assert all(t["planned"] for t in f["trades"])

    def test_only_the_share_table_is_read_options_are_ignored(self):
        options = "<derivativeTransaction><securityTitle><value>Stock Option</value></securityTitle></derivativeTransaction>"
        assert len(si.parse_form4(form4([tx()], derivative=options))["trades"]) == 1

    def test_joint_filers_and_roles(self):
        extra = ("<reportingOwner><reportingOwnerId><rptOwnerName>Holdings LP</rptOwnerName></reportingOwnerId>"
                 "<reportingOwnerRelationship><isTenPercentOwner>true</isTenPercentOwner></reportingOwnerRelationship></reportingOwner>")
        f = si.parse_form4(form4([tx()], owner="Fund Manager", director=True, extra_owner=extra))
        assert f["owners"] == ["Fund Manager", "Holdings LP"] and f["relation"]["director"] and f["relation"]["ten_percent"]
        assert si._role(f["relation"]) == "Director"
        assert si._role({"title": "CEO", "director": True}) == "CEO" and si._role({"ten_percent": True}) == "10% owner"

    def test_a_missing_price_gives_no_value_and_unreadable_documents_are_refused(self):
        f = si.parse_form4(form4([tx(code="A", price=None)]))
        assert f["trades"][0]["price"] is None
        assert si.parse_form4("<html>not a filing</html>") is None and si.parse_form4("<ownershipDocument><unclosed>") is None
        assert si.parse_form4('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaa">]><ownershipDocument>&a;</ownershipDocument>') is None

    def test_the_raw_xml_file_is_the_listed_file_without_the_styling_folder(self):
        assert si._document("xslF345X06/primarydocument.xml") == "primarydocument.xml"
        assert si._document("wk-form4_1.xml") == "wk-form4_1.xml" and si._document("") == ""


def f4(trades, who="Roe Richard", filed="2026-10-03", accession="a1", **kw):
    return parsed(form4(trades, owner=who, **kw), filed=filed, accession=accession)


@pytest.mark.unit
class TestSummarizing:
    def test_trades_outside_the_window_are_left_out_and_an_amendment_replaces_the_original(self):
        original = f4([tx("2026-10-01"), tx("2025-01-01")], accession="a1")
        amended = parsed(form4([tx("2026-10-01")]), filed="2026-10-10", accession="a2", amended=True)
        trades = si.flatten([original, amended], "2026-05-10", AS_OF)
        assert len(trades) == 1 and trades[0]["amended"] is True and trades[0]["filed"] == "2026-10-10"

    def test_two_different_trades_by_one_insider_on_one_day_are_both_kept(self):
        trades = si.flatten([f4([tx(after=5000), tx(after=4000)])], "2026-05-10", AS_OF)
        assert len(trades) == 2

    def test_three_different_insiders_buying_within_a_month_make_a_cluster_and_two_do_not(self):
        def buys(*pairs):
            return [{"who": w, "traded": d} for w, d in pairs]

        assert si.cluster(buys(("A", "2026-09-01"), ("B", "2026-09-10"), ("C", "2026-09-25")))
        assert si.cluster(buys(("A", "2026-09-01"), ("B", "2026-09-10"), ("A", "2026-09-25"))) is None          # only two people
        assert si.cluster(buys(("A", "2026-06-01"), ("B", "2026-08-10"), ("C", "2026-09-25"))) is None          # too far apart
        found = si.cluster(buys(("A", "2026-06-01"), ("B", "2026-06-05"), ("C", "2026-06-09"), ("D", "2026-06-12"), ("E", "2026-10-01")))
        assert len({t["who"] for t in found}) == 4

    def test_the_newest_cluster_is_the_one_reported_even_when_an_older_one_is_bigger(self):
        older = [("A", "2026-06-01"), ("B", "2026-06-05"), ("C", "2026-06-09"), ("D", "2026-06-12")]
        newer = [("E", "2026-10-01"), ("F", "2026-10-05"), ("G", "2026-10-09")]
        found = si.cluster([{"who": w, "traded": d} for w, d in older + newer])
        assert {t["who"] for t in found} == {"E", "F", "G"} and found[-1]["traded"] == "2026-10-09"

    def test_the_report_counts_open_market_trades_the_live_window_net_planned_sales_and_routine_entries(self):
        notes = '<footnotes><footnote id="F1">10b5-1 plan</footnote></footnotes>'
        filings = [
            f4([tx("2026-10-20", "S", 2000, 100.0, planned_ref="F1"), tx("2026-10-20", "F", 500, 100.0), tx("2026-10-21", "M", 100, 10.0)],
               who="Seller One", officer="Chief Executive Officer", footnotes=notes, accession="b1", filed="2026-10-22"),
            f4([tx("2026-09-15", "P", 1000, 40.0), tx("2026-09-20", "P", 500, 41.0)], who="Buyer Two", director=True, accession="b2",
               filed="2026-09-22"),
            f4([tx("2026-06-01", "S", 100, 60.0)], who="Seller Three", accession="b3", filed="2026-06-03"),       # older than the live window
        ]
        text = si.build_report("TEST", filings, AS_OF, unread=0, total_entries=3, truncated=False)
        assert "- Open-market purchases: 2, about $60K by 1 insider, the latest 47 days ago." in text
        assert "- Open-market sales: 2, about $206K by 2 insiders, the latest 17 days ago." in text
        assert ("- Last 60 days (the live window): 2 purchases about $60K and 1 sale about $200K, net selling of $140K in dollars "
                "(order ignored; the Latest move line gives the direction).") in text
        assert "- Planned sales: 1 of 2 sales, about $200K, was made under a pre-arranged Rule 10b5-1 plan" in text
        assert "1 x shares withheld for taxes" in text and "1 x option or derivative exercise" in text
        assert "| 2026-10-20 | 17 | 2026-10-22 | Seller One | Chief Executive Officer | open-market sale (10b5-1 plan) | 2,000 | 100.00 | $200K | 5,000 |" in text
        assert "| 2026-09-15 | 52 | 2026-09-22 | Buyer Two | Director | open-market purchase | 1,000 | 40.00 | $40K | 5,000 |" in text
        assert "withheld for taxes" in text.split("| Traded")[0] and "| F " not in text                         # routine entries are not in the table
        assert "no run of 3 or more different insiders" in text

    def test_a_cluster_is_named_in_the_report(self):
        filings = [f4([tx("2026-10-01", "P", 100, 10.0)], who=name, accession=name) for name in ("Ann Lee", "Bo Chan", "Cy Park")]
        assert ("- Cluster: 3 different insiders bought on the open market within 30 days (2026-10-01 to 2026-10-01, the latest 36 days ago): "
                "Ann Lee, Bo Chan, Cy Park.") in si.build_report("TEST", filings, AS_OF, 0, 3, False)

    def test_a_cluster_counts_as_current_through_90_days_and_not_after(self):
        def report(day):
            return si.build_report("TEST", [f4([tx(day, "P", 100, 10.0)], who=name, accession=name) for name in ("Ann Lee", "Bo Chan", "Cy Park")],
                                   AS_OF, 0, 3, False)

        assert "- Cluster: 3 different insiders" in report("2026-08-08")                 # 90 days ago: still current
        stale = report("2026-08-07")                                                      # 91 days ago
        assert "- Cluster: none now. An older run of 3 different insiders buying within 30 days ended 91 days ago, too old to count" in stale
        assert "- Cluster: 3 different insiders" not in stale
        assert "none now" not in scorecard.build_scorecard({"get_insider_transactions": stale}) and \
            "Cluster" not in scorecard.build_scorecard({"get_insider_transactions": stale})

    def test_every_line_carries_its_age_and_the_live_window_leaves_out_older_purchases(self):
        filings = [f4([tx("2026-10-30", "P", 100, 10.0)], who="Ann Lee", accession="a"),         # 7 days ago
                   f4([tx("2026-08-30", "P", 400, 10.0)], who="Bo Chan", accession="b")]         # 68 days ago: context only
        text = si.build_report("TEST", filings, AS_OF, 0, 2, False)
        assert "- Open-market purchases: 2, about $5K by 2 insiders, the latest 7 days ago." in text
        assert "- Last 60 days (the live window): 1 purchase about $1K and 0 sales about $0, net buying of $1K in dollars" in text
        assert "| 2026-08-30 | 68 |" in text and "- Age matters: days are counted from the trade date." in text
        assert si._ago(0) == "today" and si._ago(1) == "1 day ago" and si._ago(2) == "2 days ago"

    # --- the latest move: the newest purchase or unplanned sale sets the direction, and a sale after one's own purchase reverses it ---

    def test_a_sale_after_the_same_insiders_purchase_is_the_latest_move_and_reverses_the_purchase(self):
        filings = [f4([tx("2026-09-07", "P", 1000, 100.0)], who="Alice Buyer", accession="a1", filed="2026-09-09"),        # 60 days ago
                   f4([tx("2026-10-07", "S", 100, 100.0)], who="Alice Buyer", accession="a2", filed="2026-10-09")]         # 30 days ago
        text = si.build_report("TEST", filings, AS_OF, 0, 2, False)
        assert "- Latest move: 1 unplanned sale by Alice Buyer on 2026-10-07 (30 days ago)." in text
        assert ("- Sold after buying: Alice Buyer (bought about $100K, last on 2026-09-07; sold about $10K after it, first on 2026-10-07, "
                "30 days ago). Each such insider's purchases before the sale are left out of the Cluster line.") in text
        assert "net buying of $90K in dollars (order ignored; the Latest move line gives the direction)" in text    # the total alone misleads
        c2 = next(line for line in scorecard.build_scorecard({"get_insider_transactions": text}).splitlines() if line.startswith("C2"))
        assert "Latest move: 1 unplanned sale by Alice Buyer on 2026-10-07 (30 days ago)" in c2 and "Sold after buying: Alice Buyer (bought" in c2

    def test_a_reversed_purchase_no_longer_counts_toward_a_cluster(self):
        buyers = [f4([tx("2026-10-01", "P", 100, 10.0)], who=name, accession=name) for name in ("Ann Lee", "Bo Chan", "Cy Park")]
        assert "- Cluster: 3 different insiders" in si.build_report("TEST", buyers, AS_OF, 0, 3, False)
        sold = [*buyers, f4([tx("2026-10-20", "S", 100, 10.0)], who="Bo Chan", accession="bo-sale")]
        text = si.build_report("TEST", sold, AS_OF, 0, 4, False)
        assert "- Sold after buying: Bo Chan" in text and "- Cluster: no run of 3 or more different insiders" in text

    def test_a_later_sale_by_a_different_insider_is_the_latest_move_but_reverses_nothing(self):
        buyers = [f4([tx("2026-10-01", "P", 100, 10.0)], who=name, accession=name) for name in ("Ann Lee", "Bo Chan", "Cy Park")]
        text = si.build_report("TEST", [*buyers, f4([tx("2026-10-20", "S", 100, 10.0)], who="Dee Fox", accession="dee")], AS_OF, 0, 4, False)
        assert "- Latest move: 1 unplanned sale by Dee Fox on 2026-10-20 (17 days ago)." in text
        assert "Sold after buying" not in text and "- Cluster: 3 different insiders" in text

    def test_a_planned_sale_is_not_the_latest_move_and_reverses_nothing(self):
        notes = '<footnotes><footnote id="F1">10b5-1 plan</footnote></footnotes>'
        filings = [f4([tx("2026-09-07", "P", 1000, 100.0)], who="Alice Buyer", accession="a1"),
                   f4([tx("2026-10-07", "S", 100, 100.0, planned_ref="F1")], who="Alice Buyer", footnotes=notes, accession="a2")]
        text = si.build_report("TEST", filings, AS_OF, 0, 2, False)
        assert "- Latest move: 1 purchase by Alice Buyer on 2026-09-07 (60 days ago)." in text and "Sold after buying" not in text
        planned_only = si.build_report("TEST", filings[1:], AS_OF, 0, 1, False)
        assert "- Latest move: none, no purchase or unplanned sale in this window." in planned_only
        assert "Latest move" not in scorecard.build_scorecard({"get_insider_transactions": planned_only})      # nothing to carry

    def test_a_purchase_after_the_same_insiders_sale_stands(self):
        filings = [f4([tx("2026-09-07", "S", 100, 100.0)], who="Alice Buyer", accession="a1"),
                   f4([tx("2026-10-07", "P", 100, 100.0)], who="Alice Buyer", accession="a2")]
        text = si.build_report("TEST", filings, AS_OF, 0, 2, False)
        assert "- Latest move: 1 purchase by Alice Buyer on 2026-10-07 (30 days ago)." in text and "Sold after buying" not in text

    def test_a_purchase_and_an_unplanned_sale_on_the_same_day_are_both_named(self):
        filings = [f4([tx("2026-10-20", "P", 100, 10.0)], who="Ann Lee", accession="a"),
                   f4([tx("2026-10-20", "S", 100, 10.0)], who="Bo Chan", accession="b")]
        assert "- Latest move: 1 purchase by Ann Lee and 1 unplanned sale by Bo Chan on 2026-10-20 (17 days ago)." in \
            si.build_report("TEST", filings, AS_OF, 0, 2, False)

    # --- a cluster that is one scheduled purchase program (many insiders at one identical price on one day) is flagged as routine ---

    def test_routine_batches_flags_purchases_filled_at_one_price_on_one_day_by_several_insiders(self):
        def trade(who, day, price):
            return {"who": who, "traded": day, "price": price}

        program = [trade(w, "2026-10-07", 79.39) for w in "ABC"] + [trade(w, "2026-11-06", 70.5) for w in "ABCD"]
        found = si.routine_batches(program)
        assert found["in_batches"] == 7 and found["total"] == 7
        assert found["days"] == [("2026-11-06", 70.5, 4), ("2026-10-07", 79.39, 3)]                # newest first
        assert si.routine_batches([trade(w, "2026-10-07", p) for w, p in (("A", 10.0), ("B", 10.2), ("C", 10.4))]) is None   # separate fills
        assert si.routine_batches([trade(w, "2026-10-07", 10.0) for w in "AB"] + [trade("C", "2026-10-07", 10.5)]) is None   # only 2 at one price
        # the share of purchases made in batches must reach 80 percent
        assert si.routine_batches([trade(w, "2026-10-07", 10.0) for w in "ABC"] + [trade("D", "2026-10-09", 11.0), trade("E", "2026-10-10", 12.0)]) is None
        assert si.routine_batches([trade(w, "2026-10-07", 10.0) for w in "ABCD"] + [trade("E", "2026-10-10", 12.0)]) is not None       # exactly 80 percent
        assert si.routine_batches([trade(w, "2026-10-07", None) for w in "ABC"]) is None             # a missing price shows nothing

    def test_the_report_notes_a_cluster_that_looks_like_a_purchase_program_and_the_scorecard_carries_it(self):
        program = [f4([tx("2026-10-01", "P", 100, 10.0)], who=name, accession=name) for name in ("Ann Lee", "Bo Chan", "Cy Park")]
        text = si.build_report("TEST", program, AS_OF, 0, 3, False)
        assert ("- Cluster note: likely routine. 3 of 3 purchases in this cluster were made on days when 3 or more insiders bought at exactly "
                "the same price (3 insiders at 10.00 on 2026-10-01), which is how a scheduled purchase program fills, not how separate "
                "decisions look.") in text
        c2 = next(line for line in scorecard.build_scorecard({"get_insider_transactions": text}).splitlines() if line.startswith("C2"))
        assert "Cluster note: likely routine. 3 of 3 purchases" in c2 and "Cluster: 3 different insiders" in c2
        separate = [f4([tx("2026-10-01", "P", 100, price)], who=name, accession=name)
                    for name, price in (("Ann Lee", 10.0), ("Bo Chan", 10.4), ("Cy Park", 9.8))]
        assert "Cluster note" not in si.build_report("TEST", separate, AS_OF, 0, 3, False)
        stale = si.build_report("TEST", [f4([tx("2026-08-07", "P", 100, 10.0)], who=n, accession=n) for n in ("Ann Lee", "Bo Chan", "Cy Park")],
                                AS_OF, 0, 3, False)
        assert "Cluster note" not in stale                                                          # an old run is not a current cluster anyway

    # --- purchases are graded by the share they add to the buyer's own holding, so a small purchase still counts for something ---

    def test_live_purchases_are_graded_large_modest_or_token_by_the_share_added_to_the_holding(self):
        filings = [f4([tx("2026-10-30", "P", 500, 10.0, after=4000)], who="Ann Lee", accession="a"),        # 500 on 3,500: +14.3 percent
                   f4([tx("2026-10-20", "P", 100, 10.5, after=5000)], who="Bo Chan", accession="b"),        # 100 on 4,900: +2.0 percent
                   f4([tx("2026-10-25", "P", 30, 9.9, after=5000)], who="Cy Park", accession="c")]          # 30 on 4,970: +0.6 percent
        text = si.build_report("TEST", filings, AS_OF, 0, 3, False)
        assert ("- Purchase size: 3 purchases in the last 60 days outside batches and reversals (an insider's fills on one day count once), "
                "graded by the share added to the buyer's holding: 1 large (10% or more of the buyer's holding), 1 modest (1% to 10%), "
                "1 token (under 1%). Largest: Ann Lee added 14.3% of the holding on 2026-10-30 (7 days ago).") in text
        assert "| Owned after | Change that day |" in text and "| 4,000 | +14.3% |" in text and "| 5,000 | +0.6% |" in text
        c2 = next(line for line in scorecard.build_scorecard({"get_insider_transactions": text}).splitlines() if line.startswith("C2"))
        assert "Purchase size: 3 purchases in the last 60 days" in c2

    def test_one_insiders_fills_on_one_day_are_added_up_and_counted_once(self):
        fills = f4([tx("2026-10-30", "P", 100, 10.0, after=1100), tx("2026-10-30", "P", 100, 10.1, after=1200)], who="Dee Fox", accession="d")
        text = si.build_report("TEST", [fills], AS_OF, 0, 1, False)
        assert "1 purchase in the last 60 days" in text and "1 large" in text         # 200 on 1,000 is +20 percent; each fill alone is under 10
        assert "Largest: Dee Fox added 20.0% of the holding" in text

    def test_a_first_purchase_is_a_new_position_and_an_unknown_holding_is_not_graded_by_size(self):
        first = si.build_report("TEST", [f4([tx("2026-10-30", "P", 400, 10.0, after=400)], who="Eve Roe", accession="e")], AS_OF, 0, 1, False)
        assert "1 large" in first and "added a new position (nothing owned before)" in first and "| new position |" in first
        unknown = si.build_report("TEST", [f4([tx("2026-10-30", "P", 400, 10.0, after="")], who="Eve Roe", accession="e")], AS_OF, 0, 1, False)
        assert "1 with the holding unknown" in unknown and "Largest:" not in unknown and "| n/a |" in unknown

    def test_a_sale_shows_the_share_of_the_holding_sold(self):
        text = si.build_report("TEST", [f4([tx("2026-10-30", "S", 200, 10.0, after=800)], who="Gus Hall", accession="g")], AS_OF, 0, 1, False)
        assert "| 800 | -20.0% |" in text and "Purchase size" not in text                      # no purchases: no size line

    def test_batch_reversed_and_old_purchases_are_left_out_of_the_grades(self):
        batch = [f4([tx("2026-10-30", "P", 50, 10.0, after=5000)], who=name, accession=name) for name in ("Ann Lee", "Bo Chan", "Cy Park")]
        assert "- Purchase size: none. No purchase in the last 60 days stands outside a batch or a reversal." in \
            si.build_report("TEST", batch, AS_OF, 0, 3, False)
        flipped = [f4([tx("2026-09-20", "P", 500, 10.0, after=4000)], who="Ann Lee", accession="a1"),
                   f4([tx("2026-10-20", "S", 10, 10.0, after=3990)], who="Ann Lee", accession="a2")]
        assert "- Purchase size: none." in si.build_report("TEST", flipped, AS_OF, 0, 2, False)
        old = si.build_report("TEST", [f4([tx("2026-08-30", "P", 500, 10.0, after=4000)], who="Ann Lee", accession="a")], AS_OF, 0, 1, False)
        assert "- Purchase size: none." in old                                                  # 68 days ago is context only

    def test_holdings_change_is_worked_out_per_insider_and_day(self):
        def line(who, day, code, shares, after):
            return {"who": who, "traded": day, "code": code, "shares": shares, "after": after, "security": "Common Stock", "held": "D"}

        trades = [line("A", "2026-10-01", "P", 100, 1100), line("A", "2026-10-01", "P", 100, 1200), line("B", "2026-10-01", "P", 50, 50),
                  line("C", "2026-10-01", "P", 10, None), line("D", "2026-10-01", "S", 300, 700), line("E", "2026-10-01", "P", 500, 100),
                  {"who": "F", "traded": "2026-10-01", "code": "F", "shares": 5, "after": 5, "security": "Common Stock", "held": "D"}]
        si._add_holdings_change(trades)
        assert [t.get("change") for t in trades] == [pytest.approx(20.0), pytest.approx(20.0), float("inf"), None, pytest.approx(30.0), None, None]

    def test_reversals_splits_standing_purchases_from_reversed_ones(self):
        def trade(who, day, planned=False, value=1000.0):
            return {"who": who, "traded": day, "planned": planned, "value": value}

        buys = [trade("A", "2026-09-01"), trade("A", "2026-10-01"), trade("B", "2026-09-01"), trade("C", "2026-10-15")]
        sales = [trade("A", "2026-09-20"), trade("B", "2026-10-20", planned=True), trade("C", "2026-10-15")]    # C: same day is not "after"
        standing, found = si.reversals(buys, sales)
        assert sorted((t["who"], t["traded"]) for t in standing) == [("A", "2026-10-01"), ("B", "2026-09-01"), ("C", "2026-10-15")]
        assert list(found) == ["A"] and found["A"]["bought_last"] == "2026-09-01" and found["A"]["sold_first"] == "2026-09-20"

    def test_the_two_summary_bullets_have_the_form_the_scorecard_reads(self):
        text = si.build_report("TEST", [f4([tx("2026-10-20", "S", 10, 100.0), tx("2026-10-21", "P", 20, 50.0)])], AS_OF, 0, 1, False)
        card = scorecard.build_scorecard({"get_insider_transactions": text})
        assert "C2. Unresolved signals" in card and "Open-market purchases: 1, about $1K by 1 insider" in card
        assert "Open-market sales: 1, about $1K by 1 insider" in card

    def test_the_scorecard_also_carries_planned_sales_and_a_buying_cluster_and_leaves_out_empty_lines(self):
        notes = '<footnotes><footnote id="F1">10b5-1 plan</footnote></footnotes>'
        seller = f4([tx("2026-10-20", "S", 100, 10.0, planned_ref="F1")], who="Seller", footnotes=notes, accession="s1")
        buyers = [f4([tx("2026-10-05", "P", 100, 10.0)], who=name, accession=name) for name in ("Ann Lee", "Bo Chan", "Cy Park")]
        c2 = next(line for line in scorecard.build_scorecard({"get_insider_transactions": si.build_report(
            "TEST", [seller, *buyers], AS_OF, 0, 4, False)}).splitlines() if line.startswith("C2"))
        assert "Planned sales: 1 of 1 sale, about $1K, was made under a pre-arranged Rule 10b5-1 plan" in c2 and "which sets the date" not in c2
        assert ("Cluster: 3 different insiders bought on the open market within 30 days (2026-10-05 to 2026-10-05, the latest 32 days ago): "
                "Ann Lee, Bo Chan, Cy Park") in c2
        assert "not evidence of deterioration, or of improvement, until confirmed" in c2
        # no cluster: the negative line is not carried; only routine entries: nothing is carried at all
        no_cluster = scorecard.build_scorecard({"get_insider_transactions": si.build_report("TEST", [seller], AS_OF, 0, 1, False)})
        assert "no run of" not in no_cluster and "Cluster" not in no_cluster and "Planned sales" in no_cluster
        routine = si.build_report("TEST", [f4([tx("2026-10-20", "F", 50, 10.0)])], AS_OF, 0, 1, False)
        assert "Open-market purchases: 0" in routine and scorecard.build_scorecard({"get_insider_transactions": routine}) == ""

    def test_a_window_with_no_trades_says_so_and_notes_what_could_not_be_read(self):
        text = si.build_report("TEST", [], AS_OF, unread=2, total_entries=5, truncated=False)
        assert "No Form 4 trades in this window." in text and "2 of 5 filings could not be read" in text

    def test_notes_say_when_the_newest_filings_only_were_read_or_some_were_about_other_companies(self):
        text = si.build_report("TEST", [f4([tx()])], AS_OF, unread=1, total_entries=120, truncated=True, elsewhere=3)
        assert "Only the newest 80 of 120 Form 4 filings" in text and "1 filing could not be read and is left out" in text
        assert "3 Form 4 filings in this company's filing list are about other companies" in text

    def test_money_and_plurals(self):
        assert [si._money(x) for x in (None, 12, 4500, 2_300_000, 7.2e9)] == ["n/a", "$12", "$4K", "$2.3M", "$7.2B"]
        assert si._count(1, "sale", "sales") == "1 sale" and si._count(2, "sale", "sales") == "2 sales"


def entry(accession, filed, form="4", doc="xslF345X06/primarydocument.xml"):
    return {"form": form, "filingDate": filed, "accessionNumber": accession, "primaryDocument": doc}


class Edgar:
    """A stand-in for EDGAR: the issuer's filing list and each filing's XML, with a count of what was fetched."""

    def __init__(self, entries, documents):
        self.entries, self.documents, self.fetched = entries, documents, []

    def submissions(self, url, name):
        keys = ("form", "filingDate", "accessionNumber", "primaryDocument")
        return {"filings": {"recent": {k: [e[k] for e in self.entries] for k in keys}}}

    def text(self, url):
        self.fetched.append(url)
        for accession, xml in self.documents.items():
            if accession.replace("-", "") in url:
                if isinstance(xml, Exception):
                    raise xml
                return xml
        raise VendorUnavailableError("not found")


@pytest.fixture
def edgar(tmp_path, monkeypatch):
    holder = {}

    def make(entries, documents, cik=CIK):
        e = Edgar(entries, documents)
        holder["edgar"] = e
        monkeypatch.setattr(si, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
        monkeypatch.setattr(sec_edgar, "cik_for", lambda symbol: cik if symbol == "TEST" else None)
        monkeypatch.setattr(sec_edgar, "_cached_json", e.submissions)
        monkeypatch.setattr(si, "_fetch_text", e.text)
        monkeypatch.setattr(si.time, "sleep", lambda s: None)
        return e

    return make


@pytest.mark.unit
class TestServingAReport:
    def test_only_form_4_filings_in_the_window_and_filed_by_the_run_date_are_read(self, edgar):
        e = edgar(
            [entry("0000000001-26-000001", "2026-10-03"), entry("0000000001-26-000002", "2026-11-20"),      # filed after the run date
             entry("0000000001-26-000003", "2025-01-02"), entry("0000000001-26-000004", "2026-10-04", form="8-K"),
             entry("0000000001-26-000005", "2026-10-05", form="4/A")],
            {"0000000001-26-000001": form4([tx("2026-10-01")]), "0000000001-26-000005": form4([tx("2026-10-02", "P", 10, 5.0)])})
        text = si.get_insider_transactions("TEST", AS_OF)
        assert len(e.fetched) == 2 and "| 2026-10-01 |" in text and "| 2026-10-02 |" in text
        assert "filed by 2026-11-06" in text and "2026-11-20" not in text

    def test_a_past_run_is_served_and_not_withheld(self, edgar):
        edgar([entry("0000000001-26-000001", "2026-09-04")], {"0000000001-26-000001": form4([tx("2026-09-02")])})
        text = si.get_insider_transactions("TEST", "2026-09-10")
        assert "withheld" not in text and "| 2026-09-02 | 8 | 2026-09-04 |" in text

    def test_a_filing_is_fetched_once_and_then_read_from_the_cache(self, edgar):
        e = edgar([entry("0000000001-26-000001", "2026-10-03")], {"0000000001-26-000001": form4([tx()])})
        first = si.get_insider_transactions("TEST", AS_OF)
        second = si.get_insider_transactions("TEST", AS_OF)
        assert first == second and len(e.fetched) == 1

    def test_a_filing_about_another_company_is_left_out_with_a_note(self, edgar):
        edgar([entry("0000000001-26-000001", "2026-10-03"), entry("0000000001-26-000002", "2026-10-04")],
              {"0000000001-26-000001": form4([tx("2026-10-01")]), "0000000001-26-000002": form4([tx("2026-10-02", "P")], issuer="0000999999")})
        text = si.get_insider_transactions("TEST", AS_OF)
        assert "| 2026-10-01 |" in text and "| 2026-10-02 |" not in text and "1 Form 4 filing in this company's filing list is about" in text

    def test_only_the_newest_filings_up_to_the_cap_are_read(self, edgar, monkeypatch):
        monkeypatch.setattr(si, "MAX_FILINGS", 3)
        entries = [entry(f"0000000001-26-{i:06d}", f"2026-10-{i:02d}") for i in range(1, 8)]
        e = edgar(entries, {x["accessionNumber"]: form4([tx(f"2026-10-{i + 1:02d}")]) for i, x in enumerate(entries)})
        text = si.get_insider_transactions("TEST", AS_OF)
        assert len(e.fetched) == 3 and "Only the newest 3 of 7 Form 4 filings" in text

    def test_one_filing_that_cannot_be_read_is_noted_and_the_rest_are_served(self, edgar):
        edgar([entry("0000000001-26-000001", "2026-10-03"), entry("0000000001-26-000002", "2026-10-04")],
              {"0000000001-26-000001": form4([tx()]), "0000000001-26-000002": VendorUnavailableError("slow")})
        text = si.get_insider_transactions("TEST", AS_OF)
        assert "| 2026-10-01 |" in text and "1 filing could not be read" in text

    def test_when_none_can_be_read_the_vendor_is_unavailable_so_the_router_moves_on(self, edgar):
        edgar([entry(f"0000000001-26-{i:06d}", "2026-10-03") for i in range(1, 4)],
              {f"0000000001-26-{i:06d}": VendorUnavailableError("down") for i in range(1, 4)})
        with pytest.raises(VendorUnavailableError, match="none of the Form 4"):
            si.get_insider_transactions("TEST", AS_OF)

    def test_it_stops_asking_after_a_few_failures_when_edgar_is_not_answering(self, edgar, monkeypatch):
        monkeypatch.setattr(si, "WORKERS", 1)
        monkeypatch.setattr(si, "MAX_FAILURES", 2)
        entries = [entry(f"0000000001-26-{i:06d}", "2026-10-03") for i in range(1, 11)]
        e = edgar(entries, {x["accessionNumber"]: VendorUnavailableError("down") for x in entries})
        with pytest.raises(VendorUnavailableError):
            si.get_insider_transactions("TEST", AS_OF)
        assert len(e.fetched) == 2

    def test_a_company_that_is_not_an_sec_filer_is_reported_as_such(self, edgar):
        edgar([], {}, cik=None)
        with pytest.raises(NoMarketDataError, match="not a US SEC filer"):
            si.get_insider_transactions("0700.HK", AS_OF)

    def test_no_form_4s_at_all_gives_a_plain_report(self, edgar):
        edgar([entry("0000000001-26-000009", "2026-10-03", form="8-K")], {})
        assert "No Form 4 trades in this window." in si.get_insider_transactions("TEST", AS_OF)


class _Response:
    def __init__(self, status=200, text="<x/>", content=None):
        self.status_code, self.text = status, text
        self.content = content if content is not None else text.encode()


@pytest.mark.unit
class TestTheRequests:
    def test_the_sec_is_told_who_is_asking_and_requests_are_spaced(self, monkeypatch):
        seen = []
        monkeypatch.setattr(si.requests, "get", lambda url, headers, timeout: seen.append(headers) or _Response())
        monkeypatch.setattr(sec_edgar, "_user_agent", lambda: "Test Agent (test@example.com)")
        monkeypatch.setattr(si, "_wait_turn", lambda: None)
        assert si._fetch_text("https://www.sec.gov/Archives/x") == "<x/>" and seen == [{"User-Agent": "Test Agent (test@example.com)"}]

    def test_a_rate_limit_is_retried_and_then_given_up_on(self, monkeypatch):
        answers = [_Response(429), _Response(200, "ok")]
        monkeypatch.setattr(si.requests, "get", lambda *a, **k: answers.pop(0))
        monkeypatch.setattr(si, "_wait_turn", lambda: None)
        monkeypatch.setattr(si.time, "sleep", lambda s: None)
        assert si._fetch_text("u") == "ok"
        monkeypatch.setattr(si.requests, "get", lambda *a, **k: _Response(503))
        with pytest.raises(VendorUnavailableError, match="did not return a Form 4"):
            si._fetch_text("u")

    def test_a_missing_page_is_not_retried_and_an_oversize_one_is_refused(self, monkeypatch):
        calls = []
        monkeypatch.setattr(si.requests, "get", lambda *a, **k: calls.append(1) or _Response(404))
        monkeypatch.setattr(si, "_wait_turn", lambda: None)
        with pytest.raises(VendorUnavailableError):
            si._fetch_text("u")
        assert len(calls) == 1
        monkeypatch.setattr(si.requests, "get", lambda *a, **k: _Response(200, "x", content=b"x" * (si.MAX_BYTES + 1)))
        with pytest.raises(VendorUnavailableError, match="larger than any real one"):
            si._fetch_text("u")

    def test_a_connection_error_is_reported_as_unavailable(self, monkeypatch):
        def boom(*a, **k):
            raise si.requests.ConnectionError("down")

        monkeypatch.setattr(si.requests, "get", boom)
        monkeypatch.setattr(si, "_wait_turn", lambda: None)
        monkeypatch.setattr(si.time, "sleep", lambda s: None)
        with pytest.raises(VendorUnavailableError):
            si._fetch_text("u")


@pytest.mark.unit
class TestRouting:
    def test_the_vendor_is_registered_and_first_in_the_default_chain(self):
        from tradingagents.default_config import build_default_config

        assert router.VENDOR_METHODS["get_insider_transactions"]["sec_edgar"] is si.get_insider_transactions
        assert build_default_config()["tool_vendors"]["get_insider_transactions"] == "sec_edgar,quiver,yfinance"

    def test_when_edgar_cannot_answer_the_router_falls_through_to_the_next_vendor(self, monkeypatch):
        def down(*a, **k):
            raise VendorUnavailableError("EDGAR is down")

        monkeypatch.setitem(router.VENDOR_METHODS["get_insider_transactions"], "sec_edgar", down)
        monkeypatch.setitem(router.VENDOR_METHODS["get_insider_transactions"], "quiver", down)
        monkeypatch.setitem(router.VENDOR_METHODS["get_insider_transactions"], "yfinance", lambda *a, **k: "yahoo rows")
        assert router.route_to_vendor("get_insider_transactions", "AAPL", None) == "yahoo rows"
