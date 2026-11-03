#!/usr/bin/env python3
"""
Eight Pillars of Fundamental Valuation, scored from SEC EDGAR company facts.

The pillars and their thresholds:

  1. 5-Year P/E                      < 22.5
  2. ROIC                            >= 9%   (NOPAT / (equity + debt - cash))
  3. Share count trend               decreasing over 5 years
  4. Free cash flow growth           growing over 5 years
  5. Net income growth               growing over 5 years
  6. Revenue growth                  growing over 5 years
  7. LT liabilities / 5-yr avg FCF   < 5
  8. Price to FCF                    < 22.5

> [!warning] Pillar 1 has two common definitions
> This module divides **current market cap by five-year average net income**.
> The other common method averages the five historical fiscal-year-end P/E
> ratios, which needs price history EDGAR does not carry. The two can disagree
> on the verdict: ADBE scores ~19x here (pass) against ~42x by the historical
> method (fail). Neither is wrong; they answer different questions. The method
> is printed with the number so the difference is always visible.

Never invents a figure: a concept that cannot be found is reported as "not
recorded" and its pillar scores as unknown rather than failing silently.

Stdlib only.

Usage:
    python3 pillars.py ADBE
    python3 pillars.py ADBE --telegram
"""

import json
from datetime import date
import sys

import cache
import dilution

SEC_FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

PE_MAX = 22.5
PFCF_MAX = 22.5
ROIC_MIN = 9.0
LTL_FCF_MAX = 5.0
# Fewer scored pillars than this and no score is shown -- see `insufficient`.
MIN_SCORED = 6
YEARS = 5

# XBRL tag names vary by filer, so each concept is a fallback chain tried in
# order. The tag actually used is recorded and printed -- a silently wrong tag
# is the main way this class of tool reports confident nonsense.
TAGS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenues",
                "RevenueFromContractWithCustomerIncludingAssessedTax",
                "SalesRevenueNet"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets"],
    "shares": ["WeightedAverageNumberOfDilutedSharesOutstanding",
               "WeightedAverageNumberOfSharesOutstandingBasic",
               "CommonStockSharesOutstanding"],
    "liabilities": ["Liabilities"],
    "liabilities_current": ["LiabilitiesCurrent"],
    "liabilities_noncurrent": ["LiabilitiesNoncurrent"],
    "equity": ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "debt": ["LongTermDebt", "LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "short_investments": ["ShortTermInvestments", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
    "op_income": ["OperatingIncomeLoss"],
    "pretax": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
               "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "tax": ["IncomeTaxExpenseBenefit"],
}


def fy_series(facts, concept):
    """
    Annual (10-K, full-year) values for the first tag that resolves.

    Returns ({fiscal_year: value}, tag_used).

    > [!warning] `fy` is the filing's fiscal year, not the data point's period
    > A single 10-K carries the current year plus two comparatives, and EDGAR
    > tags all three with the *filing's* `fy`. ADBE's 2025 10-K yields three
    > rows tagged `fy=2025` holding FY2023, FY2024 and FY2025 revenue. Keying on
    > `fy` silently mislabels the series by two years -- it reads as plausible
    > data and is wrong. The period is keyed on `end` instead, and flow concepts
    > are restricted to spans of roughly a year so quarterly rows cannot leak in.

    The fiscal year is labelled by the calendar year its period ends in. Where
    the same year appears twice (an original and a restatement), the most
    recently filed figure wins.
    """
    us = facts.get("us-gaap", {})
    # Candidates are ranked by how recent their data is, not by their order in
    # the list. A filer can abandon a tag mid-life: NVDA stopped reporting
    # PaymentsToAcquirePropertyPlantAndEquipment after 2012 and moved to
    # PaymentsToAcquireProductiveAssets. Taking the first tag with *any* rows
    # valued NVDA on its 2012 cash flows and produced $1/share against a $227
    # price. Most recent year wins; more years breaks the tie.
    ranked = []
    for tag in TAGS[concept]:
        if tag not in us:
            continue
        best = {}
        for unit_rows in us[tag]["units"].values():
            for row in unit_rows:
                if row.get("form") not in ("10-K", "10-K/A"):
                    continue
                end = row.get("end")
                if not end:
                    continue
                start = row.get("start")
                if start:
                    try:
                        days = (date.fromisoformat(end) - date.fromisoformat(start)).days
                    except ValueError:
                        continue
                    if not 340 <= days <= 400:
                        continue          # not an annual period
                try:
                    year = date.fromisoformat(end).year
                except ValueError:
                    continue
                filed = row.get("filed", "")
                if year not in best or filed >= best[year][1]:
                    best[year] = (row["val"], filed)
        if best:
            series = {y: v for y, (v, _) in best.items()}
            ranked.append(((max(series), len(series)), series, tag))
    if not ranked:
        return {}, None
    ranked.sort(key=lambda r: r[0], reverse=True)
    return ranked[0][1], ranked[0][2]


