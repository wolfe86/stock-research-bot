#!/usr/bin/env python3
"""
Economic and earnings calendar: what is scheduled, what the market expects,
and -- afterwards -- what actually printed.

Two consumers, one ranking:
  bot.py                 /calendar -- the week's most significant events
  premarket_report.py    what lands today, before the bell

> Scope: CPI, jobs, rates and the other releases that move the whole market
> -- an **allowlist**, not a filter. An event scores or it does not exist.
> Nasdaq publishes ~20 US economic events a day -- CFTC positioning, EIA
> sub-series, mortgage sub-indices, Redbook -- and a report that prints all
> of them is one nobody reads.
>   - **Tier-2 fillers:** ADP, regional Fed surveys, Treasury auctions and bill
>     announcements, M2. They fill out a day, they never lead it.
>   - **Fed speakers are dropped, except the Chair and Vice Chair.** See
>     `FED_PRINCIPAL`.
>   - **Earnings: only the reporters worth knowing about.** Listing every
>     reporter buried the economic releases under a dozen unknown names. A
>     reporter is listed only if `cared_for()` says so: a large, known name
>     (CARE_FLOOR), today's gapper screen, a ticker on CALENDAR_WATCHLIST in
>     local_settings.py, or the alerts focus watchlist. Market cap decides
>     where a listed name sorts.

Source: Nasdaq's public calendar API. No key, no account, stdlib only, which
is what the unattended systemd runs need.

    /api/calendar/economicevents?date=  event, time, consensus, previous, actual
    /api/calendar/earnings?date=        ticker, session, market cap, consensus
                                        EPS, and after the fact actual + surprise

> [!warning] Two quirks in the economic feed, both verified
> **1. The `date` parameter is off by one.** `date=2026-09-19` returns the
> events of **18 September**. Confirmed three ways: Initial Jobless Claims
> (always Thursday) lands on the Friday query, and the FOMC decisions returned
> by the 2026-10-29 and 2026-12-10 queries are the Fed's published 28 October
> and 9 December meeting dates. `ECON_QUERY_OFFSET` is that correction, and
> `--check` re-verifies it against the weekday anchors.
>
> **2. The `gmt` field is not GMT.** It is a fixed UTC-4 -- Eastern *daylight*
> time applied year round. The Fed decision, which is always 14:00 ET, prints
> as `14:00` in October and `15:00` in December; only a fixed UTC-4 explains
> both. Read as GMT the times would be four hours out; read as ET they are
> correct until November and then an hour late, which is the kind of error
> nobody notices until they miss a print. `FEED_TZ` is that offset, and every
> time is converted to ET before it is shown.

The earnings feed has neither quirk: `date=` means that date, and the session
is carried as a flag rather than a time.
"""

import json
import math
import re
import sys
from datetime import date, datetime, time as dtime, timedelta, timezone
from html import escape as html_escape
from zoneinfo import ZoneInfo

import cache
import market
import settings

ET = ZoneInfo("America/New_York")

ECON_URL = "https://api.nasdaq.com/api/calendar/economicevents?date={day}"
EARN_URL = "https://api.nasdaq.com/api/calendar/earnings?date={day}"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json"}

# See the module docstring: the economic feed answers with the day *before* the
# one asked for, and stamps its times at a fixed UTC-4 rather than GMT.
ECON_QUERY_OFFSET = timedelta(days=1)
FEED_TZ = timezone(timedelta(hours=-4))

# A consensus a week out does not move; today's actuals do. The scheduled
# reports run once, so the short TTL costs one fetch and keeps the close
# honest.
TTL_AHEAD = 6 * 3600
TTL_TODAY = 600

COUNTRY = "United States"

# The floor for a name worth knowing about, and the same one market.py uses
# for the large-cap movers -- one threshold in the project, not two. AMD, KO
# and WM all clear it; nothing a reader would have to look up does.
LARGE_CAP_FLOOR = market.LARGE_CAP_FLOOR

