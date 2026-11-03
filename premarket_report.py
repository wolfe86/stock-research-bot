#!/usr/bin/env python3
"""
Premarket gapper report for small-float momentum trading.

Pulls premarket gainers, enriches with float/short-interest/country,
computes float rotation, appends to the CSV log (data/movers.csv), writes a
markdown report to SRB_OUTPUT_DIR/daily/, and posts a phone-readable summary
to Telegram. With TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID unset the report is
still written; the post is skipped with a note on stderr.

Stdlib only -- no pip install needed, so it runs unattended from a systemd timer.
SEC_USER_AGENT is needed for the dilution columns (SEC EDGAR); without it
those columns print "—" and the error is logged per ticker.

Usage:
    python3 premarket_report.py                 # today's report
    python3 premarket_report.py --dry-run       # print, don't write or post
    python3 premarket_report.py --telegram-only # re-post today's rows from the CSV
"""

import re
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
# Aliased: three scraping functions below bind `html` as a local variable.
from html import escape as html_escape
from pathlib import Path
from zoneinfo import ZoneInfo

import calendar_events as cal
import dilution
import market
import notify
import ah_watch
import settings
import store
import themes

ROOT = Path(__file__).resolve().parent

# Reports go to SRB_OUTPUT_DIR (default data/reports); code and the CSV log
# stay with the repository.
DAILY = settings.OUTPUT_DIR / "daily"
CSV_PATH = ROOT / "data" / "movers.csv"
# The CSV log's columns. Several are left for a post-session fill-in (open,
# hod, outcome, ...); the report writes the ones it knows.
CSV_HEADER = (
    "date,ticker,company,country,exchange,sector,float_shares,shares_out,"
    "short_float_pct,market_cap,prev_close,pm_high,pm_low,pm_volume,gap_pct,"
    "open,hod,lod,close,close_pct_chg,session_volume,rel_volume,float_rotation,"
    "has_news,catalyst_type,catalyst_headline,news_source,halts,"
    "reverse_split_recent,offering_risk,open_to_hod_pct,open_to_close_pct,"
    "outcome,theme_tag,traded,notes,ssr_active,dilution_risk,dilution_flags,"
    "shelf_count,shelf_last,offering_count,offering_last,days_since_offering,"
    "rs_detected,rs_ratio,rs_prior_shares,rs_asof,cik").split(",")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

ET = ZoneInfo("America/New_York")
LOCAL = settings.LOCAL       # local time zone (settings.TIMEZONE)
TZL = settings.TIMEZONE_LABEL

# --- Screening thresholds: tuned for low-float momentum, not blue chips ---
MIN_GAP_PCT = 10.0          # ignore anything under +10%
MIN_PM_VOLUME = 50_000      # filter out illiquid noise prints
MAX_MARKET_CAP = 2_000_000_000
ENRICH_TOP_N = 15           # Finviz lookups (rate-limited, so cap it)

# --- Telegram: a phone-readable digest, not the whole report ---
# Credentials come from the environment (see settings.py). Never hardcode
# them here.
TG_TOP_N = 8                # gappers listed in the post

RISK_BADGE = {"EXTREME": "🔴 EXTREME", "HIGH": "🟠 HIGH",
              "MEDIUM": "🟡 MED", "LOW": "🟢 LOW"}


