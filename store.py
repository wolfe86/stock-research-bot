#!/usr/bin/env python3
"""
Research history: every /pillars and /price lookup, kept.

The HTTP cache is disposable -- delete it and it refills. This is the opposite:
a durable record of what was researched, when, at what price, and what the
models said at the time. It answers questions the cache cannot:

    How has ADBE's score moved over six months?
    Which names were looked at before they ran?
    Do 8/8 names actually outperform?

SQLite, stdlib, one file at data/research.db.

Writes must never break a command: every call is wrapped, and a failure to
record is logged and swallowed. Losing a history row is not worth losing a
reply the user asked for.

Usage:
    python3 store.py ADBE          # history for one ticker
    python3 store.py --summary     # what has been researched
"""

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB = Path(__file__).resolve().parent / "data" / "research.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS lookups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,          -- UTC ISO8601
    ticker          TEXT    NOT NULL,
    command         TEXT    NOT NULL,          -- pillars | price
    name            TEXT,
    price           REAL,
    mcap            REAL,
    pillars_passed  INTEGER,
    pillars_scored  INTEGER,
    dcf_low         REAL,
    dcf_base        REAL,
    dcf_high        REAL,
    implied_growth  REAL,
    target_5y_10    REAL,
    target_5y_15    REAL,
    target_10y_10   REAL,
    target_10y_15   REAL,
    exit_pe         REAL,
    rev_growth      REAL,
    net_margin      REAL,
    fcf             REAL,
    requested_by    INTEGER,                   -- telegram user id, or null
    detail          TEXT,                      -- json: per-pillar marks, warnings
    hits            INTEGER NOT NULL DEFAULT 1, -- times seen unchanged
    last_ts         TEXT                        -- most recent of those times
);
CREATE INDEX IF NOT EXISTS ix_lookups_ticker_ts ON lookups(ticker, ts);
CREATE INDEX IF NOT EXISTS ix_lookups_ts ON lookups(ts);

-- One row per scheduled report run. The market context is flattened into
-- columns rather than a blob so it can be queried: "what was Fear & Greed on
-- the days the gappers actually followed through?"
CREATE TABLE IF NOT EXISTS reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date     TEXT    NOT NULL,          -- YYYY-MM-DD, local time (settings.TIMEZONE)
    kind            TEXT    NOT NULL,          -- premarket | aftermarket
    generated_ts    TEXT    NOT NULL,          -- UTC ISO8601
    trend_label     TEXT,
    spx             REAL,
    spx_off_high    REAL,
    fng_score       REAL,
    fng_rating      TEXT,
    vix             REAL,
    wti             REAL,
    brent           REAL,
    gold            REAL,
    btc             REAL,
    us10y           REAL,
    aaii_bull       REAL,
    aaii_bear       REAL,
    divergence      INTEGER,                   -- breadth contradicted price
    gapper_count    INTEGER,
    macro_count     INTEGER,
    note_path       TEXT,
    UNIQUE (report_date, kind)
);

-- One row per ticker per premarket report: the watchlist as it stood that
-- morning, which is what makes outcomes checkable later.
CREATE TABLE IF NOT EXISTS gappers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id       INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    report_date     TEXT    NOT NULL,
    rank            INTEGER,
    ticker          TEXT    NOT NULL,
    gap_pct         REAL,
    pm_price        REAL,
    pm_volume       REAL,
    float_shares    REAL,
    float_rotation  REAL,
    short_float_pct REAL,
    rel_volume      REAL,
    dilution_risk   TEXT,
    shelf_count     INTEGER,
    days_since_offering INTEGER,
    ssr_active      TEXT,
    country         TEXT,
    UNIQUE (report_date, ticker)
);
CREATE INDEX IF NOT EXISTS ix_gappers_ticker ON gappers(ticker);
CREATE INDEX IF NOT EXISTS ix_reports_date ON reports(report_date);