TOP_N = 5                  # events in the /calendar digest
MAX_EARNINGS_PER_DAY = 3   # before the watchlist exceptions are added
TIER1 = 60                 # at or above this an event always earns its line


# --- What counts, and what it is worth -------------------------------------
#
# Ordered: the first pattern to match wins, so `Core CPI` is scored before the
# rule for `CPI` can claim it. Anything unmatched scores nothing and is
# dropped -- the allowlist is the whole filter.
#
# `family` collapses one real-world release that the feed splits into several
# rows. The employment report arrives as Nonfarm Payrolls, Unemployment Rate
# and Average Hourly Earnings at the same 08:30; it is one event and gets one
# line, the highest-scoring member of its family that day.

ECON_RULES = [
    (r"\bfomc\s+statement\b",                      "fed_decision", 100, "FOMC statement"),
    (r"\binterest\s+rate\s+decision\b",            "fed_decision", 100, "Fed rate decision"),
    (r"\bfomc\s+press\s+conference\b",             "fed_decision",  95, "Fed press conference"),
    (r"\bcore\s+cpi\b",                            "cpi",           90, None),
    (r"\bcpi\b|\bconsumer\s+price\s+index\b",      "cpi",           88, None),
    (r"\bnonfarm\s+payrolls?\b",                   "jobs_report",   88, "Nonfarm Payrolls"),
    (r"\bcore\s+pce\b",                            "pce",           85, None),
    (r"\bpce\s+price\s+index\b",                   "pce",           83, None),
    (r"\bunemployment\s+rate\b",                   "jobs_report",   80, None),
    (r"\baverage\s+hourly\s+earnings\b",           "jobs_report",   78, None),
    (r"\bcore\s+ppi\b",                            "ppi",           74, None),
    (r"\bppi\b|\bproducer\s+price\s+index\b",      "ppi",           73, None),
    (r"\bcore\s+retail\s+sales\b",                 "retail",        71, None),
    (r"\bretail\s+sales\b",                        "retail",        72, None),
    (r"\bgdp\b",                                   "gdp",           70, None),
    # "PMI" required on purpose: the feed also carries the ISM sub-indices
    # (Employment, New Orders, Prices), and a sub-index scoring the same as the
    # headline would win the family on a coin toss and report the wrong number.
    (r"\bism\s+manufacturing\s+pmi\b",             "ism_mfg",       66, None),
    (r"\bism\s+(non-?manufacturing|services)\s+pmi\b", "ism_svc",   65, None),
    (r"\bfomc\s+(meeting\s+)?minutes\b",           "fed_minutes",   62, "FOMC minutes"),
    # Below TIER1: real numbers, but not ones to lead a day with. They fill a
    # day that has nothing above them, one at a time.
    (r"\binitial\s+jobless\s+claims\b",            "claims",        40, None),
    (r"\bconsumer\s+confidence\b",                 "confidence",    36, None),
    (r"\bmichigan\s+consumer\s+sentiment\b",       "sentiment",     35, None),
    (r"\bcore\s+durable\s+goods\s+orders\b",       "durables",      31, None),
    (r"\bdurable\s+goods\s+orders\b",              "durables",      30, None),
    (r"\bhousing\s+starts\b",                      "housing",       30, None),
    (r"\bbuilding\s+permits\b",                    "housing",       29, None),
    # Tier-2: these fill out the day, they never lead it.
    (r"\badp\s+employment\b|\badp\s+nonfarm\b",    "adp",           45, None),
    (r"\brichmond\s+(manufacturing|services)\b",   "richmond",      28, None),
    (r"\bphiladelphia\s+fed\b",                    "philly_fed",    28, None),
    (r"\bempire\s+state\b",                        "empire",        28, None),
    (r"\b(kc|kansas\s+city|dallas|chicago)\s+fed\b", "regional_fed", 26, None),
    (r"\b\d+-(year|month)\s+(note|bond)\s+auction\b", "auction",    25, None),
    (r"\bm2\b|\bmoney\s+supply\b",                 "money_supply",  22, None),
    (r"\b\d+-week\s+bill\s+(announcement|auction)\b", "bills",      20, None),
]
ECON_RULES = [(re.compile(p, re.I), fam, sc, lbl) for p, fam, sc, lbl in ECON_RULES]