def load_facts(ticker):
    """
    Fetch one filer's annual series for every concept in TAGS.

    Shared by the pillars scorecard and the valuation models so both read the
    same numbers from the same filings -- two modules pulling EDGAR separately
    is how the scorecard and the valuation end up quietly disagreeing.

    Returns {"name", "cik", "series", "tags"} or {"error"}.
    """
    t = ticker.upper().strip().lstrip("$")
    cik_map = dilution.load_cik_map()
    cik = cik_map.get(t) or cik_map.get(t.replace(".", "-"))
    if not cik:
        return {"error": f"{t} not found in the SEC ticker list."}
    # Filings only change when a new one lands, so a day-old copy is the same
    # copy. This is also what stops /price fetching the subject twice.
    try:
        raw = cache.fetch(SEC_FACTS.format(cik=cik), cache.TTL_FILINGS,
                          fetcher=dilution._get)
    except Exception:
        raw = None
    if not raw:
        return {"error": f"SEC filing data unavailable for {t}."}
    try:
        doc = json.loads(raw)
    except ValueError:
        return {"error": f"SEC returned unreadable data for {t}."}
    facts = doc.get("facts", {})
    series, tags = {}, {}
    for concept in TAGS:
        series[concept], tags[concept] = fy_series(facts, concept)
    return {"name": doc.get("entityName", t), "cik": cik,
            "series": series, "tags": tags}


def industry(ticker):
    """(SIC code, description) for a filer, from the submissions API."""
    t = ticker.upper().strip().lstrip("$")
    cik_map = dilution.load_cik_map()
    cik = cik_map.get(t) or cik_map.get(t.replace(".", "-"))
    if not cik:
        return None, None
    try:
        raw = cache.fetch(f"https://data.sec.gov/submissions/CIK{cik}.json",
                          cache.TTL_PROFILE, fetcher=dilution._get)
    except Exception:
        raw = None
    if not raw:
        return None, None
    try:
        d = json.loads(raw)
    except ValueError:
        return None, None
    return d.get("sic"), d.get("sicDescription")


def last_n(series, n=YEARS):
    """The most recent n fiscal years, oldest first."""
    return [v for _, v in sorted(series.items())[-n:]]


def grew(vals):
    """Grown over the window: last above first, on at least three data points."""
    return len(vals) >= 3 and vals[-1] > vals[0]


def pct(a, b):
    return None if not b else (a - b) / abs(b) * 100.0


class Pillar:
    def __init__(self, n, name, target, value, passed, note):
        self.n, self.name, self.target = n, name, target
        self.value, self.passed, self.note = value, passed, note

    @property
    def mark(self):
        return "❓" if self.passed is None else ("✅" if self.passed else "❌")


