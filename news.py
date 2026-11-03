#!/usr/bin/env python3
"""
Market headlines for the `/news` command: what is being reported right now.

Two modes, because "what is the market doing" and "what is this company doing"
are different questions:

    news.headlines()        the market -- newest first, across several desks
    news.ticker(t)          one company -- its own announcements first

> Ranked by **recency, not importance**: the command answers "the 5 or 10 most
> recent headlines", so the ordering is the clock and nothing else. That is the
> opposite of `calendar_events.py`, which ranks by significance because a
> scheduled event's importance is knowable in advance and a breaking headline's
> is not.

RSS is parsed through `market.feed_items`.

> [!warning] The obvious feeds are the stale ones
> CNBC's **Markets** and **Earnings** feeds sound exactly right for this and
> are unusable: measured 2026-09-21, their median item was **107 and 517 hours
> old**, with the newest in Earnings already five days stale. MarketWatch's
> "realtime headlines" feed was worse -- its newest item was over a year old.
> Freshness was measured per feed before any of them were chosen, and the three
> market-only sources below ran at a 5-8 hour median.

Stdlib only.
"""

import re
import sys
from datetime import datetime, timedelta
from html import escape as html_escape
from zoneinfo import ZoneInfo

import market

ET = ZoneInfo("America/New_York")

# (label, url, market_only). `market_only` marks a desk that carries nothing
# but market and company news, so its items need no filtering at all.
NEWS_FEEDS = [
    ("Seeking Alpha", "https://seekingalpha.com/market_currents.xml", True),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories", True),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_bulletins", True),
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html", False),
    ("CNBC", "https://www.cnbc.com/id/10000664/device/rss/rss.html", False),
]

MAX_AGE_HOURS = 48      # older than this is not "recent" by any reading
DEFAULT_N = 5
MAX_N = 15

# Applied only to the general-news desks, which are business channels carrying
# a minority of politics and human interest. A **droplist** rather than an
# allowlist, because the default there is already business: on the day this was
# written CNBC's Economy desk led with French wine production and its Top News
# desk with a dinner guest list, while genuinely market-moving items ("Jamie
# Dimon says hyperscaler AI spending could hit $1 trillion") carry no keyword a
# reasonable allowlist would have held. Naming the noise is the smaller and
# more honest list; extend it when something slips through.
NOISE = re.compile(
    r"\b(election|midterm|campaign trail|senator|congressman|congresswoman|"
    r"impeach|indict|pardon|dinner|wedding|royal|celebrity|recipe|"
    r"vacation|travel guide|holiday gift|horoscope|obituary|"
    r"super bowl|nfl|nba|world cup|olympic|movie review|box office)\b", re.I)


def _norm(title):
    """A title reduced to what two feeds would agree on, for deduplication."""
    return re.sub(r"[^a-z0-9]", "", (title or "").lower())[:60]


def _age(dt, now=None):
    """'3h ago' / '45m ago' / '2d ago'."""
    secs = ((now or datetime.now(ET)) - dt.astimezone(ET)).total_seconds()
    if secs < 90 * 60:
        return f"{max(1, int(secs // 60))}m ago"
    if secs < 36 * 3600:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def headlines(limit=DEFAULT_N, hours=MAX_AGE_HOURS):
    """
    The most recent market headlines, newest first, deduplicated across feeds.

    A dead feed costs its own items and nothing else -- `market.feed_items`
    returns an empty list rather than raising, so the command still answers
    from whatever is reachable.
    """
    now = datetime.now(ET)
    cutoff = now - timedelta(hours=hours)
    seen, out = set(), []
    for source, url, market_only in NEWS_FEEDS:
        for n in market.feed_items(source, url):
            dt = n["dt"].astimezone(ET)
            if dt < cutoff or dt > now + timedelta(hours=2):
                # Future stamps happen; a feed an hour ahead is a clock
                # problem, a feed a day ahead is wrong and is dropped.
                continue
            if not market_only and NOISE.search(n["title"]):
                continue
            key = _norm(n["title"])
            if not key or key in seen:
                continue
            seen.add(key)
            out.append({**n, "dt": dt})
    out.sort(key=lambda n: n["dt"], reverse=True)
    return out[:limit]


# Recency buckets, in days: today's headlines are preferred over this week's,
# and this week's over this month's. Today is its own
# bucket by calendar date rather than by 24 hours, because "today's news" means
# today's session, not the last twenty-four hours.
BUCKETS = ((None, "today"), (7, "this week"), (30, "this month"))
STALE_AFTER_DAYS = 3    # newest item older than this earns a warning