-- Scheduled events and their expectations, so the close can be judged against
-- what was expected that morning rather than against a consensus that has
-- since been revised. `expected` is written once and never overwritten; the
-- close only fills in `actual`. Same reasoning as the gappers table: the value
-- of the row is that it records what was known at the time.
CREATE TABLE IF NOT EXISTS calendar_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date      TEXT    NOT NULL,          -- YYYY-MM-DD
    kind            TEXT    NOT NULL,          -- econ | earnings
    key             TEXT    NOT NULL,          -- event family, or earn:TICKER
    label           TEXT,
    time_et         TEXT,
    session         TEXT,                      -- bmo | amc, earnings only
    score           REAL,
    expected        TEXT,
    previous        TEXT,
    actual          TEXT,
    surprise        TEXT,
    recorded_ts     TEXT    NOT NULL,
    updated_ts      TEXT,
    UNIQUE (event_date, kind, key)
);
CREATE INDEX IF NOT EXISTS ix_calendar_date ON calendar_events(event_date);
"""


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    # WAL so the bot and the scheduled reports can write without blocking
    # each other -- they are separate processes hitting the same file.
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    # Columns added after the first rows were written; ALTER is a no-op once
    # they exist, and sqlite has no "add column if not exists".
    have = {r["name"] for r in con.execute("PRAGMA table_info(lookups)")}
    for col, decl in (("hits", "INTEGER NOT NULL DEFAULT 1"),
                      ("last_ts", "TEXT")):
        if col not in have:
            con.execute(f"ALTER TABLE lookups ADD COLUMN {col} {decl}")
    return con


def _num(v):
    return None if v is None else float(v)


def record_pillars(r, user_id=None):
    """Persist a scorecard. Returns the row id, or None."""
    if not r or "error" in r or r.get("insufficient"):
        return None
    detail = {
        "pillars": [{"n": p.n, "name": p.name, "value": p.value,
                     "passed": p.passed} for p in r.get("pillars", [])],
    }
    return _insert({
        "ticker": r["ticker"], "command": "pillars", "name": r.get("name"),
        "price": _num(r.get("price")), "mcap": _num(r.get("mcap")),
        "pillars_passed": r.get("passed"), "pillars_scored": r.get("scored"),
        "requested_by": user_id, "detail": json.dumps(detail),
    })


def record_price(r, user_id=None):
    """Persist a valuation. Returns the row id, or None."""
    if not r or "error" in r:
        return None
    em = r.get("em") or {}
    d = r.get("dcf") or {}
    detail = {"warnings": r.get("warnings", []),
              "pe_series": r.get("pe_series", []),
              "peers": [c["ticker"] for c in (r.get("comps") or [])][1:]}
    return _insert({
        "ticker": r["ticker"], "command": "price", "name": r.get("name"),
        "price": _num(r.get("price")),
        "dcf_low": _num(d.get("low")), "dcf_base": _num(d.get("base")),
        "dcf_high": _num(d.get("high")),
        "implied_growth": _num(r.get("implied")),
        "target_5y_10": _num(em.get((5, "10%"))),
        "target_5y_15": _num(em.get((5, "15%"))),
        "target_10y_10": _num(em.get((10, "10%"))),
        "target_10y_15": _num(em.get((10, "15%"))),
        "exit_pe": _num(r.get("exit_pe")),
        "rev_growth": _num(r.get("g_rev")), "net_margin": _num(r.get("margin")),
        "fcf": _num(r.get("fcf0")),
        "requested_by": user_id, "detail": json.dumps(detail),
    })


# Repeats inside this window that changed nothing are folded into the existing
# row. Matched to the 24h filings cache: within it the models are fed identical
# inputs, so a second lookup is the same observation, not a new one.
DEDUP_WINDOW_HOURS = 24

# The fields whose change means "this is genuinely a new observation". Price is
# excluded on purpose -- it ticks constantly, and deduping on it would insert a
# row for every lookup, which is the behaviour being fixed.
MATERIAL = ("pillars_passed", "pillars_scored", "dcf_base", "dcf_low",
            "dcf_high", "implied_growth", "target_5y_10", "target_10y_10",
            "exit_pe", "rev_growth", "net_margin", "fcf")


def _same_observation(prev, row):
    """True when every material model output is unchanged."""
    for k in MATERIAL:
        a, b = prev[k] if k in prev.keys() else None, row.get(k)
        if a is None and b is None:
            continue
        if a is None or b is None:
            return False
        if abs(float(a) - float(b)) > 1e-6:
            return False
    return True


def _insert(row):
    now = datetime.now(timezone.utc)
    row["ts"] = now.isoformat(timespec="seconds")
    cutoff = (now - timedelta(hours=DEDUP_WINDOW_HOURS)).isoformat(timespec="seconds")
    try:
        with connect() as con:
            prev = con.execute(
                "SELECT * FROM lookups WHERE ticker = ? AND command = ? "
                "AND ts >= ? ORDER BY ts DESC LIMIT 1",
                (row["ticker"], row["command"], cutoff)).fetchone()
            if prev and _same_observation(prev, row):
                # Same numbers, so record that it was looked at again rather
                # than storing a duplicate that pollutes the time series.
                con.execute(
                    "UPDATE lookups SET hits = hits + 1, last_ts = ?, price = ? "
                    "WHERE id = ?",
                    (row["ts"], row.get("price"), prev["id"]))
                return prev["id"]
            cols = ", ".join(row)
            marks = ", ".join("?" * len(row))
            cur = con.execute(f"INSERT INTO lookups ({cols}) VALUES ({marks})",
                              list(row.values()))
            return cur.lastrowid
    except Exception as e:
        # A lost history row must never cost the user their reply.
        print(f"  ! history write failed: {e}", file=sys.stderr)
        return None


def _ctx_row(ctx):
    """Flatten a market context into report columns."""
    q = (ctx or {}).get("quotes") or {}
    t = (ctx or {}).get("trend") or {}
    f = (ctx or {}).get("fng") or {}
    a = (ctx or {}).get("aaii") or {}
    num = lambda sym: (_num(_parse(q.get(sym, {}).get("last")))
                       if q.get(sym) else None)
    return {
        "trend_label": t.get("label"),
        "spx": _num(t.get("last")),
        "spx_off_high": _num(t.get("off_high")),
        "fng_score": _num(f.get("score")), "fng_rating": f.get("rating"),
        "vix": num(".VIX"), "wti": num("@CL.1"), "brent": num("@BZ.1"),
        "gold": num("@GC.1"), "btc": num("BTC.CM="), "us10y": num("US10Y"),
        "aaii_bull": _num(a.get("bullish")), "aaii_bear": _num(a.get("bearish")),
        "divergence": 1 if (ctx or {}).get("divergence") else 0,
    }


def _parse(s):
    """'7,740.24' / '4.961%' -> float."""
    if s is None:
        return None
    s = str(s).replace(",", "").replace("%", "").replace("$", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def record_report(kind, report_date, ctx, rows=None, macro_count=None,
                  note_path=None):
    """
    Persist a scheduled report and, for a premarket run, its watchlist.

    Idempotent on (report_date, kind): a re-run replaces the day's row rather
    than adding a second, so the `-auto.md` second report does not double-count.
    """
    row = {"report_date": report_date, "kind": kind,
           "generated_ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "gapper_count": len(rows) if rows else None,
           "macro_count": macro_count, "note_path": note_path}
    row.update(_ctx_row(ctx))
    try:
        with connect() as con:
            cols = ", ".join(row)
            marks = ", ".join("?" * len(row))
            updates = ", ".join(f"{c}=excluded.{c}" for c in row
                                if c not in ("report_date", "kind"))
            cur = con.execute(
                f"INSERT INTO reports ({cols}) VALUES ({marks}) "
                f"ON CONFLICT(report_date, kind) DO UPDATE SET {updates}",
                list(row.values()))
            rid = cur.lastrowid or con.execute(
                "SELECT id FROM reports WHERE report_date=? AND kind=?",
                (report_date, kind)).fetchone()["id"]

            for i, r in enumerate(rows or [], 1):
                g = {
                    "report_id": rid, "report_date": report_date, "rank": i,
                    "ticker": (r.get("ticker") or "").upper(),
                    "gap_pct": _num(r.get("gap_pct")),
                    "pm_price": _num(r.get("pm_price")),
                    "pm_volume": _num(r.get("pm_volume")),
                    "float_shares": _num(r.get("float_shares")),
                    "float_rotation": _num(r.get("float_rotation")),
                    "short_float_pct": _num(r.get("short_float_pct")),
                    "rel_volume": _num(r.get("rel_volume")),
                    "dilution_risk": r.get("dilution_risk"),
                    "shelf_count": r.get("shelf_count"),
                    "days_since_offering": r.get("days_since_offering"),
                    "ssr_active": r.get("ssr_active"),
                    "country": r.get("country"),
                }
                if not g["ticker"]:
                    continue
                gc = ", ".join(g)
                gm = ", ".join("?" * len(g))
                gu = ", ".join(f"{c}=excluded.{c}" for c in g
                               if c not in ("report_date", "ticker"))
                con.execute(
                    f"INSERT INTO gappers ({gc}) VALUES ({gm}) "
                    f"ON CONFLICT(report_date, ticker) DO UPDATE SET {gu}",
                    list(g.values()))
            return rid
    except Exception as e:
        print(f"  ! report history write failed: {e}", file=sys.stderr)
        return None


GAPPER_WATCH_DAYS = 4


def todays_gappers(limit=40, max_age_days=GAPPER_WATCH_DAYS):
    """
    The tickers on the most recent premarket screen, if it is still current.

    Used to rank an earnings date on a name already being watched above one
    that merely has a large market cap. Bounded by age on purpose: a watchlist
    from last month is not a watchlist, and silently carrying it forward would
    flag names nobody has looked at in weeks.
    """
    try:
        with connect() as con:
            latest = con.execute(
                "SELECT MAX(report_date) AS d FROM gappers").fetchone()
            day = latest["d"] if latest else None
            if not day:
                return []
            age = (datetime.now().date()
                   - datetime.strptime(day, "%Y-%m-%d").date()).days
            if age > max_age_days:
                return []
            return [dict(r) for r in con.execute(
                "SELECT * FROM gappers WHERE report_date = ? "
                "ORDER BY rank LIMIT ?", (day, limit))]
    except Exception as e:
        print(f"  ! gapper watchlist read failed: {e}", file=sys.stderr)
        return []


def record_calendar(event_date, events):
    """
    Persist the day's scheduled events. Returns how many rows were written.

    `expected` and `previous` are written once and then left alone: the morning
    report's job is to record what the market was looking for, and a consensus
    revised during the session must not be allowed to rewrite that. Only
    `actual` and `surprise` are updated afterwards, and only once they carry a
    value -- a later run that finds nothing must not erase a print.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    written = 0
    try:
        with connect() as con:
            for ev in events or []:
                row = {
                    "event_date": event_date, "kind": ev["kind"],
                    "key": ev["family"], "label": ev.get("label"),
                    "time_et": (f"{ev['when']:%H:%M}" if ev.get("when") else None),
                    "session": ev.get("session"), "score": _num(ev.get("score")),
                    "expected": ev.get("expected"), "previous": ev.get("previous"),
                    "actual": ev.get("actual"), "surprise": ev.get("surprise"),
                    "recorded_ts": now, "updated_ts": now,
                }
                cols = ", ".join(row)
                marks = ", ".join("?" * len(row))
                con.execute(
                    f"INSERT INTO calendar_events ({cols}) VALUES ({marks}) "
                    "ON CONFLICT(event_date, kind, key) DO UPDATE SET "
                    "  label = excluded.label,"
                    "  time_et = COALESCE(excluded.time_et, calendar_events.time_et),"
                    "  session = COALESCE(excluded.session, calendar_events.session),"
                    "  score = excluded.score,"
                    # Written once: the morning's expectation is the record.
                    "  expected = COALESCE(calendar_events.expected, excluded.expected),"
                    "  previous = COALESCE(calendar_events.previous, excluded.previous),"
                    # Filled in later, never blanked out by a run that came up empty.
                    "  actual = COALESCE(excluded.actual, calendar_events.actual),"
                    "  surprise = COALESCE(excluded.surprise, calendar_events.surprise),"
                    "  updated_ts = excluded.updated_ts",
                    list(row.values()))
                written += 1
            return written
    except Exception as e:
        print(f"  ! calendar write failed: {e}", file=sys.stderr)
        return 0