def fetch(url, retries=3, timeout=25):
    """GET with a browser UA and simple backoff."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/json,*/*",
                "Accept-Language": "en-US,en;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="ignore")
        except Exception as e:
            if attempt == retries - 1:
                print(f"  ! fetch failed {url}: {e}", file=sys.stderr)
                return ""
            time.sleep(2 * (attempt + 1))
    return ""


def _field(block, key, numeric=True):
    """Pull one key out of a JS object literal block."""
    if numeric:
        m = re.search(rf'\b{key}:(-?[\d.eE+]+)', block)
        if not m:
            return None
        try:
            return float(m.group(1))
        except ValueError:
            return None
    m = re.search(rf'\b{key}:"((?:[^"\\]|\\.)*)"', block)
    return m.group(1).replace('\\"', '"') if m else None


def get_premarket_gainers():
    """
    Extract the gainer records from the SvelteKit payload embedded in the page.

    Parsed record-by-record rather than as one JSON blob: the payload is JS object
    notation (unquoted keys, occasional undefined/nulls) and a single malformed
    value would otherwise throw away the whole list.
    """
    html = fetch("https://stockanalysis.com/markets/premarket/gainers/")
    if not html:
        return []

    blocks = re.findall(r'\{no:\d+,[^{}]*\}', html)
    if not blocks:
        print("  ! could not locate embedded data payload", file=sys.stderr)
        return []

    out, seen = [], set()
    for b in blocks:
        tkr = _field(b, "s", numeric=False)
        if not tkr or tkr in seen:
            continue
        seen.add(tkr)
        out.append({
            "s": tkr,
            "n": _field(b, "n", numeric=False) or "",
            "premarketChangePercent": _field(b, "premarketChangePercent"),
            "premarketPrice": _field(b, "premarketPrice"),
            "premarketVolume": _field(b, "premarketVolume"),
            "marketCap": _field(b, "marketCap"),
        })
    return out


def parse_num(s):
    """Finviz numbers: '125.06M', '19.90%', '2,115.66', '-'."""
    if not s or s.strip() in ("-", "", "N/A"):
        return None
    s = s.strip().replace(",", "").replace("%", "")
    mult = 1
    if s and s[-1] in "KMBT":
        mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[s[-1]]
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


FINVIZ_FIELDS = ["Shs Float", "Shs Outstand", "Short Float", "Short Ratio",
                 "Market Cap", "Rel Volume", "Avg Volume", "Volume", "Price"]

COUNTRIES = ("USA|China|Hong Kong|Singapore|Israel|Canada|Cayman Islands|Germany|"
             "Netherlands|United Kingdom|Malaysia|Taiwan|South Korea|Japan|Australia|"
             "India|Brazil|Greece|Ireland|Switzerland|France|Sweden|Norway|Denmark")


def get_finviz(ticker):
    """Scrape float, short interest, country, sector from a Finviz quote page."""
    html = fetch(f"https://finviz.com/quote.ashx?t={ticker}")
    if not html:
        return {}

    cells = re.findall(r"<td[^>]*>(.*?)</td>", html, re.S)
    clean = []
    for c in cells:
        t = re.sub(r"<[^>]+>", "", c)
        for a, b in (("&amp;", "&"), ("&nbsp;", " "), ("&#39;", "'"), ("&quot;", '"')):
            t = t.replace(a, b)
        clean.append(t.strip())

    out = {}
    for i, c in enumerate(clean):
        if c in FINVIZ_FIELDS and i + 1 < len(clean) and c not in out:
            out[c] = clean[i + 1]

    cm = re.search(rf"\b({COUNTRIES})\b", html)
    out["Country"] = cm.group(1) if cm else ""

    out["news"] = parse_finviz_news(html)

    out["Sector"] = finviz_category(html, "sec")
    out["Industry"] = finviz_category(html, "ind")
    return out


# The sector / industry chips in the quote header. Finviz has used two
# markups: the older `screener.ashx?v=111&f=sec_healthcare` and the current
# `screener?v=111&f=sec_healthcare`, whose industry chip wraps its text in a
# <span class="min-w-0 truncate">. The old pattern silently returned "" from
# the day Finviz dropped `.ashx` -- unnoticed because Sector was scraped and
# never displayed. Both are matched now, so a further change breaks the field
# rather than passing an empty string off as "no sector".
def finviz_category(html, kind):
    m = re.search(rf'f={kind}_[^"\'>]*["\'][^>]*>(?:\s*<span[^>]*>)?([^<]+)<', html)
    if not m:
        return ""
    t = m.group(1)
    for a, b in (("&amp;", "&"), ("&nbsp;", " "), ("&#39;", "'"), ("&quot;", '"')):
        t = t.replace(a, b)
    return t.strip()


# Wire services that carry company press releases. Finviz also lists articles and
# commentary (Yahoo, Moby, GuruFocus...); those are left out on purpose.
PR_WIRES = ("newswire", "business wire", "accesswire", "newsfile", "mediawire")


def parse_finviz_news(html):
    """
    News rows from a Finviz quote page, as dicts with an ET datetime.

    Finviz stamps rows in ET and only prints the date on the first row of each
    day ("Sep-16-26 04:15PM", or "Today 06:30AM"); later rows carry just the time.
    Returns None when the news table is missing, so a failed page is never read
    as "no news".
    """
    i = html.find('id="news-table"')
    if i < 0:
        return None
    j = html.find("</table>", i)
    today = datetime.now(ET).date()
    out, day = [], None
    for tr in re.findall(r"<tr.*?</tr>", html[i:j], re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        a = re.search(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', tr, re.S)
        src = re.search(r"<span>\((.*?)\)</span>", tr)
        if not tds or not a:
            continue
        stamp = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", tds[0])).strip()
        parts = stamp.split(" ")
        try:
            if len(parts) == 2:
                if parts[0] == "Today":
                    day = today
                elif parts[0] == "Yesterday":
                    day = today - timedelta(days=1)
                else:
                    day = datetime.strptime(parts[0], "%b-%d-%y").date()
            t = datetime.strptime(parts[-1], "%I:%M%p").time()
        except ValueError:
            continue
        if day is None:
            continue
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", a.group(2))).strip()
        for x, y in (("&amp;", "&"), ("&#39;", "'"), ("&quot;", '"'), ("&nbsp;", " ")):
            title = title.replace(x, y)
        url = a.group(1)
        if url.startswith("/"):
            url = "https://finviz.com" + url
        out.append({
            "dt": datetime.combine(day, t, tzinfo=ET),
            "source": src.group(1) if src else "",
            "title": title,
            "url": url,
        })
    return out


def press_releases(news, start, end):
    """Wire-service releases stamped in [start, end), oldest first."""
    if news is None:
        return None
    hits = [n for n in news
            if start <= n["dt"] < end
            and any(w in n["source"].lower() for w in PR_WIRES)]
    return sorted(hits, key=lambda n: n["dt"])


def last_close(now_et):
    """16:00 ET on the previous weekday (market holidays are not skipped)."""
    d = now_et.date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET)


def pr_lines(tkr, prs):
    L = []
    for n in prs:
        title = n["title"].replace("[", "(").replace("]", ")")
        L.append(f"- **{tkr}** · {n['dt']:%a %H:%M} ET · [{title}]({n['url']}) "
                 f"*({n['source']})*")
    return L


def build_rows():
    print("Fetching premarket gainers...")
    gainers = get_premarket_gainers()
    print(f"  got {len(gainers)} raw")

    # Screen down to plausible low-float momentum candidates
    cands = []
    for g in gainers:
        gap = g.get("premarketChangePercent") or 0
        vol = g.get("premarketVolume") or 0
        cap = g.get("marketCap") or 0
        if gap < MIN_GAP_PCT or vol < MIN_PM_VOLUME:
            continue
        if cap and cap > MAX_MARKET_CAP:
            continue
        cands.append(g)

    cands.sort(key=lambda x: x.get("premarketChangePercent") or 0, reverse=True)
    print(f"  {len(cands)} pass screen (gap>{MIN_GAP_PCT}%, vol>{MIN_PM_VOLUME:,})")

    rows = []
    for i, g in enumerate(cands):
        tkr = g.get("s", "")
        row = {
            "ticker": tkr,
            "company": g.get("n", ""),
            "gap_pct": round(g.get("premarketChangePercent") or 0, 2),
            "pm_price": g.get("premarketPrice"),
            "pm_volume": g.get("premarketVolume"),
            "market_cap": g.get("marketCap"),
        }

        if i < ENRICH_TOP_N:
            print(f"  enriching {tkr} ({i+1}/{min(len(cands), ENRICH_TOP_N)})")
            fv = get_finviz(tkr)
            row["float_shares"] = parse_num(fv.get("Shs Float"))
            row["shares_out"] = parse_num(fv.get("Shs Outstand"))
            row["short_float_pct"] = parse_num(fv.get("Short Float"))
            row["rel_volume"] = parse_num(fv.get("Rel Volume"))
            row["country"] = fv.get("Country", "")
            row["sector"] = fv.get("Sector", "")
            now_et = datetime.now(ET)
            row["press"] = press_releases(fv.get("news"), last_close(now_et), now_et)
            time.sleep(0.6)  # be polite to Finviz

        # The squeeze tell: how many times the float turned over premarket
        fl, pv = row.get("float_shares"), row.get("pm_volume")
        row["float_rotation"] = round(pv / fl, 2) if fl and pv else None
        rows.append(row)

    # --- dilution / borrow risk: can this company dump into the spike? ---
    print("Loading SEC filing data...")
    cik_map = dilution.load_cik_map()
    ssr_set, ssr_day = dilution.load_ssr_list()
    print(f"  CIKs={len(cik_map):,} SSR={len(ssr_set)} symbols ({ssr_day})")

    for r in rows[:ENRICH_TOP_N]:
        t = r["ticker"]
        r["ssr_active"] = "YES" if t.upper() in ssr_set else "no"
        try:
            prof = dilution.sec_profile(t, cik_map, shares_out=r.get("shares_out"))
            risk, flags = dilution.dilution_risk(prof)
        except Exception as e:
            print(f"  ! {t} dilution lookup failed: {e}", file=sys.stderr)
            continue
        r.update(prof)
        r["dilution_risk"] = risk
        r["dilution_flags"] = "; ".join(flags)
        print(f"  {t}: {risk}")

    return rows


def fmt(v, kind="num"):
    if v is None:
        return "—"
    if kind == "sh":
        for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
            if v >= div:
                return f"{v/div:.2f}{suf}"
        return f"{v:.0f}"
    if kind == "pct":
        return f"{v:.1f}%"
    if kind == "x":
        return f"{v:.2f}x"
    return f"{v:,.0f}" if isinstance(v, (int, float)) else str(v)


def flag_sets(rows):
    """
    The structural flags, computed once.

    Shared by the markdown report and the Telegram post so the two can never
    disagree about what was flagged on a given morning.
    """
    return {
        "micro": [r for r in rows if (r.get("float_shares") or 9e9) < 2e6],
        "squeeze": [r for r in rows if (r.get("short_float_pct") or 0) > 15],
        "rotated": [r for r in rows if (r.get("float_rotation") or 0) >= 1.0],
        "danger": [r for r in rows if r.get("dilution_risk") in ("EXTREME", "HIGH")],
        "clean": [r for r in rows if r.get("dilution_risk") == "LOW"],
        "ssr_on": [r for r in rows if r.get("ssr_active") == "YES"],
    }


def write_report(rows, now_ct, ctx=None, events=None, dry=False):
    date_str = now_ct.strftime("%Y-%m-%d")
    now_et = now_ct.astimezone(ET)

    # Frontmatter, so the file drops into a notes app (Obsidian and the like).
    L = [
        "---",
        "type: source",
        f"title: Premarket Report — {date_str}",
        # The spaced title is an alias so [[Premarket Report — YYYY-MM-DD]] resolves
        # in Obsidian: the filename uses underscores, and Obsidian matches
        # filename or alias, never the `title:` field.
        f"aliases: [Premarket Report — {date_str}, "
        f"premarket {now_ct.strftime('%-d %b %Y')}]",
        "tags: [investing, market-data, trading, premarket, small-cap]",
        f"created: {date_str}",
        "---",
        "",
        f"# Premarket Report — {now_ct.strftime('%A, %B %-d, %Y')}",
        "",
        "Generated by Stock Research Bot.",
        "",
        f"**Generated:** {now_ct.strftime('%H:%M')} {TZL} "
        + ("" if TZL == "ET" else f"({now_et.strftime('%H:%M')} ET) ")
        + f"· Covers premarket 04:00–{now_et.strftime('%H:%M')} ET",
        "",
        "---",
    ]

    # Theme Watch first: what the runners have been running on, or that there
    # is no theme yet. Never costs the report.
    try:
        L += themes.markdown_block(now_ct.date(), [r["ticker"] for r in rows])
    except Exception as e:
        L += ["", f"*Theme Watch failed: {e}*", "", "---"]

    # After-Hours Watch: last night's after-hours runners and whether the 4am
    # hour cleared the price each one has to hold.
    try:
        L += ah_watch.markdown_block(now_ct.date())
    except Exception as e:
        L += ["", f"*After-Hours Watch failed: {e}*", "", "---"]

    # Market context first: the trend and the crowd's read frame everything below.
    if ctx:
        L += market.markdown_block(ctx)

    if events is not None:
        L += ["---"] + cal.markdown_day(events)

    L += [
        "---",
        "",
        "## Premarket Gappers",
        "",
        "*Float rotation = premarket volume ÷ float. >1x means the whole float "
        "turned over before the open — the key squeeze tell.*",
        "",
        "| # | Ticker | Gap % | Price | Float | Rot | Short % | RelVol | "
        "Dilution | Shelf | Last Offering | SSR | Rev Split | Country |",
        "|---|--------|-------|-------|-------|-----|---------|--------|"
        "----------|-------|---------------|-----|-----------|---------|",
    ]

    for i, r in enumerate(rows[:25], 1):
        star = " ⭐" if (r.get("float_rotation") or 0) >= 1.0 else ""
        risk = r.get("dilution_risk") or "—"
        risk_md = RISK_BADGE.get(risk, risk)
        dso = r.get("days_since_offering")
        last_off = f"{r['offering_last']} ({dso}d)" if r.get("offering_last") else "none"
        rs = (f"~1:{r['rs_ratio']:.0f}" if r.get("rs_detected") else
              ("no" if r.get("rs_detected") is False else "—"))
        L.append(
            f"| {i} | **{r['ticker']}**{star} | +{r['gap_pct']:.1f}% | "
            f"${r.get('pm_price') or '—'} | {fmt(r.get('float_shares'),'sh')} | "
            f"{fmt(r.get('float_rotation'),'x')} | "
            f"{fmt(r.get('short_float_pct'),'pct')} | "
            f"{fmt(r.get('rel_volume'),'x')} | {risk_md} | "
            f"{r.get('shelf_count') if r.get('shelf_count') is not None else '—'} | "
            f"{last_off} | {r.get('ssr_active','—')} | {rs} | "
            f"{r.get('country') or '—'} |"
        )

    # --- automated signal scan ---
    L += ["", "## Auto-Flags", ""]
    flags = flag_sets(rows)
    micro, squeeze, rotated = flags["micro"], flags["squeeze"], flags["rotated"]

    if micro:
        L.append(f"- 🔥 **Sub-2M float ({len(micro)}):** " +
                 ", ".join(f"{r['ticker']} ({fmt(r['float_shares'],'sh')})" for r in micro))
    if squeeze:
        L.append(f"- 🩳 **Short float >15% ({len(squeeze)}):** " +
                 ", ".join(f"{r['ticker']} ({r['short_float_pct']:.1f}%)" for r in squeeze))
    if rotated:
        L.append(f"- 🔄 **Float rotated >1x premarket ({len(rotated)}):** " +
                 ", ".join(f"{r['ticker']} ({r['float_rotation']:.1f}x)" for r in rotated))

    countries = {}
    for r in rows:
        c = r.get("country")
        if c and c != "USA":
            countries.setdefault(c, []).append(r["ticker"])
    for c, tks in sorted(countries.items(), key=lambda kv: -len(kv[1])):
        if len(tks) >= 2:
            L.append(f"- 🌏 **{c} cluster ({len(tks)}):** {', '.join(tks)}")

    # Enriched rows only: press is None when the page failed, so no false "no news"
    no_news = [r for r in rows if r.get("press") == []]
    if no_news:
        L.append(f"- 📰 **No press release since last close ({len(no_news)}):** " +
                 ", ".join(r["ticker"] for r in no_news))

    if not (micro or squeeze or rotated or countries or no_news):
        L.append("- *No structural flags triggered.*")

    start = last_close(now_et)
    L += ["", "## 📰 After-Hours & Premarket Press Releases", "",
          f"*Wire-service releases from {start:%a %b %-d} 16:00 ET to "
          f"{now_et:%H:%M} ET, for the top {ENRICH_TOP_N} gappers.*", ""]
    any_pr = False
    for r in rows[:ENRICH_TOP_N]:
        if r.get("press"):
            L += pr_lines(r["ticker"], r["press"])
            any_pr = True
    if not any_pr:
        L.append("- *None found.*")
    failed = [r["ticker"] for r in rows[:ENRICH_TOP_N] if r.get("press") is None]
    if failed:
        L.append(f"- *News not checked (Finviz page failed): {', '.join(failed)}*")

    danger, clean, ssr_on = flags["danger"], flags["clean"], flags["ssr_on"]

    L += ["", "## ⚠️ Trust Check — *can they dump into your spike?*", ""]
    if danger:
        L.append("**Dilution risk — treat rallies as exit liquidity:**")
        for r in danger:
            L.append(f"- **{r['ticker']}** ({r['dilution_risk']}): "
                     f"{r.get('dilution_flags','')}")
        L.append("")
    if clean:
        L.append("**Clean filers — no shelf, no recent offering:** " +
                 ", ".join(f"**{r['ticker']}**" for r in clean))
        L.append("")
    if ssr_on:
        L.append("**🩳 Under SSR today** (shorts can't hit the bid — helps a squeeze): " +
                 ", ".join(r["ticker"] for r in ssr_on))
        L.append("")
    L.append("*Borrow availability / easy-to-borrow is broker-gated and not in "
             "this report — check your platform before assuming a squeeze.*")
    L.append("")

    md = "\n".join(L)
    if dry:
        print("\n" + md)
        return None

    DAILY.mkdir(parents=True, exist_ok=True)
    out = DAILY / f"Premarket_Report_{date_str}.md"
    if out.exists():
        # Never clobber a report someone may have annotated.
        out = DAILY / f"Premarket_Report_{date_str}-auto.md"
    out.write_text(md)
    print(f"\n✓ Report: {out}")
    return out


def esc(v):
    """Escape for Telegram's HTML parse mode. Company names carry & and < often enough."""
    return html_escape(str(v), quote=False)


