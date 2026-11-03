#!/usr/bin/env python3
"""
Market context: trend, consensus sentiment, commodities, crypto and yields.

Shared by the premarket report and the alerts digests. Every fetch degrades to
None rather than raising, so one dead source costs a section, never the report.

Deliberately NOT a quote board: today's index moves are on any ticker. What a
ticker cannot tell you is where the market sits in its trend and what the
crowd thinks -- which is what this module measures.

Stdlib only, to keep the unattended systemd runs dependency-free.

Sources:
  CNBC quote cache      -- real indices, futures, yields. No key.
  CNN Fear & Greed      -- the most-watched consensus sentiment gauge, 0-100,
                           built from seven equally weighted components.
  AAII sentiment survey -- retail bull/bear survey, published weekly (Thu).
"""

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html import unescape

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

CNBC_QUOTE = ("https://quote.cnbc.com/quote-html-webservice/restQuote/"
              "symbolType/symbol?symbols={syms}&requestMethod=itv&noform=1"
              "&partnerId=2&fund=1&exthrs=1&output=json")

FNG_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
AAII_URL = "https://www.aaii.com/sentimentsurvey"

# CNBC symbols. Indices first -- .SPX drives the trend read.
INDEXES = [".SPX", ".IXIC", ".DJI", ".RUT", ".VIX"]
COMMODITIES = ["@CL.1", "@BZ.1", "@GC.1"]
CRYPTO = ["BTC.CM="]
YIELDS = ["US10Y", "US2Y", "US30Y"]
ALL_SYMBOLS = INDEXES + COMMODITIES + CRYPTO + YIELDS

LABEL = {
    ".SPX": "S&P 500", ".IXIC": "NASDAQ", ".DJI": "Dow", ".RUT": "Russell 2000",
    ".VIX": "VIX", "@CL.1": "WTI crude", "@BZ.1": "Brent crude",
    "@GC.1": "Gold", "BTC.CM=": "Bitcoin",
    "US10Y": "US 10Y", "US2Y": "US 2Y", "US30Y": "US 30Y",
}

# --- Icons for the Telegram digest ---
# The icon carries the reading, so it must track the value rather than being
# decoration: a fixed icon beside a changing label is worse than none, because
# it is read as meaning something.
TREND_ICON = {
    "BULL MARKET": "🐂",
    "BEAR MARKET": "🐻",
    "CORRECTION": "📉",
    "NEUTRAL": "⚖️",
}

# Keyed on CNN's own rating strings.
FNG_ICON = {
    "extreme fear": "😱",
    "fear": "😨",
    "neutral": "😐",
    "greed": "🤑",
    "extreme greed": "🤩",
}

INSTRUMENT_ICON = {
    "@CL.1": "🛢️", "@BZ.1": "🛢️", "@GC.1": "🪙", "BTC.CM=": "₿",
    "US10Y": "🏦", "US2Y": "🏦", "US30Y": "🏦",
}

# Fear & Greed components, in the order CNN displays them.
FNG_COMPONENTS = [
    ("market_momentum_sp500", "Momentum"),
    ("stock_price_strength", "Price strength"),
    ("stock_price_breadth", "Breadth"),
    ("put_call_options", "Put/call"),
    ("market_volatility_vix", "Volatility"),
    ("junk_bond_demand", "Junk bonds"),
    ("safe_haven_demand", "Safe haven"),
]


def _get(url, timeout=25, referer=None):
    """GET returning text, or None. Never raises."""
    headers = {"User-Agent": UA, "Accept": "application/json, text/html, */*"}
    if referer:
        headers["Referer"] = referer
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"  ! fetch failed {url.split('?')[0]}: {e}", file=sys.stderr)
        return None


def num(s):
    """'7,735.56' / '+1.11%' / '4.965%' -> float, or None."""
    if s is None:
        return None
    s = str(s).strip().replace(",", "").replace("%", "").replace("+", "")
    try:
        return float(s)
    except ValueError:
        return None