def age_bucket(dt, now=None):
    """0 today, 1 this week, 2 this month, 3 older."""
    now = now or datetime.now(ET)
    dt = dt.astimezone(ET)
    if dt.date() == now.date():
        return 0
    days = (now - dt).total_seconds() / 86400
    for i, (limit, _) in enumerate(BUCKETS):
        if limit is not None and days <= limit:
            return i
    return len(BUCKETS)


def ticker(sym, limit=DEFAULT_N):
    """
    Headlines for one company: newest first, but never at the cost of showing
    something that is not about the company at all.

    Two competing requirements, resolved by ordering the keys rather than
    picking a winner:

      * **Recency leads.** Today's headlines outrank the week's, the week's
        outrank the month's.
      * **Relevance breaks ties.** Inside a bucket the company's own
        announcements come first, then headlines naming it, then commentary
        that merely mentions it -- which is still tagged as such, because a
        ticker's Finviz page carries plenty of it. Newest-first *alone* once
        put an Autodesk story at the top of ADBE's page; bucketing keeps that
        fix while letting fresh news win.

    `fundamentals.recent_news()` is asked for more rows than are wanted so
    there is something to re-rank -- it sorts relevance-first, which is the
    right answer for the single catalyst headline `/fundamentals` needs and
    the wrong one here.
    """
    import fundamentals
    import quote

    sym = sym.upper().strip()
    q = quote.get(sym) or {}
    rows = fundamentals.recent_news(sym, q.get("name", ""), limit=limit * 5)
    now = datetime.now(ET)
    rows.sort(key=lambda n: (age_bucket(n["dt"], now),
                             0 if n.get("is_company_news") else 1,
                             -n["dt"].timestamp()))
    return q, rows[:limit]


# --- Rendering --------------------------------------------------------------

def esc(v):
    return html_escape(str(v), quote=False)


def _line(i, n, now=None):
    title = esc(n["title"])
    link = f'<a href="{esc(n["url"])}">{title}</a>' if n.get("url") else title
    return (f"<b>{i}.</b> {link}\n"
            f"   <i>{esc(n['source'])} · {esc(_age(n['dt'], now))}</i>")


def telegram_headlines(items, hours=MAX_AGE_HOURS):
    """The /news digest."""
    now = datetime.now(ET)
    if not items:
        return ("📰 <b>MARKET NEWS</b>\n\n<i>No headlines in the last "
                f"{hours}h — every feed came back empty, which usually means a "
                "network problem rather than a quiet news day.</i>")
    L = [f"📰 <b>MARKET NEWS</b> · {now:%H:%M} ET", ""]
    for i, n in enumerate(items, 1):
        L.append(_line(i, n, now))
    L += ["", "<i>Most recent first, across MarketWatch, Seeking Alpha and "
              "CNBC. Not advice.</i>"]
    return "\n".join(L)


def telegram_ticker(sym, q, rows):
    """The /news TICKER reply."""
    if not rows:
        return (f"📭 No recent headlines found for <b>{esc(sym)}</b>.")
    now = datetime.now(ET)
    head = f"📰 <b>{esc(sym)}</b>"
    if q.get("name") and q["name"].upper() != sym:
        head += f" — {esc(q['name'])}"
    if q.get("price") is not None:
        chg = q.get("change_pct")
        move = f" ({chg:+.1f}%)" if chg is not None else ""
        head += f"\n<i>${q['price']:,.2f}{move}</i>"
    # Say so when there is nothing from today, rather than letting a week-old
    # headline at the top of the list imply there is.
    newest = max(n["dt"] for n in rows)
    if age_bucket(newest, now) > 0:
        L = [head, "", f"<i>Nothing today — newest is {esc(_age(newest, now))}.</i>", ""]
        if (now - newest.astimezone(ET)).days >= STALE_AFTER_DAYS:
            L[2] = (f"⚠️ <i>Quiet name — newest headline is "
                    f"{esc(_age(newest, now))}.</i>")
    else:
        L = [head, ""]

    for i, n in enumerate(rows, 1):
        title = esc(n["title"])
        link = f'<a href="{esc(n["url"])}">{title}</a>' if n.get("url") else title
        tag = "" if n.get("is_company_news") else " · <i>mentions only</i>"
        L.append(f"<b>{i}.</b> {link}\n"
                 f"   <i>{esc(n.get('source') or '?')} · "
                 f"{esc(_age(n['dt'], now))}</i>{tag}")
    L += ["", "<i>Newest first. Within a day, the company's own announcements "
              "lead. Not advice.</i>"]
    return "\n".join(L)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args and not args[0].isdigit():
        sym = args[0].upper()
        q, rows = ticker(sym, int(args[1]) if len(args) > 1 else DEFAULT_N)
        print(telegram_ticker(sym, q, rows))
        return 0
    n = int(args[0]) if args else DEFAULT_N
    print(telegram_headlines(headlines(n)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
