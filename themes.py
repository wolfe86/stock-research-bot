"""
Theme Watch: what the small-cap runners have been running on, read before the
open from a theme archive -- a SQLite file with one row per stock per theme
per session, written during the day by whatever screener you run.

The model: a theme is any repeatable pattern, and it is established over 3+
sessions, not one. So this reads the last few full sessions and says what is
established, warming or cooling -- and says plainly when nothing is
established yet, which is a finding in itself.

The archive is optional and nothing in this repository writes it. Point
SRB_THEME_DB at it (default data/themes.sqlite). Without it the premarket
report prints "Theme archive not available" and carries on. Expected table:

    CREATE TABLE thememembers (
        day      TEXT,   -- session date, YYYY-MM-DD
        symbol   TEXT,
        theme    TEXT,   -- a key from NAMES below, "s-<sector>", "other:<label>";
                         -- keys starting "c-" are ignored
        peak     REAL,   -- the stock's peak % gain on the session
        headline TEXT    -- its catalyst headline, if any
    );

Two lenses, kept apart because a sector is not a theme:
  - Catalyst clusters: the runners' own headlines, bucketed by keyword.
    Corporate plumbing (reverse splits, offerings) is the most common real
    theme on the small-cap board and theme tags cannot see it.
  - Tagged themes: the archive's named themes (ai, defense, foreignlf...),
    ranked by hit rate against the whole board, never by member count.

Stdlib only, read-only on the archive. Beta: thresholds below are first
guesses until there are weeks of sessions to tune them against.
"""

import re
import sqlite3
from datetime import date

import settings

DB = settings.THEME_DB

RAN = 20.0           # a "runner" peaked at or above this % on the session
REPEAT = 30.0        # repeat-runner bar: this % on 2+ sessions in the window
WINDOW = 5           # full sessions read
FULL_SESSION = 200   # rows needed to count a day as a full session
ESTABLISHED = 3      # hot sessions in the window to call a theme established

# Catalyst clusters, first match wins, so the specific ones come first.
CLUSTERS = [
    ("Reverse split / listing compliance",
     r"reverse (stock |share )?split|ratio change|1-for-\d|compliance|delist|bid price|deficiency"),
    ("Offering / financing",
     r"offering|private placement|financing|registered direct|warrant|convertible|notes? (repaid|offering)"),
    ("M&A / strategic deal",
     r"acqui|merger|to combine|term sheet|strategic (transaction|alternative)|buyout|tender offer"),
    ("Clinical / FDA",
     r"\bfda\b|trial|clinical|\bnda\b|phase [123i]|study|patients"),
    ("AI",
     r"\bai\b|agentic|artificial intelligence|machine learning"),
    ("Crypto / treasury",
     r"bitcoin|crypto|token|ethereum|solana|digital asset|treasury strategy"),
    ("Contract / order",
     r"contract|award|purchase order|\border\b|partnership|agreement"),
    ("Earnings / guidance",
     r"results|earnings|guidance|quarter|revenue"),
    ("Dividend / buyback",
     r"dividend|buyback|repurchase"),
]

# Theme keys -> readable names. Unknown keys print as-is.
NAMES = {
    "ai": "AI", "defense": "Defense", "oil": "Oil", "minerals": "Critical minerals",
    "foreignlf": "Foreign low-float", "nonews": "No-news runners", "nuclear": "Nuclear",
    "quantum": "Quantum", "space": "Space", "biotech": "Biotech", "crypto": "Crypto",
    "s-health": "Healthcare", "s-tech": "Technology", "s-industrials": "Industrials",
    "s-financial": "Financials", "s-cyclical": "Consumer cyclical",
    "s-defensive": "Consumer defensive", "s-energy": "Energy", "s-materials": "Materials",
    "s-comms": "Communications", "s-realestate": "Real estate", "s-utilities": "Utilities",
}


def name(key):
    if key.startswith("other:"):
        return key[6:]
    return NAMES.get(key, key)


def _sessions(con, today):
    """Last WINDOW full weekday sessions before today, oldest first."""
    rows = con.execute(
        "select day, count(*) from thememembers where day < ? group by day "
        "order by day desc", (today.isoformat(),)).fetchall()
    out = [d for d, n in rows
           if n >= FULL_SESSION and date.fromisoformat(d).weekday() < 5]
    return sorted(out[:WINDOW])


