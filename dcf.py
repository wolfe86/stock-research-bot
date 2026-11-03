#!/usr/bin/env python3
"""
Valuation: a discounted cash flow, a target-price assumptions model,
and a reverse DCF -- all from SEC EDGAR filings.

Three numbers, because they answer different questions:

  DCF              What are the future free cash flows worth today?
                   Ten years of FCF discounted at a required return, plus a
                   perpetuity terminal value, plus net cash, per share.

  Assumptions      What should I pay today to earn my required return?
  model            Grow revenue, apply a net margin,
                   apply an exit P/E to get a year-5 price, then discount that
                   back at the return you want. Not a DCF -- a target price.

  Reverse DCF      What growth does today's price already assume?
                   The most honest of the three: it needs no growth forecast,
                   it extracts the market's.

> [!warning] A valuation is an assumptions engine, not a measurement
> Every figure here is a range, and every assumption behind it is printed. A
> single "fair value" would be false precision. Assumptions are derived from
> the filed history -- never invented -- and where history cannot support one
> the model is skipped rather than defaulted.

Stdlib only.

Usage:
    python3 dcf.py ADBE
    python3 dcf.py ADBE --telegram
"""

import sys

import history
import pillars
import quote

YEARS_DCF = 10          # explicit forecast horizon before the terminal value
YEARS_EM = 5            # the target-price model projects five years...
YEARS_EM_LONG = 10      # ...and ten
DISCOUNT = 0.10         # required return for the DCF and the base target
DISCOUNT_HIGH = 0.15    # the stricter "I want 15%" case
TERMINAL_GROWTH = 0.025 # perpetuity growth; below long-run GDP on purpose
EXIT_PE = 15.0          # deliberately conservative exit multiple
MAX_GROWTH = 0.20       # no company compounds above this for a decade
GROWTH_DECAY = 0.85     # growth fades each year toward the terminal rate


def cagr(vals, years=None):
    """Compound annual growth between the first and last value."""
    if not vals or len(vals) < 2 or vals[0] <= 0 or vals[-1] <= 0:
        return None
    n = years or (len(vals) - 1)
    return (vals[-1] / vals[0]) ** (1.0 / n) - 1.0


def fcf_series(series):
    """Free cash flow per fiscal year: operating cash flow minus capex."""
    ocf, capex = series["ocf"], series["capex"]
    years = sorted(set(ocf) & set(capex))[-pillars.YEARS:]
    return years, [ocf[y] - capex[y] for y in years]


def project(base, growth, years, decay=GROWTH_DECAY, floor=TERMINAL_GROWTH):
    """
    Grow `base` forward, fading the rate toward the terminal rate each year.

    A flat growth rate held for ten years is the single biggest way a DCF
    flatters a good company; fading it is the minimum honest correction.

    The fade converges on the terminal rate **from either direction**. Fading
    by `max(floor, g * decay)` looks right and is not: a negative rate snaps
    straight to the floor after one year, so a shrinking business is modelled
    as growing from year two. That made the reverse DCF report -25% for ADBE.
    """
    out, v, g = [], base, growth
    for _ in range(years):
        v *= (1 + g)
        out.append(v)
        g = floor + (g - floor) * decay
    return out


def discount_stream(flows, rate):
    return sum(f / (1 + rate) ** (i + 1) for i, f in enumerate(flows))


def dcf_value(fcf0, growth, shares, net_cash, rate=DISCOUNT,
              terminal=TERMINAL_GROWTH):
    """Intrinsic value per share. None when the terminal formula is undefined."""
    if not fcf0 or fcf0 <= 0 or not shares or rate <= terminal:
        return None
    flows = project(fcf0, growth, YEARS_DCF)
    pv = discount_stream(flows, rate)
    tv = flows[-1] * (1 + terminal) / (rate - terminal)
    pv += tv / (1 + rate) ** YEARS_DCF
    return (pv + (net_cash or 0)) / shares


