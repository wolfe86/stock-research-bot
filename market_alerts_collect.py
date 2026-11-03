#!/usr/bin/env python3
"""
Market alerts collector -- polls every source and keeps only what is new.

Runs every 15 minutes from market-alerts-collect.timer. Each new item lands in
data/market_alerts.db with digested = NULL; the evening digest reads those,
and marks them once they have been posted.

Every item carries a layer: `core` (the broad market, always on) or `focus`
(the focus topic -- see the FOCUS_* settings in market_alerts_sources). Changing
the focus changes what the focus layer polls; nothing here changes.

No Claude here, and no Telegram: this script only gathers. A failing source
is logged and skipped -- one dead feed must never cost the rest.

Usage:
    python3 market_alerts_collect.py            # poll everything
    python3 market_alerts_collect.py --only youtube,edgar
    python3 market_alerts_collect.py --status   # what is waiting for the digest

YouTube transcripts are optional: set YT_TRANSCRIPT_BIN to a command that
takes `-p -o OUT.txt -- VIDEO_ID` and writes a plain-text transcript (a small
wrapper around yt-dlp works). Without it, videos are summarised from their
title and description.

Stdlib only.
"""

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import market_alerts_sources as S  # noqa: E402
import settings  # noqa: E402

F = S.FOCUS
ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "market_alerts.db"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128 Safari/537.36")
# SEC wants a named contact (SEC_USER_AGENT, read when EDGAR is first
# called); Reddit wants a descriptive agent.
REDDIT_UA = "linux:stock-research-bot:0.2"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id         TEXT PRIMARY KEY,     -- sha1 of the canonical key
    kind       TEXT NOT NULL,        -- youtube | reddit | news | letter | gov | gnews | edgar | truth | holder | trends
    source     TEXT NOT NULL,        -- feed or channel name
    label      TEXT NOT NULL,        -- data | opinion
    title      TEXT NOT NULL,
    url        TEXT NOT NULL,
    published  TEXT NOT NULL,        -- UTC ISO8601
    fetched    TEXT NOT NULL,
    extra      TEXT,                 -- json: description, ticker, form, items...
    digested   TEXT,                 -- date of the digest that posted it
    layer      TEXT NOT NULL DEFAULT 'core'   -- core | focus
);
CREATE INDEX IF NOT EXISTS ix_items_digested ON items(digested);
CREATE TABLE IF NOT EXISTS titles (
    norm  TEXT PRIMARY KEY,          -- normalised headline, for cross-feed dedupe
    seen  TEXT NOT NULL
);
"""

# An empty pattern would match every title, so no keywords = never matches.
FOCUS_KW = re.compile("|".join(F.KEYWORDS) if F.KEYWORDS else r"(?!x)x", re.I)
NOISE = re.compile("|".join(S.NOISE), re.I)


def now():
    return datetime.now(timezone.utc)


def log(msg):
    print(f"{now():%Y-%m-%d %H:%M:%SZ} {msg}", flush=True)


def get(url, ua=UA, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": ua,
                                               "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def strip(s):
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s or "", flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", unescape(s)).strip()


def tag(block, name):
    m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.S)
    return strip(m.group(1)) if m else ""


def parse_dt(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)            # RSS: RFC 822
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))  # Atom
        except ValueError:
            return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def parse_feed(body):
    """RSS <item> and Atom <entry> alike, as dicts with title/url/dt/desc."""
    out = []
    for block in re.findall(r"<item>(.*?)</item>", body, re.S):
        out.append({
            "title": tag(block, "title"),
            "url": tag(block, "link"),
            "dt": parse_dt(tag(block, "pubDate") or tag(block, "dc:date")),
            "desc": tag(block, "description")[:400],
        })
    for block in re.findall(r"<entry>(.*?)</entry>", body, re.S):
        m = re.search(r'<link[^>]*href="([^"]+)"', block)
        vid = tag(block, "yt:videoId")
        out.append({
            "title": tag(block, "title"),
            "url": unescape(m.group(1)) if m else "",
            "dt": parse_dt(tag(block, "published") or tag(block, "updated")),
            "desc": tag(block, "media:description")[:400]
                    or tag(block, "content")[:400],
            "video_id": vid,
        })
    return [i for i in out if i["title"] and i["url"] and i["dt"]]


def norm_title(t):
    t = re.sub(r"\s+-\s+[^-]+$", "", t)          # Google News " - Publisher"
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


class Store:
    def __init__(self, path=DB):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.added = 0

    def add(self, layer, kind, source, label, title, url, dt, extra=None,
            key=None):
        cutoff = now() - timedelta(hours=S.MAX_AGE_HOURS)
        if dt < cutoff:
            return
        iid = hashlib.sha1((key or url).encode()).hexdigest()
        if self.db.execute("SELECT 1 FROM items WHERE id=?", (iid,)).fetchone():
            return
        # The same story arrives from several feeds; keep the first.
        nt = norm_title(title)
        if kind != "edgar" and nt and self.db.execute(
                "SELECT 1 FROM titles WHERE norm=?", (nt,)).fetchone():
            return
        # A core item that is also on the focus topic counts as focus.
        if layer == "core" and FOCUS_KW.search(title):
            layer = "focus"
        self.db.execute(
            "INSERT INTO items (id, kind, source, label, title, url, published,"
            " fetched, extra, digested, layer) VALUES (?,?,?,?,?,?,?,?,?,NULL,?)",
            (iid, kind, source, label, title, url, dt.isoformat(),
             now().isoformat(), json.dumps(extra or {}), layer))
        if nt:
            self.db.execute("INSERT OR IGNORE INTO titles VALUES (?,?)",
                            (nt, now().isoformat()))
        self.added += 1

    def commit(self):
        self.db.commit()


# --- collectors --------------------------------------------------------------

def collect_youtube(st):
    for layer, chans in (("core", S.CORE_YOUTUBE), ("focus", F.YOUTUBE)):
        for name, cid in chans:
            try:
                for it in parse_feed(get(S.YOUTUBE_FEED.format(cid))):
                    st.add(layer, "youtube", name, S.OPINION, it["title"],
                           it["url"], it["dt"],
                           {"video_id": it.get("video_id"), "desc": it["desc"]})
            except Exception as e:
                log(f"! youtube {name}: {e}")


TRANSCRIPTS = ROOT / "data" / "market_alerts" / "transcripts"
YT = settings.YT_TRANSCRIPT_BIN
YT_PER_RUN = 4        # YouTube returns 429 after ~10 quick pulls
YT_GAP = 20           # seconds between pulls
YT_TRIES = 6          # then give up; the digest reports "not available"


def fetch_transcripts(st):
    """
    Pull transcripts for new videos a few at a time, spread across the day.

    Done here rather than in the evening digest because pulling a dozen at
    once got every one after the first refused with HTTP 429. A failure is
    counted in <id>.fail and retried on later runs, up to YT_TRIES.
    Skipped entirely when YT_TRANSCRIPT_BIN is not set.
    """
    if not YT:
        return
    TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    rows = st.db.execute("SELECT extra FROM items WHERE kind='youtube' "
                         "AND digested IS NULL ORDER BY published DESC").fetchall()
    todo = []
    for (extra,) in rows:
        vid = json.loads(extra or "{}").get("video_id")
        if not vid or (TRANSCRIPTS / f"{vid}.txt").exists():
            continue
        fail = TRANSCRIPTS / f"{vid}.fail"
        n = int(fail.read_text() or 0) if fail.exists() else 0
        if n < YT_TRIES:
            todo.append((vid, fail, n))
    for k, (vid, fail, n) in enumerate(todo[:YT_PER_RUN]):
        if k:
            time.sleep(YT_GAP)
        out = TRANSCRIPTS / f"{vid}.txt"
        try:
            r = subprocess.run([YT, "-p", "-o", str(out), "--", vid],
                               capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"! transcript {vid}: {e}")
            continue
        if r.returncode == 0 and out.exists() and out.stat().st_size:
            fail.unlink(missing_ok=True)
            log(f"transcript {vid} ok")
        else:
            out.unlink(missing_ok=True)
            fail.write_text(str(n + 1))
            log(f"! transcript {vid} failed ({n + 1}/{YT_TRIES}): "
                f"{(r.stderr.strip().splitlines() or ['?'])[-1][:160]}")


def collect_reddit(st):
    """A rotating slice of subreddits per run -- see REDDIT_PER_RUN. Off when
    CORE_REDDIT and FOCUS_REDDIT are both empty (the default)."""
    subs = [("core", s) for s in S.CORE_REDDIT] + \
           [("focus", s) for s in F.REDDIT]
    if not subs:
        return
    slot = int(time.time() // 900)               # one slot per 15-min run
    start = (slot * S.REDDIT_PER_RUN) % len(subs)
    pick = [subs[(start + i) % len(subs)]
            for i in range(min(S.REDDIT_PER_RUN, len(subs)))]
    for i, (layer, sub) in enumerate(pick):
        if i:
            time.sleep(S.REDDIT_GAP)
        try:
            for it in parse_feed(get(S.REDDIT_FEED.format(sub), ua=REDDIT_UA)):
                st.add(layer, "reddit", f"r/{sub}", S.OPINION, it["title"],
                       it["url"], it["dt"], {"desc": it["desc"][:300]})
        except Exception as e:
            log(f"! reddit r/{sub}: {e}")


def collect_core_news(st):
    for name, url, filt in S.CORE_NEWS:
        try:
            for it in parse_feed(get(url)):
                if filt and NOISE.search(it["title"]):
                    continue
                st.add("core", "news", name, S.OPINION, it["title"], it["url"],
                       it["dt"], {"desc": it["desc"][:300]})
        except Exception as e:
            log(f"! news {name}: {e}")


def collect_letters(st):
    for name, url in S.CORE_NEWSLETTERS:
        try:
            for it in parse_feed(get(url, timeout=40)):
                st.add("core", "letter", name, S.OPINION, it["title"],
                       it["url"], it["dt"], {"desc": it["desc"][:400]})
        except Exception as e:
            log(f"! letter {name}: {e}")


def collect_focus_feeds(st, feeds, kind, label):
    for name, url, filt in feeds:
        try:
            for it in parse_feed(get(url)):
                if filt and not FOCUS_KW.search(it["title"]):
                    continue
                st.add("focus", kind, name, label, it["title"], it["url"],
                       it["dt"], {"desc": it["desc"][:300]})
        except Exception as e:
            log(f"! {kind} {name}: {e}")


def collect_gnews(st):
    for layer, queries in (("core", S.CORE_GNEWS), ("focus", F.GNEWS)):
        for q in queries:
            url = S.GNEWS_FEED.format(urllib.parse.quote_plus(q))
            try:
                for it in parse_feed(get(url)):
                    # Google News titles end in " - Publisher"; keep it as source.
                    m = re.search(r"\s+-\s+([^-]+)$", it["title"])
                    pub = m.group(1).strip() if m else "Google News"
                    st.add(layer, "gnews", pub, S.OPINION, it["title"],
                           it["url"], it["dt"], {"query": q})
            except Exception as e:
                log(f"! gnews {q!r}: {e}")
            time.sleep(1)


def collect_truth(st):
    """Trump's Truth Social posts -- core layer, every run."""
    for name, aid in S.TRUTH:
        try:
            for s in json.loads(get(S.TRUTH_API.format(id=aid))):
                post = s.get("reblog") or s
                text = strip(post.get("content", ""))
                if not text:
                    continue                     # media-only post
                dt = parse_dt(s.get("created_at"))
                if not dt:
                    continue
                pre = "ReTruth: " if s.get("reblog") else ""
                st.add("core", "truth", name, S.OPINION,
                       (pre + text)[:280], s.get("url") or post.get("url"), dt,
                       {"desc": text[:600]})
        except Exception as e:
            log(f"! truth {name}: {e}")


