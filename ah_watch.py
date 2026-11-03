#!/usr/bin/env python3
"""
After-Hours Watch: the one after-hours gainer the 4am crowd is likely to trade,
and the price it has to hold before it is worth caring about.

Two halves:
  - Evening (ah-watch.timer, just after the 20:00 ET after-hours close): rank
    the after-hours gainers and save the pick, with its 8pm close as the level
    to hold, to data/ah_watch/<session date>.json. Then write the evening
    digest (SRB_OUTPUT_DIR/daily/Evening_Digest_<date>.md), render it to PDF
    if report_pdf's dependencies are installed, and post it to Telegram.
  - Morning (called by premarket_report.py): read last night's pick and judge
    it against the premarket so far.

The rules come from a study run 2026-10-27 over 330 after-hours runs of +10%
or more (Aug-Oct 2026, 196 small caps, Yahoo 5-minute bars):
  - The bigger the after-hours move, the more is still there at 07:00 ET
    (rank correlation 0.69).
  - The tell is 04:00-05:00 ET: a stock that trades above its 8pm close in
    that hour kept at least half its move at 07:00 ET 62-80% of the time;
    one that never got above it, 25-36%.
  - A thinly traded evening is a fake move (kept half 18-21%).
Yahoo's extended-hours volume is unusable (mostly zero), so "active" means
5-minute bars with prints, not shares.

Stdlib only (the PDF is optional -- see report_pdf.py). Usage:
    python3 ah_watch.py              # evening pick + digest for today's session
    python3 ah_watch.py --dry-run    # same, but print the Telegram post instead
    python3 ah_watch.py --digest-only  # rebuild the digest from the saved pick
    python3 ah_watch.py --morning    # print the morning block for the latest pick
"""

import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import settings

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "ah_watch"
ET = ZoneInfo("America/New_York")
LOCAL = settings.LOCAL       # local time zone (settings.TIMEZONE)
TZL = settings.TIMEZONE_LABEL
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

MIN_AH = 0.10        # after-hours gain to be considered
MIN_BARS = 15        # active 5-minute bars out of 48; under this the move is thin
BUSY_BARS = 30
FADE = -0.02         # last 45 minutes worse than this = fading into the close
PIN_AH, PIN_RANGE = 0.40, 0.015   # big gain stuck in a tight last hour = likely cash deal


def fetch(url, ua=UA):
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    return urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")


def gainers():
    """{symbol: {name, cap}} from stockanalysis.com's after-hours gainers page, in rank order."""
    html = fetch("https://stockanalysis.com/markets/afterhours/gainers/")
    out = {}
    for b in re.findall(r'\{no:\d+,[^{}]*\}', html):
        s = re.search(r's:"([A-Z.]+)"', b)
        if not s or s.group(1) in out:
            continue
        n = re.search(r'n:"((?:[^"\\]|\\.)*)"', b)
        c = re.search(r'marketCap:([\d.e+]+)', b)
        out[s.group(1)] = {"name": n.group(1) if n else "", "cap": float(c.group(1)) if c else None}
    return out


def bars(sym, rng="1d"):
    """[(datetime ET, close, high)] of 5-minute bars, extended hours included.
    Yahoo rate-limits with 429s, so back off and alternate its two hosts."""
    for attempt, wait in enumerate((0, 5, 15, 40)):
        time.sleep(wait)
        host = ("query1", "query2")[attempt % 2]
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}"
               f"?interval=5m&range={rng}&includePrePost=true")
        try:
            # Yahoo 429s the full Chrome user-agent but serves a bare one.
            raw = fetch(url, ua="Mozilla/5.0")
            break
        except urllib.error.HTTPError as e:
            if e.code != 429 or wait == 40:
                raise
    r = json.loads(raw)["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    return [(datetime.fromtimestamp(t, ET), q["close"][i], q["high"][i] or q["close"][i])
            for i, t in enumerate(r.get("timestamp") or []) if q["close"][i] is not None]


def _m(t):
    return t.hour * 60 + t.minute