MEDAL = {1: "🥇", 2: "🥈", 3: "🥉"}


def _hook(rows, flags):
    """One line at the top: the most interesting thing about this morning."""
    rot = max(rows, key=lambda r: r.get("float_rotation") or 0, default=None)
    if rot and (rot.get("float_rotation") or 0) >= 5:
        return (f"🌀 <b>{esc(rot['ticker'])}</b> turned its float "
                f"<b>{rot['float_rotation']:.0f}x</b> before the bell")
    if flags["micro"]:
        m = min(flags["micro"], key=lambda r: r.get("float_shares") or 9e9)
        return (f"🐜 <b>{esc(m['ticker'])}</b> is trading on a "
                f"<b>{fmt(m['float_shares'], 'sh')}</b> float")
    top = rows[0] if rows else None
    if top:
        return (f"🚀 <b>{esc(top['ticker'])}</b> leads at "
                f"<b>+{top['gap_pct']:.0f}%</b>")
    return None


def build_telegram_message(rows, now_ct, ctx=None, events=None):
    """
    The morning digest, as Telegram HTML.

    Telegram renders no tables, so the report's 14-column grid is reshaped into
    two lines per ticker. The full detail stays in the markdown report -- this
    is a heads-up on a phone, not a replacement for it.

    Written to be read in a feed: a hook, the market mood, then a ranked board.
    Emoji carry meaning and never decorate -- a medal is a rank, a flame is
    rotation above 5x, a bug is a sub-2M float. The warnings are never dressed
    up or dropped to make room.
    """
    now_et = now_ct.astimezone(ET)
    flags = flag_sets(rows)

    head = [f"☀️ <b>PREMARKET</b> · {now_ct:%a %b %-d}",
            f"<i>{now_et:%H:%M} ET · {len(rows)} gappers cleared the screen</i>"]
    hook = _hook(rows, flags)
    if hook:
        head += ["", hook]
    if ctx:
        head += [""] + market.telegram_block(ctx, esc)
    head += ["", cal.telegram_day(events, "📅 <b>ON THE CALENDAR TODAY</b>")]
    head += ["", "━━━━━━━━━━━━━━", ""]

    body = []
    for i, r in enumerate(rows[:TG_TOP_N], 1):
        rot = r.get("float_rotation") or 0
        tag = MEDAL.get(i, f"<b>{i}.</b>")
        heat = " 🔥" if rot >= 5 else (" ⭐" if rot >= 1 else "")
        try:
            price = f"${float(r['pm_price']):.2f}"
        except (TypeError, ValueError, KeyError):
            price = "—"   # --telegram-only: the CSV log has no pm_price column
        body.append(f"{tag} <b>{esc(r['ticker'])}</b>{heat} "
                    f"+{r['gap_pct']:.0f}% · {price}")
        bits = [f"{fmt(r.get('float_shares'), 'sh')} float",
                f"{fmt(r.get('float_rotation'), 'x')} rot"]
        badge = RISK_BADGE.get(r.get("dilution_risk"))
        if badge:
            bits.append(badge)
        body.append("   <i>" + esc(" · ".join(bits)) + "</i>")
        body.append("")   # a blank line between tickers

    tail = []
    if flags["micro"]:
        tail.append("🐜 <b>Sub-2M float:</b> " + esc(", ".join(
            f"{r['ticker']} ({fmt(r['float_shares'], 'sh')})" for r in flags["micro"])))
    if flags["squeeze"]:
        tail.append("")
        tail.append("🩳 <b>Short &gt;15%:</b> " + esc(", ".join(
            f"{r['ticker']} ({r['short_float_pct']:.0f}%)" for r in flags["squeeze"])))
    if flags["rotated"]:
        tail.append("")
        tail.append("🌀 <b>Float rotated &gt;1x:</b> " + esc(", ".join(
            f"{r['ticker']} ({r['float_rotation']:.1f}x)" for r in flags["rotated"])))
    if flags["ssr_on"]:
        tail.append("")
        tail.append("🛑 <b>Under SSR:</b> " +
                    esc(", ".join(r["ticker"] for r in flags["ssr_on"])))

    if flags["danger"]:
        tail += ["", "━━━━━━━━━━━━━━", "", "⚠️ <b>They can sell into your spike</b>"]
        for r in flags["danger"]:
            tail.append(f"• <b>{esc(r['ticker'])}</b> ({esc(r['dilution_risk'])}): "
                        f"{esc(r.get('dilution_flags') or 'no detail')}")
    if flags["clean"]:
        tail += ["", "✅ <b>Clean filers:</b> " +
                 esc(", ".join(r["ticker"] for r in flags["clean"]))]
    tail += ["", "<i>Not advice. Check the borrow before you assume a squeeze.</i>"]

    # No trimming: a long digest goes out in parts, split on line boundaries by
    # notify.send(split=True) -- several posts rather than lost lines.
    return "\n".join(head + body + tail)