def median(vals):
    good = sorted(v for v in (vals or []) if v is not None)
    if not good:
        return None
    n = len(good)
    return good[n // 2] if n % 2 else (good[n // 2 - 1] + good[n // 2]) / 2


def em_target(rev0, growth, margin, shares, pe, rate=DISCOUNT, years=YEARS_EM):
    """
    The target-price model: the price to pay today for a required return.

    Revenue grown forward, a net margin applied, an exit P/E applied to the
    resulting earnings, and that future price discounted back at `rate`.

    Not a DCF. A DCF asks what the cash flows are worth; this asks what to pay
    to earn a stated return, which is a different question with a different
    answer -- and the two routinely disagree.
    """
    if not (rev0 and shares and margin and margin > 0 and pe and pe > 0):
        return None
    rev_n = project(rev0, growth, years)[-1]
    eps_n = rev_n * margin / shares
    price_n = eps_n * pe
    return price_n / (1 + rate) ** years


def implied_growth(price, fcf0, shares, net_cash, rate=DISCOUNT):
    """
    Reverse DCF: the FCF growth rate today's price implies.

    Solved by bisection because the DCF is monotonic in growth -- no calculus,
    and it cannot diverge.
    """
    if not (price and fcf0 and fcf0 > 0 and shares):
        return None
    lo, hi = -0.50, 0.60
    for _ in range(80):
        mid = (lo + hi) / 2
        v = dcf_value(fcf0, mid, shares, net_cash, rate)
        if v is None:
            return None
        if v < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def snapshot(ticker):
    """
    One row of comparable metrics. Deliberately small: the multiples that
    survive across companies, plus the two figures that explain them.
    """
    q = quote.get(ticker)
    f = pillars.load_facts(ticker)
    if "error" in f or not q:
        return None
    S = f["series"]
    rev, ni = pillars.last_n(S["revenue"]), pillars.last_n(S["net_income"])
    h = history.get(ticker) or {}
    pe_s = h.get("pe") or []
    return {
        "ticker": ticker.upper().lstrip("$"),
        "name": f["name"],
        "price": q.get("price"),
        "mcap": q.get("mcap"),
        "pe": q.get("pe"),
        "pe_med": median(pe_s[:5]),
        "pfcf": (pe_s and (h.get("pfcf") or [None])[0]) or None,
        "roic": (h.get("roic") or [None])[0],
        "growth": cagr(rev) if len(rev) >= 2 else None,
        "margin": (sum(ni) / sum(rev)) if (rev and ni and sum(rev) > 0) else None,
    }


def comps(ticker, peers):
    """
    Side-by-side multiples against named peers.

    > [!warning] Comps measure relative value, not value
    > A company cheap against its peers is still expensive if the whole sector
    > is. And the peer list decides the answer -- which is why peers are named
    > by the caller and never guessed. SIC 7372 puts Adobe beside Microsoft.
    """
    rows = [snapshot(ticker)]
    for p in peers[:3]:
        r = snapshot(p)
        if r:
            rows.append(r)
    return [r for r in rows if r]


def analyse(ticker, q=None, peers=None):
    f = pillars.load_facts(ticker)
    if "error" in f:
        return f
    S = f["series"]
    q = q or {}
    price = q.get("price")
    shares = q.get("shares") or (pillars.last_n(S["shares"])[-1]
                                 if S["shares"] else None)

    years, fcf = fcf_series(S)
    rev = pillars.last_n(S["revenue"])
    ni = pillars.last_n(S["net_income"])
    if len(fcf) < 3 or not shares:
        return {"error": f"Not enough annual cash-flow history for "
                         f"{ticker.upper()} to value it."}

    latest = lambda k: (sorted(S[k].items())[-1][1] if S[k] else None)
    cash = (latest("cash") or 0) + (latest("short_investments") or 0)
    net_cash = cash - (latest("debt") or 0)

    fcf0 = fcf[-1]
    g_fcf = cagr(fcf)
    g_rev = cagr(rev) if len(rev) >= 2 else None
    margin = (sum(ni) / sum(rev)) if (rev and ni and sum(rev) > 0) else None

    # Growth is capped and bracketed: the base case is the filed history, the
    # low case halves it, the high case is capped at MAX_GROWTH.
    base_g = min(g_fcf, MAX_GROWTH) if g_fcf is not None else None
    cases = None
    if base_g is not None:
        cases = {"low": max(0.0, base_g * 0.5),
                 "base": max(0.0, base_g),
                 "high": min(MAX_GROWTH, max(0.0, base_g) * 1.25)}

    dcf_vals = ({k: dcf_value(fcf0, g, shares, net_cash) for k, g in cases.items()}
                if cases else {})
    # --- the exit multiple, from five years of actual fiscal-year-end P/Es ---
    # The base assumption is the past five years' P/E, with one correction:
    # the *mean* is wrecked by a single near-zero-earnings year. CRM's FY2023 P/E is 800x on $208m of net
    # income, pulling its five-year mean to 221x against a median of 66x. The
    # median is the average that survives one bad year; both are reported.
    sic, sic_desc = pillars.industry(ticker)
    hist = history.get(ticker)
    pe_series = (hist or {}).get("pe") or []
    pe_mean, pe_med = history.avg(pe_series), median(pe_series[:5])
    exit_pe = pe_med
    pe_source = "5-year median of fiscal-year-end P/E"
    if exit_pe is None:
        exit_pe, pe_source = EXIT_PE, f"fallback, no price history ({EXIT_PE:.0f}x)"

    em_g = min(g_rev, MAX_GROWTH) if g_rev is not None else None
    em_vals = {}
    if em_g is not None and margin and exit_pe:
        for horizon in (YEARS_EM, YEARS_EM_LONG):
            for label, rate in (("10%", DISCOUNT), ("15%", DISCOUNT_HIGH)):
                em_vals[(horizon, label)] = em_target(
                    rev[-1], em_g, margin, shares, exit_pe, rate=rate,
                    years=horizon)

    # A DCF is only as good as the base year. One distorted year quietly wrecks
    # it: KO's FY2024 carried a ~$6bn IRS deposit, which drags its five-year FCF
    # CAGR to -17% and collapses the valuation. The model cannot tell a one-off
    # from a decline, so it says when the history is too rough to trust.
    avg_fcf = sum(fcf) / len(fcf)
    warnings = []
    if any(v <= 0 for v in fcf):
        warnings.append("Free cash flow was negative in at least one of the "
                        "last five years, so the growth rate and the base year "
                        "are both unreliable.")
    if g_fcf is not None and g_fcf < 0:
        warnings.append(f"Free cash flow shrank over the period "
                        f"({_pc(g_fcf)} a year). The model projects that "
                        f"decline forward, which a one-off charge would not "
                        f"deserve — check the filings before believing it.")
    if avg_fcf > 0 and abs(fcf0 - avg_fcf) / avg_fcf > 0.40:
        warnings.append(f"The latest year's free cash flow "
                        f"(${fcf0/1e9:,.1f}B) is far from the five-year average "
                        f"(${avg_fcf/1e9:,.1f}B), so the base year may not be "
                        f"representative.")

    if pe_mean and pe_med and pe_med > 0 and pe_mean / pe_med > 1.5:
        warnings.append(f"The five-year P/E mean ({pe_mean:.0f}x) is far above "
                        f"its median ({pe_med:.0f}x) — one near-zero-earnings "
                        f"year distorts the average, so the median is used.")
    pe_now = (q or {}).get("pe")
    if exit_pe and pe_now and pe_now > 0 and exit_pe / pe_now > 1.5:
        warnings.append(f"The exit multiple ({exit_pe:.0f}x) is well above "
                        f"today's P/E ({pe_now:.0f}x). The multiple has been "
                        f"compressing, so averaging it forward assumes it "
                        f"re-rates back up — the single most flattering "
                        f"assumption in this model.")
    if exit_pe and exit_pe > 30:
        warnings.append(f"An exit multiple of {exit_pe:.0f}x is high in absolute "
                        f"terms; the target prices move almost proportionally "
                        f"with it.")

    return {
        "ticker": ticker.upper().lstrip("$"), "name": f["name"], "price": price,
        "avg_fcf": avg_fcf, "warnings": warnings,
        "shares": shares, "net_cash": net_cash, "fcf0": fcf0, "fcf_years": years,
        "g_fcf": g_fcf, "g_rev": g_rev, "margin": margin,
        "cases": cases, "dcf": dcf_vals, "em": em_vals,
        "exit_pe": exit_pe, "pe_source": pe_source,
        "pe_mean": pe_mean, "pe_median": pe_med, "pe_series": pe_series[:5],
        "pe_now": (q or {}).get("pe"),
        "implied": implied_growth(price, fcf0, shares, net_cash) if price else None,
        "comps": comps(ticker, peers) if peers else None,
        "sic": sic, "industry": sic_desc,
    }


def _money(v):
    return "—" if v is None else f"${v:,.2f}"


def _pc(v):
    return "—" if v is None else f"{v*100:.1f}%"


def _verdict(price, low, high):
    if not price or low is None or high is None:
        return None
    if price < low:
        return "below the whole range"
    if price > high:
        return "above the whole range"
    return "inside the range"


def markdown(r):
    if "error" in r:
        return r["error"]
    d, e = r["dcf"], r["em"]
    L = [f"# {r['name']} ({r['ticker']}) — valuation", ""]
    if r["price"]:
        L.append(f"**Price:** {_money(r['price'])} · "
                 f"FY{r['fcf_years'][0]}–{r['fcf_years'][-1]}")
    L += ["", "## Assumptions, from the filed history", "",
          "| Input | Value | Source |", "|---|---|---|",
          f"| Latest free cash flow | ${r['fcf0']/1e9:,.2f}B | operating cash flow − capex |",
          f"| 5-year average FCF | ${r['avg_fcf']/1e9:,.2f}B | for comparison with the base year |",
          f"| FCF growth (5-yr CAGR) | {_pc(r['g_fcf'])} | used for the DCF |",
          f"| Revenue growth (5-yr CAGR) | {_pc(r['g_rev'])} | used for the target price |",
          f"| Net margin (5-yr average) | {_pc(r['margin'])} | total net income / total revenue |",
          f"| Net cash | ${r['net_cash']/1e9:,.2f}B | cash + short-term investments − debt |",
          f"| Discount rate | {_pc(DISCOUNT)} | required return |",
          f"| Terminal growth | {_pc(TERMINAL_GROWTH)} | below long-run GDP |",
          f"| Exit P/E | {r['exit_pe']:.1f}x | {r['pe_source']} |",
          "",
          f"*Growth fades by {int((1-GROWTH_DECAY)*100)}% a year toward the terminal "
          f"rate — a flat rate held for {YEARS_DCF} years is how a DCF flatters a "
          f"good company.*", ""]

    if d:
        L += ["## 1. Discounted cash flow", "",
              "| Case | FCF growth | Value per share |", "|---|---|---|"]
        for k in ("low", "base", "high"):
            L.append(f"| {k.title()} | {_pc(r['cases'][k])} | **{_money(d.get(k))}** |")
        v = _verdict(r["price"], d.get("low"), d.get("high"))
        if v:
            L += ["", f"Today's price of {_money(r['price'])} is **{v}**."]
        L.append("")

    if e:
        L += ["## 2. Target price — the assumptions model", "",
              "*What to pay today to earn the return you want.*", "",
              "| Horizon | For 10% a year | For 15% a year |", "|---|---|---|"]
        for h in (YEARS_EM, YEARS_EM_LONG):
            L.append(f"| {h} years | **{_money(e.get((h,'10%')))}** "
                     f"| **{_money(e.get((h,'15%')))}** |")
        L += ["",
              f"Base assumptions: revenue grows **{_pc(r['g_rev'])}** a year "
              f"(5-year CAGR, fading), a **{_pc(r['margin'])}** net margin "
              f"(5-year average), and a **{r['exit_pe']:.1f}x** exit multiple "
              f"({r['pe_source']}).", ""]
        if r.get("pe_series"):
            L += [f"*Fiscal-year-end P/Es: "
                  f"{' · '.join(f'{v:.0f}x' for v in r['pe_series'] if v)} "
                  f"— mean {r['pe_mean']:.0f}x, median {r['pe_median']:.0f}x, "
                  f"today {r['pe_now']:.0f}x.*"
                  if r.get("pe_now") else "", ""]
        L.append("")

    if r["implied"] is not None:
        L += ["## 3. Reverse DCF — what the price already assumes", "",
              f"At {_money(r['price'])}, the market is pricing roughly "
              f"**{_pc(r['implied'])} annual FCF growth** for {YEARS_DCF} years "
              f"at a {_pc(DISCOUNT)} discount rate. The company actually grew FCF "
              f"at {_pc(r['g_fcf'])} over the last {len(r['fcf_years'])} years.",
              "",
              "*This is the number that needs no forecast — it extracts the "
              "market's own.*", ""]

    if r.get("warnings"):
        L += ["## Reliability", ""]
        for w in r["warnings"]:
            L += ["> [!warning] Treat this valuation with caution", f"> {w}", ""]
    if r.get("comps"):
        L += ["## 4. Comparables", "",
              "| | " + " | ".join(c["ticker"] for c in r["comps"]) + " |",
              "|---|" + "---|" * len(r["comps"])]
        def row(label, key, fmt):
            return (f"| {label} | " +
                    " | ".join(fmt(c.get(key)) for c in r["comps"]) + " |")
        L += [
            row("Price", "price", lambda v: _money(v)),
            row("Market cap", "mcap", lambda v: "—" if not v else f"${v/1e9:,.0f}B"),
            row("P/E today", "pe", lambda v: "—" if not v else f"{v:.1f}x"),
            row("P/E 5-yr median", "pe_med", lambda v: "—" if not v else f"{v:.1f}x"),
            row("P/FCF (last FY)", "pfcf", lambda v: "—" if not v else f"{v:.1f}x"),
            row("ROIC", "roic", lambda v: "—" if v is None else f"{v*100:.1f}%"),
            row("Revenue growth", "growth", lambda v: _pc(v)),
            row("Net margin", "margin", lambda v: _pc(v)),
        ]
        L += ["",
              "> [!warning] Comps measure relative value, not value",
              "> A company cheap against its peers is still expensive if the "
              "whole sector is. The peer list decides the answer, which is why "
              "peers are named rather than guessed.", ""]
    elif r.get("industry"):
        L += [f"*Industry: {r['industry']} (SIC {r['sic']}). "
              f"Add a peer to compare: `/price {r['ticker']} <PEER>`*", ""]

    L += ["---", "",
          "*Assumptions, not measurements. Not investment advice.*"]
    return "\n".join(L)


def telegram(r):
    if "error" in r:
        return f"⚠️ {r['error']}"
    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    d, e = r["dcf"], r["em"]
    L = [f"💵 <b>{esc(r['name'])} ({esc(r['ticker'])})</b>"]
    if r["price"]:
        L.append(f"<i>now {_money(r['price'])}</i>")
    L.append("")
    if d:
        L.append("<b>DCF value/share</b>")
        L.append(f"  {_money(d.get('low'))} – {_money(d.get('high'))} "
                 f"<i>(base {_money(d.get('base'))})</i>")
        v = _verdict(r["price"], d.get("low"), d.get("high"))
        if v:
            L.append(f"  <i>price is {esc(v)}</i>")
    if e:
        L += ["", "<b>Pay no more than</b>"]
        for h in (YEARS_EM, YEARS_EM_LONG):
            L.append(f"  <b>{h}y</b>  {_money(e.get((h,'10%')))} <i>@10%</i>"
                     f"   {_money(e.get((h,'15%')))} <i>@15%</i>")
        L.append(f"  <i>rev {_pc(r['g_rev'])} · margin {_pc(r['margin'])} · "
                 f"{r['exit_pe']:.0f}x exit</i>")

    if r["implied"] is not None:
        L += ["", "<b>Price already assumes</b>",
              f"  {_pc(r['implied'])} FCF growth for {YEARS_DCF}y",
              f"  <i>actually grew {_pc(r['g_fcf'])}</i>"]
    if r.get("warnings"):
        L.append("")
        for w in r["warnings"]:
            L.append(f"⚠️ <i>{esc(w)}</i>")
    if r.get("comps") and len(r["comps"]) > 1:
        L += ["", "<b>vs peers</b>"]
        for c in r["comps"]:
            pe = "—" if not c.get("pe") else f"{c['pe']:.0f}x"
            pf = "—" if not c.get("pfcf") else f"{c['pfcf']:.0f}x"
            ro = "—" if c.get("roic") is None else f"{c['roic']*100:.0f}%"
            mg = "—" if c.get("margin") is None else f"{c['margin']*100:.0f}%"
            L.append(f"  <b>{esc(c['ticker'])}</b> P/E {pe} · P/FCF {pf} · "
                     f"ROIC {ro} · margin {mg}")
        L.append("  <i>relative value only — a cheap peer group is still a "
                 "peer group</i>")
    elif r.get("industry"):
        L.append("")
        L.append(f"<i>{esc(r['industry'])} — add a peer: "
                 f"/price {esc(r['ticker'])} PEER</i>")
    L += ["", f"<i>FCF ${r['fcf0']/1e9:,.1f}B · {_pc(DISCOUNT)} discount</i>",
          "<i>Assumptions, not measurements. Not investment advice.</i>"]
    return "\n".join(L)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    t = args[0]
    r = analyse(t, quote.get(t.lstrip("$")), peers=args[1:])
    print(telegram(r) if "--telegram" in sys.argv else markdown(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