def quotes(symbols=None):
    """
    One CNBC call for every instrument. Returns {symbol: quote dict}.

    CNBC returns display strings ('7,735.56'), kept as-is for printing; the
    numeric parsing happens where arithmetic is actually needed.
    """
    symbols = symbols or ALL_SYMBOLS
    body = _get(CNBC_QUOTE.format(syms=urllib.parse.quote("|".join(symbols))))
    if not body:
        return {}
    try:
        got = json.loads(body)["FormattedQuoteResult"]["FormattedQuote"]
    except (KeyError, ValueError, TypeError) as e:
        print(f"  ! CNBC payload unreadable: {e}", file=sys.stderr)
        return {}
    out = {}
    for q in got:
        sym = q.get("symbol")
        if sym:
            out[sym] = q
    return out


def fear_greed():
    """
    CNN Fear & Greed: headline score, rating, history and the seven components.

    This is the report's answer to 'is the market bullish or bearish' -- a
    published consensus gauge rather than a label computed here.
    """
    body = _get(FNG_URL, referer="https://www.cnn.com/")
    if not body:
        return None
    try:
        d = json.loads(body)
        fg = d["fear_and_greed"]
    except (KeyError, ValueError, TypeError) as e:
        print(f"  ! Fear & Greed payload unreadable: {e}", file=sys.stderr)
        return None
    comps = []
    for key, label in FNG_COMPONENTS:
        c = d.get(key) or {}
        score = c.get("score")
        if score is None:
            continue
        comps.append({"label": label, "score": float(score),
                      "rating": c.get("rating", "")})
    return {
        "score": float(fg.get("score", 0)),
        "rating": fg.get("rating", ""),
        "prev_close": fg.get("previous_close"),
        "week": fg.get("previous_1_week"),
        "month": fg.get("previous_1_month"),
        "year": fg.get("previous_1_year"),
        "components": comps,
    }


def aaii():
    """
    AAII retail sentiment survey -- % bullish / neutral / bearish.

    Published weekly on Thursday, so the same figures repeat for several days.
    A classic contrarian gauge: extreme retail bearishness often marks lows.
    """
    body = _get(AAII_URL)
    if not body:
        return None
    text = re.sub(r"<[^>]+>", " ", body)
    out = {}
    for key in ("Bullish", "Neutral", "Bearish"):
        m = re.search(key + r"[^0-9]{0,40}(\d{1,2}\.\d)\s*%", text)
        if m:
            out[key.lower()] = float(m.group(1))
    return out if len(out) == 3 else None


def trend_state(spx):
    """
    Where the S&P sits in its cycle, by the 20% rule.

    > [!note] Approximation
    > The textbook rule measures from the cycle peak. This uses the rolling
    > 52-week high, which CNBC supplies directly. The two agree except shortly
    > after a peak older than a year, and the percentage is always printed so
    > the label can be checked against it.
    """
    if not spx:
        return None
    last, hi, lo = num(spx.get("last")), num(spx.get("yrhiprice")), num(spx.get("yrloprice"))
    if not (last and hi and lo):
        return None
    off_high = (last - hi) / hi * 100.0
    off_low = (last - lo) / lo * 100.0
    if off_high <= -20:
        label = "BEAR MARKET"
    elif off_high <= -10:
        label = "CORRECTION"
    elif off_low >= 20:
        label = "BULL MARKET"
    else:
        label = "NEUTRAL"
    return {"label": label, "off_high": off_high, "off_low": off_low,
            "last": last, "high": hi, "low": lo,
            "high_date": spx.get("yrhidate", ""), "low_date": spx.get("yrlodate", "")}


def divergence(fng):
    """
    Breadth contradicting the headline -- the tell that an index is being
    carried by a few names while the average stock goes nowhere.

    Returns a warning string, or None when breadth is not an outlier.
    """
    if not fng:
        return None
    weak = [c for c in fng["components"]
            if c["label"] in ("Breadth", "Price strength") and c["score"] <= 20]
    if len(weak) < 2:
        return None
    return ("Breadth and price strength are at the floor — the index is moving "
            "on a handful of names while the average stock is not participating.")