def post_telegram(rows, now_ct, ctx=None, events=None, dry=False):
    """Send the digest. Delivery itself lives in notify.py, shared with the
    other reports."""
    notify.send(build_telegram_message(rows, now_ct, ctx, events), dry=dry,
                split=True)


# Every column the report formats as a number. CSV values come back as strings,
# and a missed one raises "Unknown format code 'f' for object of type 'str'".
CSV_NUMERIC = ("gap_pct", "pm_price", "pm_volume", "float_shares",
               "short_float_pct", "float_rotation", "shelf_count",
               "rel_volume", "rs_ratio")


def rows_from_csv(date_str):
    """
    Rebuild a day's rows from the CSV log, for --telegram-only.

    Lets a post be retried without re-scraping Finviz. CSV values come back as
    strings, so the numeric columns the digest formats are coerced here.
    """
    if not CSV_PATH.exists():
        return []
    out = []
    with open(CSV_PATH, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("date") != date_str:
                continue
            for k in CSV_NUMERIC:
                v = (row.get(k) or "").strip()
                try:
                    row[k] = float(v) if v else None
                except ValueError:
                    row[k] = None
            out.append(row)
    out.sort(key=lambda r: r.get("gap_pct") or 0, reverse=True)
    return out


def append_csv(rows, now_ct, dry=False):
    if dry or not rows:
        return
    date_str = now_ct.strftime("%Y-%m-%d")
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CSV_PATH.exists() or not CSV_PATH.stat().st_size:
        # First run: start the log with its header.
        with open(CSV_PATH, "w", newline="") as f:
            csv.writer(f).writerow(CSV_HEADER)
    with open(CSV_PATH) as f:
        header = next(csv.reader(f))
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        for r in rows:
            w.writerow({**r, "date": date_str, "pm_volume": r.get("pm_volume")})
    print(f"✓ Logged {len(rows)} rows -> {CSV_PATH}")


def todays_calendar(now_ct, rows=None):
    """
    What is scheduled to hit the tape today, with the morning's gappers passed
    in so a name already on the screen that reports today is flagged as the
    catalyst it is rather than filtered out for being small.

    Never fatal: an unreachable calendar costs a section, not the report.
    Returns None on failure rather than an empty list, because "nothing is
    scheduled today" and "the feed did not answer" are different facts and the
    report says which one it is.
    """
    try:
        return cal.day_events(now_ct.date(),
                              watchlist=[r["ticker"] for r in rows or []])
    except Exception as e:
        print(f"  ! calendar unavailable: {e}", file=sys.stderr)
        return None


def main():
    dry = "--dry-run" in sys.argv
    tg_only = "--telegram-only" in sys.argv

    now_ct = datetime.now(LOCAL)
    print(f"=== Premarket report {now_ct:%Y-%m-%d %H:%M %Z} ===")

    if tg_only:
        # Re-post from the CSV log: no scraping, and the report file is untouched.
        rows = rows_from_csv(now_ct.strftime("%Y-%m-%d"))
        if not rows:
            print("No rows logged for today.", file=sys.stderr)
            return 1
        print(f"  {len(rows)} rows from {CSV_PATH.name}")
        post_telegram(rows, now_ct, market.context(),
                      todays_calendar(now_ct, rows), dry=dry)
        return 0

    rows = build_rows()
    if not rows:
        print("No candidates found.", file=sys.stderr)
        return 1
    print("Fetching market context...")
    ctx = market.context()
    print("Fetching today's calendar...")
    events = todays_calendar(now_ct, rows)
    print(f"  {len(events)} scheduled events clear the bar"
          if events is not None else "  ! calendar unavailable")
    out = write_report(rows, now_ct, ctx, events, dry=dry)
    append_csv(rows, now_ct, dry)
    if not dry:
        store.record_report("premarket", now_ct.strftime("%Y-%m-%d"), ctx,
                            rows=rows, note_path=str(out) if out else None)
        # What the market expects, recorded before the open, so tonight's close
        # is judged against this morning's number rather than a revised one.
        store.record_calendar(now_ct.strftime("%Y-%m-%d"), events or [])
    # Last, and never fatal: the report is already safely on disk.
    post_telegram(rows, now_ct, ctx, events, dry=dry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