def load_cik_map():
    p = ROOT / "data" / "cache" / "cik_map.json"   # kept fresh by dilution.py
    try:
        return json.loads(p.read_text())
    except Exception:
        raw = json.loads(get("https://www.sec.gov/files/company_tickers.json",
                             ua=settings.sec_user_agent()))
        return {v["ticker"].upper(): str(v["cik_str"]).zfill(10)
                for v in raw.values()}


def collect_edgar(st):
    if not F.EDGAR_TICKERS:
        return
    ciks = load_cik_map()
    for t in F.EDGAR_TICKERS:
        cik = ciks.get(t)
        if not cik:
            log(f"! edgar {t}: no CIK")
            continue
        try:
            d = json.loads(get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                               ua=settings.sec_user_agent()))
        except Exception as e:
            log(f"! edgar {t}: {e}")
            continue
        r = d.get("filings", {}).get("recent", {})
        for i, form in enumerate(r.get("form", [])[:40]):
            if not form.startswith(S.EDGAR_FORMS):
                continue
            acc = r["accessionNumber"][i]
            dt = parse_dt(r["acceptanceDateTime"][i])
            items = r.get("items", [""] * (i + 1))[i]
            named = [S.EIGHT_K_ITEMS[x] for x in items.split(",")
                     if x in S.EIGHT_K_ITEMS]
            title = f"{t} {form}" + (f" — {', '.join(named)}" if named else "")
            url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                   f"{acc.replace('-', '')}/{r['primaryDocument'][i]}")
            if dt:
                st.add("focus", "edgar", "SEC EDGAR", S.DATA, title, url, dt,
                       {"ticker": t, "form": form, "items": items}, key=acc)
        time.sleep(0.15)                            # SEC: under 10 req/s