def reports(kind=None, limit=20):
    try:
        with connect() as con:
            if kind:
                q = ("SELECT * FROM reports WHERE kind=? "
                     "ORDER BY report_date DESC LIMIT ?")
                return [dict(r) for r in con.execute(q, (kind, limit))]
            return [dict(r) for r in con.execute(
                "SELECT * FROM reports ORDER BY report_date DESC, kind LIMIT ?",
                (limit,))]
    except Exception as e:
        print(f"  ! reports read failed: {e}", file=sys.stderr)
        return []


def history(ticker, limit=20):
    """Every lookup for one ticker, newest first."""
    try:
        with connect() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM lookups WHERE ticker = ? "
                "ORDER BY ts DESC LIMIT ?",
                (ticker.upper().lstrip("$"), limit))]
    except Exception as e:
        print(f"  ! history read failed: {e}", file=sys.stderr)
        return []


def summary(limit=30):
    """What has been researched, most recently seen first."""
    try:
        with connect() as con:
            return [dict(r) for r in con.execute(
                "SELECT ticker, SUM(hits) AS lookups, "
                "       COUNT(*) AS observations, "
                "       MAX(COALESCE(last_ts, ts)) AS last_seen, "
                "       MAX(pillars_passed) AS best_score, "
                "       COUNT(DISTINCT command) AS commands "
                "FROM lookups GROUP BY ticker ORDER BY last_seen DESC LIMIT ?",
                (limit,))]
    except Exception as e:
        print(f"  ! summary failed: {e}", file=sys.stderr)
        return []