# Checked before the rules, so a speaker or a survey can never be mistaken for
# the thing it is named after: "FOMC Member Williams Speaks" must not score as
# an FOMC event, and "Atlanta Fed GDPNow" is a nowcast, not the GDP release.
#
# The second group are companions the feed publishes alongside a headline
# release. They are real numbers, but they are not *the* number: GDP Sales is
# final sales and GDP Price Index is the deflator, neither of which is the GDP
# print the market trades. One line per release means picking the headline.
ECON_DROP = re.compile(
    r"cftc|redbook|baker hughes|api weekly|gdpnow|"
    r"balance sheet|mortgage|refinanc|reserve balances|"
    r"leading index|current account|"
    r"private nonfarm payrolls|government payrolls|manufacturing payrolls|"
    r"u6 unemployment|gdp price index|gdp sales", re.I)

# Fed speakers are dropped, with one mechanical exception: the Chair and the
# Vice Chair.
#
# Whether a given speech is significant is a judgement, and guessing it would
# put fiction in the report, so the exception is a name test, which can be
# checked.
#
# > [!warning] This keys off how the feed *labels* the row, not who holds the office
# > On 2026-09-22 Nasdaq published Vice Chair Philip Jefferson as "Fed Governor
# > Jefferson Speaks", so he did not match and was dropped. If the Vice Chair
# > matters regardless of Nasdaq's wording, this needs a name list instead.
FED_PRINCIPAL = re.compile(
    r"\b(fed(eral\s+reserve)?\s+chair(man|woman)?|vice\s+chair|powell)\b", re.I)
SPEAKER = re.compile(r"speaks|speech", re.I)

# An earnings date on a name already on the morning's gapper screen is a live
# catalyst on something already being watched, so it outranks its size.
GAPPER_BONUS = 15

# Weekly releases whose weekday is fixed, used by --check to prove the feed's
# date offset has not changed underneath us.
WEEKDAY_ANCHORS = {"Initial Jobless Claims": 3}   # Thursday


def _clean(v):
    """Feed values carry HTML entities and use '&nbsp;' for 'nothing yet'."""
    if v is None:
        return None
    s = re.sub(r"&[a-z]+;|&#\d+;", " ", str(v).replace("&amp;", "&"))
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # The earnings feed writes "N/A" where a company has no year-ago figure --
    # a newly listed name, or one that changed its fiscal calendar. That is an
    # absent value, not a value, and printing it as one is worse than a dash.
    return None if s.upper() in ("", "N/A", "-", "--") else s


def _val(s):
    """'196K' / '1,730K' / '0.3%' / '($1.08)' / '$54.78' -> float, or None."""
    if s is None:
        return None
    s = str(s).strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "").replace("%", "").replace("$", "").strip()
    mult = 1.0
    if s and s[-1] in "KMBT":
        mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[s[-1]]
        s = s[:-1]
    try:
        v = float(s) * mult
    except ValueError:
        return None
    return -v if neg else v


def _fetch(url, day):
    """
    Cached GET returning the feed's `rows`, or **None** when it could not be read.

    > [!warning] `[]` and `None` are different answers and must stay that way
    > `[]` is "the feed answered and nothing is scheduled". `None` is "the feed
    > did not answer". Until 2026-09-22 this returned `[]` for both, so a dead
    > Nasdaq printed *"Nothing scheduled today."* -- a confident claim about a
    > day nobody had checked. Found when the 09-22 report said exactly that on
    > a morning carrying ADP, Richmond Fed, an auction and seven reporters.
    """
    ttl = TTL_AHEAD if day > date.today() else TTL_TODAY
    try:
        body = cache.fetch(url, ttl, headers=HEADERS, timeout=25)
        data = (json.loads(body) or {}).get("data") or {}
        return data.get("rows") or []
    except Exception as e:
        print(f"  ! calendar fetch failed ({url.split('?')[0]}, {day}): {e}",
              file=sys.stderr)
        return None