def collect_trends(st):
    """Market terms in today's top US Google searches."""
    keep = re.compile("|".join(S.TRENDS_KEEP), re.I)
    try:
        body = get(S.TRENDS_RSS)
    except Exception as e:
        return log(f"! trends: {e}")
    for block in re.findall(r"<item>(.*?)</item>", body, re.S):
        term = tag(block, "title")
        if not term or not keep.search(term):
            continue
        traffic = tag(block, "ht:approx_traffic")
        dt = parse_dt(tag(block, "pubDate")) or now()
        news = tag(block, "ht:news_item_title")
        st.add("core", "trends", "Google Trends", S.DATA,
               f'"{term}" is trending in US searches ({traffic} searches)',
               f"https://trends.google.com/trends/explore?geo=US&q="
               f"{urllib.parse.quote(term)}", dt,
               {"desc": news[:300], "traffic": traffic},
               key=f"trends:{term.lower()}:{dt:%Y-%m-%d}")


def subject_company(cik, acc):
    """The company a 13D/13G is about, from the filing's header page."""
    url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
           f"{acc.replace('-', '')}/{acc}-index-headers.html")
    try:
        page = get(url, ua=settings.sec_user_agent())
        m = re.search(r"SUBJECT COMPANY.*?CONFORMED NAME:\s*([^\n<]+)",
                      page, re.S | re.I)
        return m.group(1).strip() if m else None
    except Exception:
        return None