def _strip(s):
    """
    RSS text to plain text: unwrap CDATA, drop tags, decode entities.

    Entities are decoded with `html.unescape` rather than a hand-written table,
    which only covered the named ones -- MarketWatch stamps its titles with
    numeric entities, so "Meta's stock" arrived as "Meta&#x2019;s stock".
    The order matters: CDATA and tags come off first, so a decoded entity can
    never produce something that then reads as markup.
    """
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", unescape(s)).strip()


def feed_items(source, url, timeout=20):
    """
    Every `<item>` in an RSS feed as {dt, title, url, source}, unsorted.

    Used by `news.py`. Items without a title or a parseable timestamp are
    dropped: a headline that cannot be placed in time is useless.
    """
    from email.utils import parsedate_to_datetime

    body = _get(url, timeout=timeout)
    if not body:
        return []
    out = []
    for item in re.findall(r"<item>(.*?)</item>", body, re.S):
        m = re.search(r"<title>(.*?)</title>", item, re.S)
        title = _strip(m.group(1)) if m else ""
        lm = re.search(r"<link>(.*?)</link>", item, re.S)
        dm = re.search(r"<pubDate>(.*?)</pubDate>", item, re.S)
        if not title or not dm:
            continue
        try:
            dt = parsedate_to_datetime(_strip(dm.group(1)))
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:
            continue
        out.append({"dt": dt, "title": title,
                    "url": _strip(lm.group(1)) if lm else "", "source": source})
    return out


# The floor for a large cap: AMD, KO and WM clear it. calendar_events.py
# uses the same threshold, so the project has one definition, not two.
LARGE_CAP_FLOOR = 50e9


def context():
    """Everything the reports need, in one shot."""
    q = quotes()
    fng = fear_greed()
    return {
        "quotes": q,
        "fng": fng,
        "aaii": aaii(),
        "trend": trend_state(q.get(".SPX")),
        "divergence": divergence(fng),
    }


def price(sym, q):
    """
    Level for display.

    Only instruments actually quoted in dollars get a `$`: commodities and
    crypto. An index is a level, not a price, and a yield already prints its
    own '%'.
    """
    if not q:
        return "—"
    last = q.get("last", "—")
    return f"${last}" if sym in COMMODITIES + CRYPTO else last


def move(sym, q):
    """
    Change for display.

    Yields move in **basis points**, not percent -- CNBC's `change_pct` for a
    yield is the percent change OF the percentage (10Y 4.996 -> 4.961 prints as
    -0.70%), which reads as a market move and is not one. For yields the
    `change` field is in percentage points, so x100 gives basis points.
    """
    if not q:
        return "—"
    if sym in YIELDS:
        c = num(q.get("change"))
        return f"{c * 100:+.1f} bp" if c is not None else "—"
    return q.get("change_pct", "—")