def _score_econ(name):
    """(family, score, label) for an event name, or None when it does not count."""
    if not name:
        return None
    # Speakers are decided first, so "FOMC Member Williams Speaks" can never
    # fall through to the `fomc` rules and score as an FOMC event.
    if SPEAKER.search(name):
        return ("fed_speaker", 55, name) if FED_PRINCIPAL.search(name) else None
    if ECON_DROP.search(name):
        return None
    for rx, family, score, label in ECON_RULES:
        if rx.search(name):
            return family, score, label or name
    return None


def _when(day, hhmm):
    """The feed's fixed-UTC-4 stamp as a real ET datetime. None when unparseable."""
    if not hhmm:
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})$", hhmm.strip())
    if not m:
        return None
    try:
        naive = datetime.combine(day, dtime(int(m.group(1)), int(m.group(2))))
    except ValueError:
        return None
    return naive.replace(tzinfo=FEED_TZ).astimezone(ET)


def economic(day):
    """
    The scored US economic events for one calendar day, best first.

    One line per real release: where the feed splits a release into several
    rows, or repeats one in two units (Building Permits in millions and again
    as a percentage), the highest-scoring row wins and the rest are dropped.
    """
    rows = _fetch(ECON_URL.format(day=day + ECON_QUERY_OFFSET), day)
    if rows is None:
        return None
    best = {}
    for r in rows:
        if _clean(r.get("country")) != COUNTRY:
            continue
        name = _clean(r.get("eventName"))
        scored = _score_econ(name)
        if not scored:
            continue
        family, score, label = scored
        ev = {
            "kind": "econ", "date": day, "family": family, "score": float(score),
            "label": label, "when": _when(day, _clean(r.get("gmt"))),
            "expected": _clean(r.get("consensus")),
            "previous": _clean(r.get("previous")),
            "actual": _clean(r.get("actual")),
            "ticker": None, "session": None, "mcap": None, "surprise": None,
        }
        # A row carrying a consensus is the one the desk quotes; prefer it when
        # two rows of a family tie on score.
        rank = (ev["score"], 1 if ev["expected"] else 0)
        if family not in best or rank > best[family][0]:
            best[family] = (rank, ev)
    out = [ev for _, ev in best.values()]
    out.sort(key=lambda e: (-e["score"], e["when"] or datetime.max.replace(tzinfo=ET)))
    return out


SESSION = {"time-pre-market": "bmo", "time-after-hours": "amc"}
SESSION_WORD = {"bmo": "before open", "amc": "after close", None: "time TBD"}


EARN_BASE = 20.0           # a reporter with no size signal still earns its line


def _earn_score(mcap):
    """
    $50B -> 75, $1T -> 85 for the names big enough to move the tape; below the
    large-cap floor, $1M -> 20 rising to 50 as the floor is approached.

    > Size decides where a reporter sorts, not whether it is listed (that is
    > cared_for()). Returning `None` under `LARGE_CAP_FLOOR` once dropped AZO,
    > the week's top reporter, $3.4B short of a $50B floor. Nothing below the
    > floor can outrank a tier-1 economic release.
    """
    if not mcap:
        return EARN_BASE
    if mcap < LARGE_CAP_FLOOR:
        span = math.log10(LARGE_CAP_FLOOR / 1e6)
        grown = math.log10(max(mcap, 1e6) / 1e6)
        return max(EARN_BASE, min(50.0, EARN_BASE + 30.0 * grown / span))
    span = math.log10(1e12 / LARGE_CAP_FLOOR)
    return max(75.0, min(85.0, 75.0 + 10.0 * math.log10(mcap / LARGE_CAP_FLOOR) / span))


# A name big enough to be widely known: AMD, Coca-Cola, Waste Management and
# AutoZone (~$47B) all clear $40B.
CARE_FLOOR = 40e9


def _researched():
    """Tickers on CALENDAR_WATCHLIST (local_settings.py)."""
    return {str(t).upper() for t in settings.CALENDAR_WATCHLIST}