def telegram_history(ticker, limit=6):
    """A compact history for the chat."""
    rows = history(ticker, limit * 2)
    if not rows:
        return f"📭 No recorded lookups for <b>{ticker.upper()}</b> yet."
    t = rows[0]["ticker"]
    L = [f"🗂 <b>{t}</b> — last {min(len(rows), limit)} lookups", ""]
    for r in rows[:limit]:
        day = r["ts"][:10]
        if r["command"] == "pillars":
            L.append(f"<b>{day}</b> · pillars "
                     f"<b>{r['pillars_passed']}/{r['pillars_scored']}</b>"
                     + (f" · ${r['price']:,.2f}" if r["price"] else ""))
        else:
            bits = []
            if r["dcf_base"]:
                bits.append(f"DCF ${r['dcf_base']:,.0f}")
            if r["implied_growth"] is not None:
                bits.append(f"implied {r['implied_growth']*100:.1f}%")
            L.append(f"<b>{day}</b> · price "
                     + (f"${r['price']:,.2f} · " if r["price"] else "")
                     + " · ".join(bits))
    L += ["", "<i>Recorded at the time of each lookup, not recomputed.</i>"]
    return "\n".join(L)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--summary" in sys.argv or not args:
        rows = summary()
        if not rows:
            print(f"No lookups recorded yet. ({DB})")
            return 0
        print(f"{'TICKER':8} {'LOOKUPS':>7} {'ROWS':>5}  {'BEST':>5}  LAST SEEN")
        for r in rows:
            best = "—" if r["best_score"] is None else f"{r['best_score']}/8"
            print(f"{r['ticker']:8} {r['lookups']:>7} {r['observations']:>5}  "
                  f"{best:>5}  {r['last_seen']}")
        return 0
    for r in history(args[0]):
        print(json.dumps({k: v for k, v in r.items() if v is not None},
                         indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