def collect_holders(st):
    """13F holdings and 13D/13G stakes filed by the big investors."""
    for name, cik in S.HOLDERS:
        try:
            d = json.loads(get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                               ua=settings.sec_user_agent()))
        except Exception as e:
            log(f"! holders {name}: {e}")
            continue
        r = d.get("filings", {}).get("recent", {})
        for i, form in enumerate(r.get("form", [])[:30]):
            if not form.upper().startswith(S.HOLDER_FORMS):
                continue
            acc = r["accessionNumber"][i]
            dt = parse_dt(r["acceptanceDateTime"][i])
            url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                   f"{acc.replace('-', '')}/{r['primaryDocument'][i]}")
            if not dt:
                continue
            title = f"{name} {form}"
            if "13D" in form.upper() or "13G" in form.upper():
                subj = subject_company(cik, acc)
                if subj:
                    title += f" — stake in {subj}"
            st.add("core", "holder", name, S.DATA, title, url, dt,
                   {"form": form}, key=acc)
        time.sleep(0.15)


COLLECTORS = {
    "youtube": collect_youtube,
    "transcripts": fetch_transcripts,
    "reddit": collect_reddit,
    "truth": collect_truth,
    "news": collect_core_news,
    "letters": collect_letters,
    "focusnews": lambda st: collect_focus_feeds(st, F.NEWS, "news", S.OPINION),
    "gov": lambda st: collect_focus_feeds(st, F.GOV, "gov", S.DATA),
    "gnews": collect_gnews,
    "edgar": collect_edgar,
    "holders": collect_holders,
    "trends": collect_trends,
}


def status():
    db = sqlite3.connect(DB)
    rows = db.execute("SELECT layer, kind, count(*) FROM items "
                      "WHERE digested IS NULL GROUP BY layer, kind "
                      "ORDER BY 1, 3 DESC").fetchall()
    total = sum(n for *_, n in rows)
    print(f"{total} items waiting for the digest (focus: {F.NAME})")
    for layer, k, n in rows:
        print(f"  {layer:5} {k:8} {n}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--only", help="comma list of: " + ",".join(COLLECTORS))
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    if a.status:
        return status()
    names = a.only.split(",") if a.only else list(COLLECTORS)
    st = Store()
    for n in names:
        try:
            COLLECTORS[n](st)
        except Exception as e:
            # One collector's setup error (no SEC_USER_AGENT, say) must not
            # cost the others.
            log(f"! {n}: {e}")
        st.commit()
    log(f"collect: {st.added} new ({','.join(names)})")


if __name__ == "__main__":
    main()