def _focus_watch():
    """The alerts focus ticker watchlist (FOCUS_EDGAR_TICKERS), if it can be read."""
    try:
        import market_alerts_sources as MA
        return set(MA.FOCUS.EDGAR_TICKERS)
    except Exception:
        return set()


def cared_for(ev, researched=None, focus=None):
    """
    Is this reporter worth a line?

    Yes if it is a large known name, on today's gapper screen, on
    CALENDAR_WATCHLIST, or on the alerts focus watchlist. Anything else is a
    name the reader does not know and does not trade.
    """
    if ev.get("on_watchlist"):
        return True
    if (ev.get("mcap") or 0) >= CARE_FLOOR:
        return True
    t = ev.get("ticker")
    researched = _researched() if researched is None else researched
    focus = _focus_watch() if focus is None else focus
    return t in researched or t in focus


def earnings(day, watchlist=()):
    """
    The day's reporters worth a line -- see cared_for(): the big names,
    today's gapper screen, CALENDAR_WATCHLIST and the alerts focus watchlist.

    `watchlist` is the tickers screened into today's report: a small name on
    the morning's screen that reports today is listed whatever its size.
    Nothing else small gets through: "significant news" on a micro cap is a
    judgement, and guessing it would put fiction in the report.
    """
    watch = {t.upper() for t in watchlist or ()}
    rows = _fetch(EARN_URL.format(day=day), day)
    if rows is None:
        return None
    out = []
    for r in rows:
        tick = (_clean(r.get("symbol")) or "").upper()
        if not tick:
            continue
        mcap = _val(_clean(r.get("marketCap")))
        score = _earn_score(mcap)
        on_watch = tick in watch
        session = SESSION.get(_clean(r.get("time")) or "")
        out.append({
            "kind": "earnings", "date": day, "family": f"earn:{tick}",
            "score": score + (GAPPER_BONUS if on_watch else 0),
            "label": tick, "name": _clean(r.get("name")), "when": None,
            "expected": _clean(r.get("epsForecast")),
            "previous": _clean(r.get("lastYearEPS")),
            "actual": _clean(r.get("eps")),
            "surprise": _clean(r.get("surprise")),
            "ticker": tick, "session": session, "mcap": mcap,
            "on_watchlist": on_watch,
        })
    # Only the reporters worth a line -- see cared_for().
    researched, focus = _researched(), _focus_watch()
    out = [e for e in out if cared_for(e, researched, focus)]
    return sorted(out, key=lambda e: (-e["score"], e["ticker"]))


def day_events(day, watchlist=()):
    """
    Everything worth a line on one day, best first.

    Every scored economic event earns its line, and so does every reporter.
    `None` when either feed could not be read -- see `_fetch`.

    > This once kept tier-1 events plus *at most one* tier-2 number. With the
    > tier-2 allowlist widened that cap hid most of a busy day, so it is gone.
    > `TIER1` still ranks the weekly `/calendar` digest, which is
    > length-limited; the markdown report is not.
    """
    econ = economic(day)
    earn = earnings(day, watchlist)
    if econ is None or earn is None:
        return None
    return sorted(econ + earn, key=lambda e: -e["score"])


def week(start=None, days=7, watchlist=()):
    """[(date, [events])] from `start`, weekends included but usually empty."""
    start = start or date.today()
    return [(start + timedelta(days=i),
             day_events(start + timedelta(days=i), watchlist))
            for i in range(days)]


def top(weekly, n=TOP_N):
    """The n most significant events of the week, in date order once chosen."""
    flat = [e for _, evs in weekly for e in (evs or [])]
    flat.sort(key=lambda e: -e["score"])
    return sorted(flat[:n], key=lambda e: (e["date"],
                                           -e["score"]))


# --- What the market expects from the Fed ----------------------------------

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
FF_SYMBOLS = ["@FF.1", "@FF.2", "@FF.3", "@FF.4", "@FF.5", "@FF.6"]