def markdown_block(ctx):
    """The `## Market` section for a report note."""
    L = ["", "## Market", ""]
    t = ctx.get("trend")
    if t:
        L += [f"**Trend — {t['label']}.** S&P 500 at {t['last']:,.2f}, "
              f"{abs(t['off_high']):.1f}% {'below' if t['off_high'] < 0 else 'above'} "
              f"its 52-week high ({t['high']:,.2f}, {t['high_date']}) and "
              f"{t['off_low']:+.1f}% from the 52-week low ({t['low']:,.2f}, {t['low_date']}).",
              "",
              "*By the 20% rule, measured against the rolling 52-week high.*", ""]

    f = ctx.get("fng")
    if f:
        L += [f"**Sentiment — {f['rating'].upper()} ({f['score']:.0f}/100).** "
              f"CNN Fear & Greed, the consensus gauge. "
              f"Yesterday {_fg(f['prev_close'])} · last week {_fg(f['week'])} · "
              f"last month {_fg(f['month'])} · last year {_fg(f['year'])}.", ""]
        L += ["| Component | Score | Reading |", "|---|---|---|"]
        for c in f["components"]:
            L.append(f"| {c['label']} | {c['score']:.0f} | {c['rating']} |")
        L.append("")

    d = ctx.get("divergence")
    if d:
        L += ["> [!warning] Price and breadth disagree", f"> {d}", ""]

    a = ctx.get("aaii")
    if a:
        L += [f"**Retail (AAII, weekly):** {a['bullish']:.1f}% bullish · "
              f"{a['neutral']:.1f}% neutral · {a['bearish']:.1f}% bearish.", ""]

    q = ctx.get("quotes") or {}
    vix = q.get(".VIX")
    if vix:
        L += [f"**VIX {vix.get('last','—')}** ({vix.get('change_pct','')}) — "
              f"{_vix_words(num(vix.get('last')))}.", ""]

    rows = [(LABEL[s], q.get(s)) for s in COMMODITIES + CRYPTO + YIELDS if q.get(s)]
    if rows:
        L += ["| Instrument | Level | Change |", "|---|---|---|"]
        for sym in COMMODITIES + CRYPTO + YIELDS:
            qq = q.get(sym)
            if qq:
                L.append(f"| {LABEL[sym]} | {price(sym, qq)} | {move(sym, qq)} |")
        L.append("")
    return L


def _fg(v):
    return f"{float(v):.0f}" if v is not None else "—"


def _vix_words(v):
    if v is None:
        return "level not recorded"
    if v < 15:
        return "calm"
    if v < 20:
        return "normal"
    if v < 25:
        return "elevated"
    if v < 30:
        return "stressed"
    return "panic"


def telegram_block(ctx, esc, yield_min_bp=None, skip=()):
    """
    The market section for a Telegram digest. `esc` is the caller's HTML escaper,
    passed in so this module stays free of report-specific imports.
    """
    L = []
    t = ctx.get("trend")
    if t:
        L.append(f"{TREND_ICON.get(t['label'], '•')} <b>{esc(t['label'])}</b> · "
                 f"S&amp;P {t['last']:,.0f} "
                 f"({abs(t['off_high']):.1f}% off 52wk high)")
    f = ctx.get("fng")
    if f:
        icon = FNG_ICON.get((f["rating"] or "").lower(), "😐")
        L.append(f"{icon} <b>Fear &amp; Greed {f['score']:.0f}/100 — "
                 f"{esc(f['rating'].upper())}</b>")
        L.append(f"   <i>{_fg(f['prev_close'])} yest · {_fg(f['week'])} wk · "
                 f"{_fg(f['month'])} mo</i>")
    q = ctx.get("quotes") or {}
    vix = q.get(".VIX")
    if vix:
        L.append(f"   <i>VIX {esc(vix.get('last','—'))} "
                 f"({esc(_vix_words(num(vix.get('last'))))})</i>")
    a = ctx.get("aaii")
    if a:
        L.append(f"   <i>AAII {a['bullish']:.0f}% bull / {a['bearish']:.0f}% bear</i>")
    d = ctx.get("divergence")
    if d:
        L.append(f"⚠️ <i>{esc(d)}</i>")

    # One instrument per line with its own icon: a single run-on line of four
    # instruments is unreadable on a phone, which is the only place this is read.
    rows = []
    for s in COMMODITIES + CRYPTO + YIELDS:
        if not q.get(s) or s in skip:
            continue
        if s in YIELDS and yield_min_bp is not None:
            bp = num(q[s].get("change"))
            if bp is None or abs(bp * 100) < yield_min_bp:
                continue
        rows.append(s)
    if rows:
        L.append("")
        for s in rows:
            L.append(f"{INSTRUMENT_ICON.get(s, '•')} <b>{esc(LABEL[s])}</b> "
                     f"{esc(price(s, q[s]))} ({esc(move(s, q[s]))})")
    return L


if __name__ == "__main__":
    ctx = context()
    print("\n".join(markdown_block(ctx)))
    print("\n--- telegram ---")
    print("\n".join(telegram_block(ctx, lambda x: str(x))))