def profile(sym):
    """The evening's numbers for one symbol, or None when there is no session."""
    b = bars(sym)
    if not b:
        return None
    day = b[-1][0].date()
    reg = [x for x in b if x[0].date() == day and 570 <= _m(x[0]) < 960]
    post = [x for x in b if x[0].date() == day and _m(x[0]) >= 960]
    if not reg or not post:
        return None
    close, level = reg[-1][1], post[-1][1]
    late = [x for x in post if _m(x[0]) >= 1155]        # 19:15 ET onward
    hour = [x for x in post if _m(x[0]) >= 1140]        # last hour
    hi_hr = max(x[2] for x in hour) if hour else level
    lo_hr = min(x[1] for x in hour) if hour else level
    return {
        "symbol": sym, "session": day.isoformat(), "close": close, "level": level,
        "ah": level / close - 1, "ah_high": max(x[2] for x in post) / close - 1,
        "bars": min(len(post), 48),   # 16:00-20:00 is 48 five-minute bars
        "late": (late[-1][1] / late[0][1] - 1) if len(late) > 1 else 0.0,
        "pinned": (level / close - 1) >= PIN_AH and (hi_hr - lo_hr) / level < PIN_RANGE,
    }


def evening():
    rows, syms, failed, board = [], gainers(), 0, []
    for s in syms:
        time.sleep(0.6)
        try:
            p = profile(s)
        except Exception as e:
            print(f"  ! {s}: {e}", file=sys.stderr)
            failed += 1
            continue
        if p:
            p.update(syms[s])
            board.append(p)
            if p["ah"] >= MIN_AH:
                rows.append(p)
    for p in rows:
        p["skip"] = ("pinned — likely a cash deal" if p["pinned"] else
                     "thin evening" if p["bars"] < MIN_BARS else
                     "fading into the close" if p["late"] < FADE else None)
    live = [p for p in rows if not p["skip"]]
    # Biggest move wins; a busy evening breaks near-ties.
    live.sort(key=lambda p: (p["ah"] * (1.0 if p["bars"] >= BUSY_BARS else 0.7)), reverse=True)
    pick = live[0]["symbol"] if live else None
    out = {"session": rows[0]["session"] if rows else datetime.now(ET).date().isoformat(),
           "made": datetime.now(LOCAL).strftime(f"%Y-%m-%d %H:%M {TZL}"),
           "pick": pick, "candidates": live[:6],
           "skipped": [p for p in rows if p["skip"]][:8],
           "board": sorted(board, key=lambda p: -p["ah"])}
    if syms and failed > len(syms) // 2:
        # A blocked run must not leave a "nothing qualified" file for the morning to trust.
        print(f"✗ AH watch: {failed}/{len(syms)} lookups failed; nothing saved", file=sys.stderr)
        sys.exit(1)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{out['session']}.json"
    path.write_text(json.dumps(out, indent=1))
    print(f"✓ AH watch {out['session']}: {pick or 'none'} -> {path}")
    for p in live[:4]:
        print(f"  {p['symbol']:6} AH {p['ah']:+.1%}  hold ${p['level']:.4g}  bars {p['bars']}  last45 {p['late']:+.1%}")
    for p in out["skipped"]:
        print(f"  skip {p['symbol']:6} AH {p['ah']:+.1%}  {p['skip']}")
    return out


# ── evening digest ─────────────────────────────────────────
# A digest of the after-hours gainers and tomorrow's watch, attached to the
# evening Telegram post as a PDF. The markdown file is the record.
DAILY = settings.OUTPUT_DIR / "daily"


def _window():
    """The 4-5am ET test hour, in local time, with ET alongside when local is not ET."""
    loc = settings.et_hours_local(4, 5)
    return loc if TZL == "ET" else f"{loc} (4–5am ET)"


def _cap(c):
    if not c:
        return "—"
    return f"${c / 1e9:.1f}B" if c >= 1e9 else f"${c / 1e6:.0f}M"


def _read(p):
    short = {"pinned — likely a cash deal": "pinned (deal)", "thin evening": "thin",
             "fading into the close": "fading"}
    if p.get("skip"):
        return short.get(p["skip"], p["skip"])
    return "qualified" if p["ah"] >= MIN_AH else "under +10%"