def _ff_curve():
    """{(year, month): implied average fed funds rate} from 30-day FF futures."""
    out = {}
    for sym, q in (market.quotes(FF_SYMBOLS) or {}).items():
        m = re.search(r"\(([A-Z][a-z]{2})'(\d{2})\)", q.get("name") or "")
        last = market.num(q.get("last"))
        if not m or last is None or m.group(1) not in MONTHS:
            continue
        out[(2000 + int(m.group(2)), MONTHS[m.group(1)])] = 100.0 - last
    return out


def _shift(y, m, n):
    t = (y * 12 + (m - 1)) + n
    return t // 12, t % 12 + 1


def fed_expectation(meeting):
    """
    What 30-day Fed Funds futures price for a meeting, or None.

    Read futures against futures, never against the stated target range: the
    range is a band and the contracts settle on the *effective* rate, so
    subtracting one from the other mixes two conventions and invents a move
    that is not there. The month before the meeting and the month after it are
    both quoted on the same basis, and the difference between them is the move
    the market has priced.

    > Assumes the month before the meeting holds no meeting of its own. Where
    > the Fed meets in consecutive months this understates the move, so the
    > two implied rates are always printed beside the conclusion.
    """
    curve = _ff_curve()
    if not curve:
        return None
    before = _shift(meeting.year, meeting.month, -1)
    after = _shift(meeting.year, meeting.month, 1)
    if before not in curve or after not in curve:
        return None
    lo, hi = curve[before], curve[after]
    move = (hi - lo) * 100.0
    odds = abs(move) / 25.0
    direction = "hike" if move > 0 else "cut"
    whole, part = int(odds), odds - int(odds)
    if abs(move) < 5:
        text = "no move priced"
    elif whole == 0:
        text = f"~{part * 100:.0f}% odds of a 25bp {direction}"
    elif part < 0.15:
        text = f"{whole * 25}bp {direction} fully priced"
    else:
        text = (f"{whole * 25}bp {direction} priced, "
                f"~{part * 100:.0f}% odds of {whole * 25 + 25}bp")
    return {"before": lo, "after": hi, "move_bp": move, "text": text}


def _fed_note(ev):
    """The expectation line for a rate decision, or None for anything else."""
    if ev["kind"] != "econ" or ev["family"] != "fed_decision":
        return None
    exp = fed_expectation(ev["date"])
    if not exp:
        return None
    return (f"futures imply {exp['after']:.2f}% after vs {exp['before']:.2f}% "
            f"now — {exp['text']}")


# --- Rendering --------------------------------------------------------------

ICON = {"econ": "🏛", "earnings": "📊"}


def esc(v):
    """Escape for Telegram's HTML parse mode. Company names carry & often enough."""
    return html_escape(str(v), quote=False)


def _expect_bit(ev):
    """'exp 201K (prev 196K)' -- what the market is looking for."""
    if ev["kind"] == "earnings":
        return f"exp {ev['expected']} EPS" if ev["expected"] else "no estimate published"
    if ev["expected"] and ev["previous"]:
        return f"exp {ev['expected']} (prev {ev['previous']})"
    if ev["expected"]:
        return f"exp {ev['expected']}"
    if ev["previous"]:
        return f"prev {ev['previous']}"
    return "consensus not published yet"


def telegram_line(ev, esc, with_day=False):
    """One event, one line, as Telegram HTML."""
    day = f"<b>{ev['date']:%a %-d}</b> " if with_day else ""
    if ev["kind"] == "earnings":
        body = (f"<b>{esc(ev['label'])}</b> {SESSION_WORD[ev['session']]}"
                f" · {esc(_expect_bit(ev))}")
        if ev.get("on_watchlist"):
            body += " · <i>on today's screen</i>"
    else:
        clock = f"<b>{ev['when']:%H:%M}</b> " if ev["when"] else ""
        body = f"{clock}{esc(ev['label'])} · {esc(_expect_bit(ev))}"
        note = _fed_note(ev)
        if note:
            body += f"\n   <i>{esc(note)}</i>"
    return f"{day}{ICON[ev['kind']]} {body}"