def _status(hot, vals):
    """hot: per-session booleans, vals: per-session strength, both oldest first."""
    k = sum(hot)
    if k >= ESTABLISHED:
        return "Established"
    if len(vals) >= 2:
        last, prev = vals[-1], vals[-2]
        if hot[-1] and (not hot[-2] or last > prev):
            return "Warming"
        if hot[-2] and not hot[-1]:
            return "Cooling"
        if hot[-1]:
            return "Holding"
        if prev > 0 and last < prev * 0.6:
            return "Fading"
    elif hot and hot[-1]:
        return "Warming"
    return "Quiet"


def read(today=None):
    """Return the theme read as a dict, or None when the archive is missing."""
    today = today or date.today()
    if not DB.exists():
        return None
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        days = _sessions(con, today)
        if not days:
            return None
        q = ",".join("?" * len(days))

        # One row per stock per session: peak and headline.
        stocks = con.execute(
            f"select day, symbol, max(peak), max(coalesce(headline,'')) "
            f"from thememembers where day in ({q}) and theme not like 'c-%' "
            f"group by day, symbol", days).fetchall()
        board = {d: [0, 0] for d in days}       # day -> [names, runners]
        runners = {d: [] for d in days}         # day -> [(symbol, peak, headline)]
        hist = {}                               # symbol -> [(day, peak)]
        for d, sym, peak, head in stocks:
            board[d][0] += 1
            if (peak or 0) >= RAN:
                board[d][1] += 1
                runners[d].append((sym, peak, head))
            if (peak or 0) >= REPEAT:
                hist.setdefault(sym, []).append((d, peak))

        # Catalyst clusters: share of each session's runners.
        clusters = []
        for label, rx in CLUSTERS:
            per, names = [], []
            for d in days:
                hits = []
                for sym, peak, head in runners[d]:
                    first = next((l for l, r in CLUSTERS if re.search(r, head, re.I)), None)
                    if first == label:
                        hits.append((sym, peak))
                per.append(len(hits))
                if d == days[-1]:
                    names = sorted(hits, key=lambda h: -h[1])
            share = [per[i] / board[d][1] if board[d][1] else 0 for i, d in enumerate(days)]
            hot = [per[i] >= 3 and share[i] >= 0.10 for i in range(len(days))]
            clusters.append({"name": label, "counts": per, "share": share,
                             "status": _status(hot, share), "latest": names[:4]})
        clusters = [c for c in clusters if any(c["counts"])]
        # Cooling means it was hot; Fading only means fewer names, so it sorts last.
        rank = {"Established": 0, "Warming": 1, "Holding": 2, "Cooling": 3,
                "Quiet": 4, "Fading": 5}
        clusters.sort(key=lambda c: (rank[c["status"]], -c["counts"][-1]))

        # Tagged themes: hit rate against the board's own hit rate.
        tagged = {}
        for d, theme, n, r in con.execute(
                f"select day, theme, count(*), sum(peak >= ?) from thememembers "
                f"where day in ({q}) and theme not like 'c-%' group by day, theme",
                [RAN] + days):
            tagged.setdefault(theme, {})[d] = (n, r)
        themes = []
        for theme, by_day in tagged.items():
            ns = [by_day.get(d, (0, 0)) for d in days]
            rate = [r / n if n else 0 for n, r in ns]
            base = [board[d][1] / board[d][0] if board[d][0] else 0 for d in days]
            hot = [ns[i][1] >= 3 and rate[i] >= 2 * base[i] for i in range(len(days))]
            if not any(r for _, r in ns):
                continue
            themes.append({"key": theme, "name": name(theme),
                           "sector": theme.startswith("s-"),
                           "members": ns[-1][0], "runners": ns[-1][1],
                           "rate": rate, "lift": rate[-1] / base[-1] if base[-1] else 0,
                           "status": _status(hot, rate)})
        themes.sort(key=lambda t: (rank[t["status"]], -t["lift"]))

        repeats = sorted(((s, v) for s, v in hist.items() if len(v) >= 2),
                         key=lambda sv: (-len(sv[1]), -sv[1][-1][1]))
    finally:
        con.close()

    return {"days": days, "board": board, "clusters": clusters,
            "themes": themes, "repeats": repeats}


