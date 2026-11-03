#!/usr/bin/env python3
"""
/fundamentals -- a briefing, plus the full report as a file.

Combines the pillars scorecard, the valuations and recent news into one answer,
split by where it is read:

  chat   ~600 characters. Verdict, the failed pillars with the reason each one
         failed, the valuation in one line, one catalyst. 1200 characters
         proved too long for a phone.
  file   The full report, attached to the message and written under
         SRB_OUTPUT_DIR/fundamentals/<TICKER>/. No length limit, tables
         intact, readable at a desk.

> [!important] A briefing, not an analysis
> A fundamental analysis takes judgement. This is a script: it produces the
> numbers and names the gaps. It tells the reader where to look; it does not
> do the looking. Every failed pillar is explained from its own value and threshold,
> which is mechanical and therefore safe -- nothing here is interpretation.

Stdlib only.

Usage:
    python3 fundamentals.py ADBE
    python3 fundamentals.py ADBE --telegram
    python3 fundamentals.py ADBE --write      # also save the report
"""

import sys
from datetime import datetime

import dcf as dcf_mod
import pillars
import quote
import settings

LOCAL = settings.LOCAL
OUT_DIR = settings.OUTPUT_DIR / "fundamentals"

TG_LIMIT = 600          # the chat message; the detail goes in the file

# Why a pillar failing matters, in one clause. Keyed by pillar number so the
# wording cannot drift from the pillar it explains.
WHY = {
    1: "earnings are priced richly against the last five years",
    2: "returns on invested capital are below a 9% hurdle",
    3: "the share count grew — dilution works against you",
    4: "free cash flow did not grow over five years",
    5: "net income did not grow over five years",
    6: "revenue did not grow over five years",
    7: "long-term liabilities are large against average free cash flow",
    8: "the price is high against the cash the business actually produces",
}


def recent_news(ticker, name="", limit=3):
    """
    Newest Finviz headlines for a ticker, company news first.

    > A ticker's Finviz page also carries commentary that merely *mentions* it.
    > Taken newest-first, ADBE's top headline was about Autodesk and CRM's about
    > Microsoft -- shown as "the catalyst" that is simply wrong. Wire-service
    > releases (the company's own announcements) are ranked first, then
    > headlines naming the company or ticker, then the rest.
    """
    try:
        import premarket_report as pm
        rows = pm.get_finviz(ticker).get("news")
    except Exception as e:
        print(f"  ! news lookup failed: {e}", file=sys.stderr)
        return []
    if not rows:
        return []

    word = (name or "").split(",")[0].split()[0].lower() if name else ""
    tick = ticker.lower()

    def rank(n):
        src = (n.get("source") or "").lower()
        title = (n.get("title") or "").lower()
        if any(w in src for w in pm.PR_WIRES):
            return 0                      # the company's own announcement
        if word and word in title:
            return 1                      # names the company
        if tick in title.split():
            return 1
        return 2                          # commentary that merely mentions it

    rows = sorted(rows, key=lambda n: (rank(n), -n["dt"].timestamp()))
    for n in rows:
        n["is_company_news"] = rank(n) < 2
    return rows[:limit]


def analyse(ticker):
    t = ticker.upper().strip().lstrip("$")
    q = quote.get(t)
    p = pillars.analyse(t, q)
    if "error" in p:
        return p
    v = dcf_mod.analyse(t, q)
    return {"ticker": t, "name": p.get("name", t), "quote": q,
            "pillars": p, "value": v if "error" not in v else None,
            "news": recent_news(t, p.get("name", ""))}


def _failed(p):
    return [x for x in p.get("pillars", []) if x.passed is False]