def telegram_week(weekly, n=TOP_N, attached=True):
    """The /calendar digest: the n that matter, one line each."""
    picks = top(weekly, n)
    total = sum(len(evs or []) for _, evs in weekly)
    start = weekly[0][0] if weekly else date.today()
    L = [f"📅 <b>WEEK OF {start:%b %-d}</b>".replace(f"{start:%b}", f"{start:%b}".upper())]
    if not picks:
        L += ["", "<i>Nothing significant scheduled. No Fed, no CPI, no jobs "
                  "number, no big reporters.</i>"]
        return "\n".join(L)
    L += [f"<i>the {len(picks)} that matter</i>", ""]
    for ev in picks:
        L.append(telegram_line(ev, esc, with_day=True))
    # "4 of 4" says nothing; the count only earns its place when something was
    # left out of the digest and is waiting in the attachment.
    note = "Full week attached" if attached else ""
    if len(picks) < total:
        note = (f"{note} · " if note else "") + f"top {len(picks)} of {total}"
    if note:
        L += ["", f"<i>{note}</i>"]
    return "\n".join(L)


def telegram_day(events, heading="📅 <b>TODAY</b>"):
    """
    The premarket digest block: what lands before the next close.

    `None` means the feed could not be read, which is not the same as a day
    with nothing on it and must never be shown as one -- the same distinction
    the gapper report draws between "no press release" and "news not checked".
    """
    if events is None:
        return f"{heading}\n<i>Calendar unavailable — the feed could not be read.</i>"
    if not events:
        return f"{heading}\n<i>Nothing scheduled today.</i>"
    # Economic releases first, then earnings, a blank line between.
    econ = [e for e in events if e["kind"] != "earnings"]
    econ.sort(key=lambda e: e["when"] or datetime.max.replace(tzinfo=ET))
    earn = [e for e in events if e["kind"] == "earnings"]
    L = [heading]
    if econ:
        L += [""] + [telegram_line(e, esc) for e in econ]
    if earn:
        L += ["", "<i>Earnings</i>"] + [telegram_line(e, esc) for e in earn]
    return "\n".join(L)


def markdown_week(weekly):
    """The full week, nothing dropped -- the file attached to /calendar."""
    start = weekly[0][0] if weekly else date.today()
    end = weekly[-1][0] if weekly else start
    L = [
        "---",
        "type: reference",
        f"title: Economic Calendar — week of {start:%Y-%m-%d}",
        f"aliases: [Economic Calendar — week of {start:%Y-%m-%d}]",
        "tags: [investing, trading, market-data, economics]",
        f"created: {date.today()}",
        f"updated: {date.today()}",
        "---",
        "",
        f"# Economic Calendar — {start:%B %-d} to {end:%B %-d, %Y}",
        "",
        "Generated by Stock Research Bot. Scheduled events and earnings that "
        "clear the significance bar: policy, prices, jobs and growth releases, "
        "plus companies above "
        f"${LARGE_CAP_FLOOR / 1e9:,.0f}B and anything on the morning's screen.",
        "",
        "*Consensus figures are the market's expectation as published before "
        "the event, not a forecast made here.*",
        "",
    ]
    for day, evs in weekly:
        if not evs:
            continue
        L += [f"## {day:%A, %B %-d}", "",
              "| | Time (ET) | Event | Expected | Previous |",
              "|---|---|---|---|---|"]
        for ev in evs:
            when = f"{ev['when']:%H:%M}" if ev["when"] else (
                SESSION_WORD[ev["session"]] if ev["kind"] == "earnings" else "—")
            label = (f"**{ev['label']}** — {ev['name']}"
                     if ev["kind"] == "earnings" and ev.get("name")
                     else f"**{ev['label']}**")
            L.append(f"| {ICON[ev['kind']]} | {when} | {label} | "
                     f"{ev['expected'] or '—'} | {ev['previous'] or '—'} |")
            note = _fed_note(ev)
            if note:
                L.append(f"| | | *{note}* | | |")
        L.append("")
    if not any(evs for _, evs in weekly):
        L += ["*Nothing clears the bar this week.*", ""]
    return "\n".join(L)