def _pct(x):
    return f"{x * 100:.0f}%"


def markdown_block(today, gapper_tickers=()):
    """The Theme Watch section for the premarket report, as a list of lines."""
    t = read(today)
    L = ["", "## 🎯 Theme Watch", ""]
    if not t:
        return L + ["*Theme archive not available — no full session recorded yet "
                    "(see SRB_THEME_DB).*", "", "---"]

    days = t["days"]
    short = [date.fromisoformat(d).strftime("%a %-d") for d in days]
    est = [c["name"] for c in t["clusters"] if c["status"] == "Established"] + \
          [x["name"] for x in t["themes"] if x["status"] == "Established" and not x["sector"]]
    warm = [c["name"] for c in t["clusters"] if c["status"] in ("Warming", "Holding")] + \
           [x["name"] for x in t["themes"] if x["status"] in ("Warming", "Holding")]
    cool = [c["name"] for c in t["clusters"] if c["status"] == "Cooling"] + \
           [x["name"] for x in t["themes"] if x["status"] == "Cooling"]

    if est:
        head = f"**Theme: {', '.join(est)}.** Hot on {ESTABLISHED}+ of the last {len(days)} sessions."
    else:
        head = (f"**No established theme yet — keep hunting.** "
                f"{len(days)} full session{'s' if len(days) != 1 else ''} on record; "
                f"a theme needs {ESTABLISHED}+.")
    L += ["> [!tip] The read", f"> {head}"]
    if warm:
        L += [">", f"> 🔺 **Warming / holding:** {', '.join(dict.fromkeys(warm))}"]
    if cool:
        L += [">", f"> 🔻 **Cooling:** {', '.join(dict.fromkeys(cool))}"]
    on_list = [g for g in gapper_tickers if g in dict(t["repeats"])]
    if on_list:
        L += [">", f"> 🔁 **Repeat runners on today's list:** {', '.join(on_list)}"]
    L.append("")

    b = t["board"]
    L += [f"*Runners = peaked +{RAN:.0f}% or more on the session. "
          "Board: " + " · ".join(f"{s} {b[d][1]}/{b[d][0]}" for s, d in zip(short, days))
          + ".*", ""]

    L += ["**Catalyst clusters** — what the runners' headlines said, share of each session's runners", "",
          "| Cluster | " + " | ".join(short) + " | Status | Latest names |",
          "|---|" + "---|" * len(days) + "---|---|"]
    for c in t["clusters"][:7]:
        cells = " | ".join(f"{n} ({_pct(s)})" if n else "—"
                           for n, s in zip(c["counts"], c["share"]))
        names = ", ".join(f"{s} +{p:.0f}%" for s, p in c["latest"]) or "—"
        L.append(f"| {c['name']} | {cells} | {c['status']} | {names} |")
    L.append("")

    tagged = [x for x in t["themes"] if not x["sector"]][:6]
    sectors = [x for x in t["themes"] if x["sector"]][:4]
    L += ["**Tagged themes** — hit rate, and lift over the whole board on the latest session", "",
          "| Theme | " + " | ".join(short) + " | Lift | Status |",
          "|---|" + "---|" * len(days) + "---|---|"]
    for x in tagged + sectors:
        label = f"{x['name']} *(sector)*" if x["sector"] else x["name"]
        cells = " | ".join(_pct(r) for r in x["rate"])
        L.append(f"| {label} | {cells} | {x['lift']:.1f}× | {x['status']} |")
    L.append("")

    if t["repeats"]:
        L.append(f"**Repeat runners** (+{REPEAT:.0f}% on 2+ sessions): " + ", ".join(
            f"**{s}** " + "/".join(f"+{p:.0f}%" for _, p in v) for s, v in t["repeats"][:10]))
        L.append("")

    L += ["*Beta. A sector is not a theme until its runners share a catalyst. "
          "Source: the theme archive.*", "", "---"]
    return L