def telegram(r):
    """The ~600 character briefing."""
    if "error" in r:
        return f"⚠️ {r['error']}"
    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    p, v, q = r["pillars"], r["value"], r["quote"] or {}
    L = [f"📋 <b>{esc(r['ticker'])}</b> — {esc(r['name'][:28])}"]

    if p.get("insufficient"):
        L.append("<i>Not scored — see the attached report.</i>")
    else:
        head = f"<b>{p['passed']}/{p['scored']} pillars</b>"
        if q.get("price"):
            head += f" · ${q['price']:,.2f}"
        if v and v.get("dcf", {}).get("low"):
            head += f" · DCF ${v['dcf']['low']:,.0f}–{v['dcf']['high']:,.0f}"
        L.append(head)

    fails = _failed(p)
    if fails:
        L.append("")
        for x in fails[:3]:
            L.append(f"❌ <b>{esc(x.name)}</b> {esc(x.value)} — "
                     f"{esc(WHY.get(x.n, 'below target'))}")
        if len(fails) > 3:
            L.append(f"<i>+{len(fails)-3} more in the report</i>")

    if v:
        em = v.get("em") or {}
        if em.get((5, "10%")):
            L.append("")
            L.append(f"Pay ≤ <b>${em[(5,'10%')]:,.0f}</b> for 10%/yr "
                     f"· <b>${em[(5,'15%')]:,.0f}</b> for 15%")
        if v.get("implied") is not None and v.get("g_fcf") is not None:
            L.append(f"<i>Price implies {v['implied']*100:.1f}% growth; "
                     f"delivered {v['g_fcf']*100:.1f}%</i>")

    if q.get("fpe") and q.get("pe"):
        L.append(f"<i>Fwd P/E {q['fpe']:.1f}x vs {q['pe']:.1f}x trailing</i>")

    # Only surface a headline in the briefing when it is actually about this
    # company; commentary belongs in the report, not on the one line in chat.
    company = [n for n in r["news"] if n.get("is_company_news")]
    if company:
        n = company[0]
        L += ["", f"📰 <i>{esc(n['title'][:70])}</i>"]

    text = "\n".join(L)
    if len(text) > TG_LIMIT:
        # Trim from the bottom; the verdict and the failures are the point.
        while len(text) > TG_LIMIT and len(L) > 4:
            L.pop()
            text = "\n".join(L)
    return text + "\n\n📎 <i>full report attached</i>"


def markdown(r):
    """The full report: everything the briefing had to leave out."""
    if "error" in r:
        return r["error"]
    now = datetime.now(LOCAL)
    d = now.strftime("%Y-%m-%d")
    p, v, q = r["pillars"], r["value"], r["quote"] or {}
    L = [
        "---", "type: source",
        f"title: {r['ticker']} Fundamentals Briefing {d}",
        f"aliases: [{r['ticker']} Fundamentals Briefing {d}]",
        "tags: [investing, fundamentals, market-data]",
        f"created: {d}", "---", "",
        f"# {r['name']} ({r['ticker']}) — Fundamentals Briefing",
        "",
        f"**Generated:** {now:%Y-%m-%d %H:%M} {settings.TIMEZONE_LABEL} by `/fundamentals`.",
        "",
        "> [!warning] A briefing, not an analysis",
        "> Computed from filings and market data. It names the gaps; it does "
        "not interpret them. An analysis that makes a call needs judgement "
        "this script does not apply.",
        "",
    ]
    if q.get("price"):
        L += [f"**Price** ${q['price']:,.2f} · **Market cap** "
              f"${(q.get('mcap') or 0)/1e9:,.1f}B · **P/E** "
              f"{q.get('pe') or '—'} trailing, {q.get('fpe') or '—'} forward", ""]

    L += ["## Eight Pillars", "", pillars.markdown(p).split("\n", 2)[-1], ""]

    fails = _failed(p)
    if fails:
        L += ["### Why the red pillars failed", ""]
        for x in fails:
            L.append(f"- **{x.name}** — {x.value} against a target of "
                     f"{x.target}. {WHY.get(x.n, '').capitalize()}. {x.note}")
        L.append("")

    if v:
        L += ["## Valuation", "", dcf_mod.markdown(v).split("\n", 2)[-1], ""]

    if r["news"]:
        L += ["## Recent headlines", "",
              "*Company announcements first, then headlines naming the "
              "company, then commentary that merely mentions it. Headlines "
              "only — no judgement is applied to them.*", ""]
        for n in r["news"]:
            title = n["title"].replace("[", "(").replace("]", ")")
            tag = "" if n.get("is_company_news") else " — *mentions only*"
            L.append(f"- **{n['dt']:%b %-d %H:%M} ET** · [{title}]({n['url']}) "
                     f"*({n['source']})*{tag}")
        L.append("")

    return "\n".join(L)


def write_report(r):
    """Save the report under OUT_DIR/<TICKER>/. Returns the path, or None."""
    if "error" in r:
        return None
    d = datetime.now(LOCAL).strftime("%Y-%m-%d")
    out = OUT_DIR / r["ticker"]
    try:
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{r['ticker']}_Fundamentals_{d}.md"
        path.write_text(markdown(r))
        return path
    except Exception as e:
        print(f"  ! report write failed: {e}", file=sys.stderr)
        return None


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    r = analyse(args[0])
    if "--telegram" in sys.argv:
        t = telegram(r)
        print(t)
        print(f"\n[{len(t)} characters]")
    else:
        print(markdown(r))
    if "--write" in sys.argv:
        print(f"\nwrote {write_report(r)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