def markdown_day(events, heading="Today's Calendar"):
    """
    The premarket report's section: economic releases, then reporters.

    > Two blocks: ranking everything on one score interleaved AZO's print with
    > the 2-Year Note Auction, which is unreadable.
    """
    L = ["", f"## {heading}", ""]
    if events is None:
        L += ["> [!warning] Calendar not checked",
              "> The events feed could not be read on this run, so this section "
              "is missing rather than empty.", ""]
        return L
    if not events:
        L += ["*Nothing scheduled today.*", ""]
        return L

    econ = [e for e in events if e["kind"] == "econ"]
    earn = [e for e in events if e["kind"] == "earnings"]

    if econ:
        L += ["**Economic**", "",
              "| | Time (ET) | Event | Expected | Previous |",
              "|---|---|---|---|---|"]
        for ev in sorted(econ, key=lambda e: (e["when"] is None,
                                              e["when"] or datetime.max.replace(tzinfo=ET))):
            when = f"{ev['when']:%H:%M}" if ev["when"] else "—"
            L.append(f"| {ICON[ev['kind']]} | {when} | **{ev['label']}** | "
                     f"{ev['expected'] or '—'} | {ev['previous'] or '—'} |")
            note = _fed_note(ev)
            if note:
                L.append(f"| | | *{note}* | | |")
        L.append("")

    if earn:
        L += ["**Earnings**", "",
              "| | Ticker | Company | When | Est. EPS | Year ago |",
              "|---|---|---|---|---|---|"]
        # Before the open first, then after the close, then unscheduled; within
        # each, biggest name first -- the order they matter in premarket.
        rank = {"bmo": 0, "amc": 1, None: 2}
        for ev in sorted(earn, key=lambda e: (rank.get(e["session"], 2), -e["score"])):
            star = " ⭐" if ev.get("on_watchlist") else ""
            L.append(f"| {ICON[ev['kind']]} | **{ev['label']}**{star} | "
                     f"{ev.get('name') or '—'} | {SESSION_WORD[ev['session']]} | "
                     f"{ev['expected'] or '—'} | {ev['previous'] or '—'} |")
        L += ["", "⭐ *on today's gapper screen*" if any(
            e.get("on_watchlist") for e in earn) else ""]
    return L


# --- Self-check -------------------------------------------------------------

def check(days=14):
    """
    Prove the feed's date offset still holds, and show the time conversion.

    Both quirks in the module docstring are empirical, not documented by
    Nasdaq, so they are worth re-testing rather than trusting: a silent change
    would move every event in the report by a day or an hour.
    """
    ok = True
    today = date.today()
    print(f"date offset: querying D+{ECON_QUERY_OFFSET.days} for D's events")
    for i in range(days):
        d = today + timedelta(days=i)
        for ev in economic(d):
            want = WEEKDAY_ANCHORS.get(ev["label"])
            if want is None:
                continue
            got = d.weekday()
            flag = "ok" if got == want else "MISMATCH"
            if got != want:
                ok = False
            print(f"  {d} ({d:%a}) {ev['label']}: {flag}")
    print(f"\nfeed timezone: reading '{FEED_TZ}' and converting to ET")
    for i in range(days):
        d = today + timedelta(days=i)
        for ev in economic(d):
            if ev["when"]:
                print(f"  {d} {ev['label']:<32} -> {ev['when']:%H:%M} ET")
    print("\n" + ("✓ anchors agree" if ok else
                  "! anchors disagree — the offset has changed, fix "
                  "ECON_QUERY_OFFSET before trusting the calendar"))
    return 0 if ok else 1


def main():
    if "--check" in sys.argv:
        return check()
    n = TOP_N
    if "--top" in sys.argv:
        n = int(sys.argv[sys.argv.index("--top") + 1])
    weekly = week()
    if "--markdown" in sys.argv:
        print(markdown_week(weekly))
        return 0
    print(telegram_week(weekly, n, attached=False))
    print("\n--- today ---")
    print(telegram_day(day_events(date.today())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