def digest_markdown(w):
    day = datetime.fromisoformat(w["session"])
    now = datetime.now(LOCAL)
    pick = next((c for c in w["candidates"] if c["symbol"] == w["pick"]), None)
    L = ["---", "type: source", f"title: Evening Digest — {w['session']}",
         f"aliases: [Evening Digest — {w['session']}]",
         "tags: [investing, market-data, trading, small-cap]",
         f"created: {w['session']}", "---", "",
         f"# Evening Digest — {day:%A, %B %-d, %Y}", "",
         "Generated by Stock Research Bot.", "",
         f"**Generated:** {now:%H:%M} {TZL} "
         + ("" if TZL == "ET" else f"({now.astimezone(ET):%H:%M} ET) ")
         + "· After hours 16:00–20:00 ET", "",
         "---", "", "## 🌙 Tomorrow's Watch", ""]
    if pick:
        lvl = f"${pick['level']:.4g}"
        L += [f"> [!tip] {pick['symbol']} — hold {lvl}",
              f"> **Interested only if it trades above {lvl} between {_window()}.** "
              f"If it can't get back above {lvl} in that hour, pass.",
              ">",
              f"> After hours {pick['ah']:+.0%} (close ${pick['close']:.4g} → 8pm {lvl}), "
              f"{pick['late']:+.1%} in the last 45 minutes, {pick['bars']}/48 active bars. "
              f"{pick.get('name', '')}, {_cap(pick.get('cap'))} market cap.", "",
              f"<!-- ahwatch {pick['symbol']} {pick['level']:.4g} evening -->", ""]
        others = [c for c in w["candidates"] if c["symbol"] != pick["symbol"]]
        if others:
            L.append("**Also qualified:** " + ", ".join(
                f"{c['symbol']} {c['ah']:+.0%} (hold ${c['level']:.4g})" for c in others))
            L.append("")
    else:
        L += ["*No after-hours gainer qualified tonight — every +10% mover was thin, "
              "fading or deal-pinned.*", ""]
    L += ["---", "", "## After-Hours Gainers", "",
          "*Ranked by the 8pm close against the 16:00 close.*", "",
          "| # | Ticker | Company | After hours | Close | Needs to hold | "
          "Last 45 min | Active | Mkt cap | Read |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for i, p in enumerate(w.get("board", []), 1):
        star = " ⭐" if p["symbol"] == w["pick"] else ""
        name = p.get("name", "")
        name = name if len(name) <= 24 else name[:23].rstrip() + "…"
        L.append(f"| {i} | **{p['symbol']}**{star} | {name} | {p['ah']:+.1%} | "
                 f"${p['close']:.4g} | **${p['level']:.4g}** | "
                 f"{p['late']:+.1%} | {p['bars']}/48 | {_cap(p.get('cap'))} | {_read(p)} |")
    L += ["", "*Needs to hold = the 8pm close. Pinned = a big gain stuck in a tight last hour, the mark of a cash buyout — it "
          "has nowhere to run. Thin = under 15 active bars of 48. Fading = down more than 2% "
          "in the last 45 minutes.*", "",
          "*The rule: a stock that trades above its 8pm close between 4 and 5am ET kept at "
          "least half its after-hours move at 7am ET 62–80% of the time; one that never got "
          "above it, 25–36% (330 after-hours runs, Aug–Oct 2026). Beta.*", ""]
    return "\n".join(L)


def telegram_caption(w):
    day = datetime.fromisoformat(w["session"])
    pick = next((c for c in w["candidates"] if c["symbol"] == w["pick"]), None)
    lines = [f"🌙 <b>Evening digest — {day:%a %b %-d}</b>", ""]
    if pick:
        lines += [f"<b>Watch: {pick['symbol']}</b> — interested only above <b>${pick['level']:.4g}</b> "
                  f"between {settings.et_hours_local(4, 5)} (after hours {pick['ah']:+.0%}).", ""]
    else:
        lines += ["No after-hours gainer qualified tonight.", ""]
    lines.append("<b>Top after-hours gainers</b>")
    for p in w.get("board", [])[:8]:
        tag = "" if not p.get("skip") else f" · {p['skip'].split(' —')[0]}"
        lines.append(f"{p['symbol']} {p['ah']:+.0%}{tag}")
    lines += ["", "Full digest attached."]
    return "\n".join(lines)


def digest(w, dry=False):
    """Write the digest, render the PDF, post both to Telegram. Never fatal."""
    DAILY.mkdir(parents=True, exist_ok=True)
    md = DAILY / f"Evening_Digest_{w['session']}.md"
    md.write_text(digest_markdown(w))
    print(f"✓ Digest: {md}")
    pdf = None
    try:
        import report_pdf
        pdf = report_pdf.render(md)
        print(f"✓ PDF: {pdf}")
    except Exception as e:
        print(f"  ! digest PDF failed: {e}", file=sys.stderr)
    try:
        import notify
        cap = telegram_caption(w)
        if pdf:
            notify.send_document(pdf.name, pdf.read_bytes(), caption=cap, dry=dry)
        else:
            notify.send(cap, dry=dry)
    except Exception as e:
        print(f"  ! digest post failed: {e}", file=sys.stderr)


def latest(today):
    """Most recent saved watch for a session before `today`."""
    files = sorted(f for f in OUT.glob("*.json") if f.stem < today.isoformat())
    return json.loads(files[-1].read_text()) if files else None


def judge(c, today):
    """Morning verdict for one candidate against today's premarket."""
    b = [x for x in bars(c["symbol"]) if x[0].date() == today and _m(x[0]) < 570]
    lvl = c["level"]
    if not b:
        return {"state": "none", "line": f"no premarket prints yet — needs to hold ${lvl:.4g}"}
    first = [x for x in b if 240 <= _m(x[0]) < 300]
    now, t = b[-1][1], b[-1][0]
    fh = max(x[2] for x in first) if first else None
    pushed = fh is not None and fh > lvl
    vs = f"${now:.4g} at {t:%H:%M} ET ({now / lvl - 1:+.0%} vs ${lvl:.4g})"
    if pushed and now >= lvl:
        return {"state": "go", "now": now,
                "line": f"✅ **Interested.** Pushed above ${lvl:.4g} at 4–5am ET (high ${fh:.4g}) and holding: {vs}"}
    if pushed:
        return {"state": "lost", "now": now,
                "line": f"⚠️ **Pushed, then lost it.** High ${fh:.4g} at 4–5am ET, now below the level: {vs}"}
    return {"state": "pass", "now": now,
            "line": f"❌ **Pass.** Never got above ${lvl:.4g} in the first hour"
                    + (f" (high ${fh:.4g})" if fh else "") + f"; now {vs}"}


def markdown_block(today):
    """The After-Hours Watch section for the premarket report."""
    w = latest(today)
    L = ["", "## 🌙 After-Hours Watch", ""]
    if not w:
        return L + ["*No after-hours watch saved for the last session.*", "", "---"]
    sess = datetime.fromisoformat(w["session"]).strftime("%a %b %-d")
    if not w.get("pick"):
        L.append(f"*No after-hours gainer qualified on {sess}.*")
    # Every runner still in play gets the 4am test, the faded and thin ones too:
    # any of them can come back when the 4am crowd arrives. Only deal pins are out.
    rest = [p for p in w.get("skipped", []) if not p.get("pinned")]
    pool = w.get("candidates", []) + rest
    others = len(pool) - (1 if w.get("pick") else 0)
    for c in pool:
        try:
            v = judge(c, today)
        except Exception as e:
            v = {"state": "none", "line": f"premarket check failed: {e}"}
        if c["symbol"] == w["pick"]:
            L += [f"**AH watch — {c['symbol']}: hold ${c['level']:.4g}.** "
                  f"After hours {c['ah']:+.0%} on {sess} (close ${c['close']:.4g} → 8pm ${c['level']:.4g}), "
                  f"{c['late']:+.1%} in the last 45 minutes, {c['bars']}/48 active bars.", "",
                  f"> [!tip] {c['symbol']} — the 4am test", f"> {v['line']}", ""]
            L.append(f"<!-- ahwatch {c['symbol']} {c['level']:.4g} {v['state']} -->")
            if others:
                L += ["", "| Also ran after hours | After hours | Needs to hold | Last night | Premarket |",
                      "|---|---|---|---|---|"]
        else:
            if not w.get("pick") and c is pool[0]:
                L += ["| Ran after hours | After hours | Needs to hold | Last night | Premarket |",
                      "|---|---|---|---|---|"]
            L.append(f"| {c['symbol']} | {c['ah']:+.0%} | ${c['level']:.4g} | "
                     f"{c.get('skip') or 'qualified'} | {v['line']} |")
    pins = [p for p in w.get("skipped", []) if p.get("pinned")]
    if pins:
        L += ["", "*Left out: " + "; ".join(
            f"{p['symbol']} {p['ah']:+.0%} (pinned — likely a cash deal)" for p in pins) + ".*"]
    L += ["", "*The rule: a stock that trades above its 8pm close between 4 and 5am ET kept at least "
          "half its after-hours move at 7am ET 62–80% of the time; one that never got above it, 25–36% "
          "(330 after-hours runs, Aug–Oct 2026). Beta.*", "", "---"]
    return L


if __name__ == "__main__":
    if "--morning" in sys.argv:
        print("\n".join(markdown_block(datetime.now(LOCAL).date())))
    elif "--digest-only" in sys.argv:
        # Rebuild tonight's digest from the saved watch without re-fetching.
        files = sorted(OUT.glob("*.json"))
        digest(json.loads(files[-1].read_text()), dry="--dry-run" in sys.argv)
    else:
        digest(evening(), dry="--dry-run" in sys.argv)