def analyse(ticker, quote=None):
    """
    Score one ticker. `quote` supplies current price and share count; without it
    the price-based pillars (1 and 8) report as unknown rather than guessing.
    """
    # SEC writes class shares with a dash (BRK-B), quote feeds with a dot.
    ticker = ticker.upper().strip().lstrip("$")
    cik_map = dilution.load_cik_map()
    cik = cik_map.get(ticker) or cik_map.get(ticker.replace(".", "-"))
    if not cik:
        return {"error": f"{ticker} not found in the SEC ticker list."}

    raw = dilution._get(SEC_FACTS.format(cik=cik))
    if not raw:
        return {"error": f"SEC filing data unavailable for {ticker}."}
    try:
        doc = json.loads(raw)
    except ValueError:
        return {"error": f"SEC returned unreadable data for {ticker}."}

    facts = doc.get("facts", {})
    name = doc.get("entityName", ticker)
    tags_used, S = {}, {}
    for concept in TAGS:
        S[concept], tags_used[concept] = fy_series(facts, concept)

    rev, ni = last_n(S["revenue"]), last_n(S["net_income"])
    ocf, capex = last_n(S["ocf"]), last_n(S["capex"])
    shares = last_n(S["shares"])

    # FCF needs both legs from the same years, so align on the overlap.
    fcf_years = sorted(set(S["ocf"]) & set(S["capex"]))[-YEARS:]
    fcf = [S["ocf"][y] - S["capex"][y] for y in fcf_years]
    avg_fcf = sum(fcf) / len(fcf) if fcf else None

    latest = lambda k: (sorted(S[k].items())[-1][1] if S[k] else None)
    equity, debt = latest("equity"), latest("debt") or 0
    cash = (latest("cash") or 0) + (latest("short_investments") or 0)
    op_inc, pretax, tax = latest("op_income"), latest("pretax"), latest("tax")

    price = (quote or {}).get("price")
    shares_out = (quote or {}).get("shares") or (shares[-1] if shares else None)
    mcap = price * shares_out if (price and shares_out) else None

    P = []

    # 1 -- 5-year P/E: market cap over five-year average net income. See the module warning.
    avg_ni = sum(ni) / len(ni) if ni else None
    if mcap and avg_ni and avg_ni > 0:
        pe = mcap / avg_ni
        P.append(Pillar(1, "5-Year P/E", f"< {PE_MAX}", f"{pe:.1f}x", pe < PE_MAX,
                        f"market cap / {len(ni)}-yr avg net income "
                        f"(${avg_ni/1e9:.2f}B)"))
    else:
        P.append(Pillar(1, "5-Year P/E", f"< {PE_MAX}", "not recorded", None,
                        "needs price and positive average earnings"))

    # 2 -- ROIC on the same basis as the hand-written cards.
    if op_inc and pretax and tax is not None and equity:
        tax_rate = tax / pretax if pretax else 0.21
        nopat = op_inc * (1 - tax_rate)
        ic = equity + debt - cash
        if ic > 0:
            roic = nopat / ic * 100
            P.append(Pillar(2, "ROIC", f">= {ROIC_MIN}%", f"{roic:.1f}%",
                            roic >= ROIC_MIN,
                            f"NOPAT ${nopat/1e9:.2f}B / invested capital "
                            f"${ic/1e9:.2f}B (equity + debt − cash)"))
        else:
            P.append(Pillar(2, "ROIC", f">= {ROIC_MIN}%", "not meaningful", None,
                            "invested capital is negative"))
    else:
        P.append(Pillar(2, "ROIC", f">= {ROIC_MIN}%", "not recorded", None,
                        "operating income or tax not found"))

    # 3 -- share count
    if len(shares) >= 3:
        P.append(Pillar(3, "Share count", "decreasing",
                        f"{shares[0]/1e6:.0f}M → {shares[-1]/1e6:.0f}M",
                        shares[-1] < shares[0],
                        f"{pct(shares[-1], shares[0]):+.1f}% over {len(shares)} years"))
    else:
        P.append(Pillar(3, "Share count", "decreasing", "not recorded", None, ""))

    # 4, 5, 6 -- growth pillars
    for n, label, vals in ((4, "FCF growth", fcf), (5, "Net income growth", ni),
                           (6, "Revenue growth", rev)):
        if len(vals) >= 3:
            P.append(Pillar(n, label, "growing",
                            f"${vals[0]/1e9:.2f}B → ${vals[-1]/1e9:.2f}B",
                            grew(vals),
                            f"{pct(vals[-1], vals[0]):+.0f}% over {len(vals)} years"))
        else:
            P.append(Pillar(n, label, "growing", "not recorded", None, ""))

    # 7 -- long-term liabilities against average FCF
    ltl = latest("liabilities_noncurrent")
    basis = "LiabilitiesNoncurrent"
    if ltl is None and latest("liabilities") and latest("liabilities_current"):
        ltl = latest("liabilities") - latest("liabilities_current")
        basis = "total − current liabilities"
    if ltl is not None and avg_fcf and avg_fcf > 0:
        r = ltl / avg_fcf
        P.append(Pillar(7, "LT liabilities / avg FCF", f"< {LTL_FCF_MAX}",
                        f"{r:.2f}", r < LTL_FCF_MAX,
                        f"${ltl/1e9:.2f}B ({basis}) / ${avg_fcf/1e9:.2f}B avg FCF"))
    else:
        P.append(Pillar(7, "LT liabilities / avg FCF", f"< {LTL_FCF_MAX}",
                        "not recorded", None, "liabilities or FCF unavailable"))

    # 8 -- price to free cash flow
    if mcap and fcf and fcf[-1] > 0:
        pfcf = mcap / fcf[-1]
        P.append(Pillar(8, "Price / FCF", f"< {PFCF_MAX}", f"{pfcf:.1f}",
                        pfcf < PFCF_MAX,
                        f"${mcap/1e9:.1f}B market cap / ${fcf[-1]/1e9:.2f}B FCF"))
    else:
        P.append(Pillar(8, "Price / FCF", f"< {PFCF_MAX}", "not recorded", None,
                        "needs price and positive FCF"))

    scored = [p for p in P if p.passed is not None]

    # A partial card must never read as a score. JPM scores 4 of 4 because banks
    # report no capex, so the FCF pillars drop out silently -- printed as "4/4"
    # that looks like a perfect result for a company the framework cannot judge.
    # Below the floor the caller shows a warning instead of a score.
    insufficient = len(scored) < MIN_SCORED
    reason = None
    if insufficient:
        missing = [p.name for p in P if p.passed is None]
        if not S["revenue"] and not S["net_income"]:
            reason = (f"No annual (10-K) figures are filed under CIK {cik}. "
                      f"A company that has re-registered keeps its history under "
                      f"its former CIK, which this lookup does not follow.")
        else:
            reason = ("Too few pillars could be computed from the filings: "
                      + ", ".join(missing) + ". Banks and insurers report no "
                      "capex or free cash flow, so this framework does not "
                      "apply to them.")

    return {
        "ticker": ticker, "name": name, "cik": cik, "pillars": P,
        "passed": sum(1 for p in P if p.passed),
        "scored": len(scored), "unknown": len(P) - len(scored),
        "insufficient": insufficient, "reason": reason,
        "price": price, "mcap": mcap,
        "fy_years": sorted(S["net_income"])[-YEARS:] if S["net_income"] else [],
        "tags_used": tags_used,
    }


def markdown(r):
    if "error" in r:
        return r["error"]
    L = [f"# {r['name']} ({r['ticker']}) — 8 Pillars", ""]
    if r["price"]:
        L.append(f"**Price:** ${r['price']:,.2f} · **Market cap:** "
                 f"${r['mcap']/1e9:,.1f}B · Fiscal years {r['fy_years'][0]}–"
                 f"{r['fy_years'][-1]}" if r["fy_years"] else "")
    L += ["", "| # | Pillar | Target | Value | | Notes |", "|---|---|---|---|:-:|---|"]
    for p in r["pillars"]:
        L.append(f"| {p.n} | {p.name} | {p.target} | {p.value} | {p.mark} | {p.note} |")
    if r.get("insufficient"):
        L += ["", "> [!warning] Not scored", f"> {r['reason']}"]
    else:
        L += ["", f"## Score: {r['passed']} / {r['scored']} pillars passed"]
        if r["unknown"]:
            L.append(f"\n*{r['unknown']} pillar(s) could not be scored from the filings.*")
    return "\n".join(L)


def telegram(r):
    if "error" in r:
        return f"⚠️ {r['error']}"
    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if r.get("insufficient"):
        return (f"📊 <b>{esc(r['name'])} ({esc(r['ticker'])})</b>\n"
                f"⚠️ <b>Not scored</b>\n<i>{esc(r['reason'])}</i>")
    L = [f"📊 <b>{esc(r['name'])} ({esc(r['ticker'])})</b>",
         f"<b>{r['passed']} / {r['scored']} pillars</b>"]
    if r["price"]:
        L.append(f"<i>${r['price']:,.2f} · ${r['mcap']/1e9:,.1f}B mcap</i>")
    L.append("")
    for p in r["pillars"]:
        L.append(f"{p.mark} <b>{esc(p.name)}</b> {esc(p.value)} "
                 f"<i>({esc(p.target)})</i>")
    L += ["", "<i>Pillar 1 = market cap ÷ 5-yr average net income. "
          "Data: SEC EDGAR 10-K filings.</i>",
          "<i>A screen, not an analysis. Not investment advice.</i>"]
    return "\n".join(L)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    import quote as quote_mod
    r = analyse(args[0], quote_mod.get(args[0]))
    print(telegram(r) if "--telegram" in sys.argv else markdown(r))
    if "--tags" in sys.argv and "error" not in r:
        print("\ntags used:")
        for k, v in r["tags_used"].items():
            print(f"  {k:22} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
