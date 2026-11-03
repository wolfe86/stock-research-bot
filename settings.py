#!/usr/bin/env python3
"""
Shared settings: where things live, the time zone, and the credentials.

Importing this module loads credentials into the environment from, in order,
a `.env` file next to the scripts and `~/.config/stock-research-bot.env`.
Values already in the environment win, so a systemd EnvironmentFile or an
exported variable always takes precedence over either file.

Anything UPPER_CASE here can be overridden in a git-ignored local_settings.py
(see local_settings.example.py). The feed lists for the alerts live in
market_alerts_sources.py and take the same overrides.

Environment variables:
    TELEGRAM_BOT_TOKEN   from @BotFather
    TELEGRAM_CHAT_ID     the broadcast channel the reports and alerts post to
    TELEGRAM_GROUP_ID    the discussion group the command bot answers in
    TELEGRAM_OWNER_ID    optional: a user allowed to DM the bot
    SEC_USER_AGENT       required for SEC EDGAR lookups: "Your Name you@example.com"
    SRB_OUTPUT_DIR       where reports and notes are written (default data/reports)
    SRB_THEME_DB         optional theme archive for the premarket Theme Watch
    CLAUDE_BIN           Claude Code CLI for the optional digest/weekly/judge runs
    YT_TRANSCRIPT_BIN    optional command that writes a YouTube transcript

Stdlib only.
"""

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
ENV_FILE = Path.home() / ".config" / "stock-research-bot.env"


def load_env(path):
    """KEY=value lines into os.environ, without overriding what is set."""
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env(BASE / ".env")
load_env(ENV_FILE)

# --- Defaults (override in local_settings.py) --------------------------------

# Local time zone for schedules, file dates and the times printed in reports.
# TIMEZONE_LABEL is the short name printed next to local times.
TIMEZONE = "America/New_York"
TIMEZONE_LABEL = "ET"

# Extra tickers the calendar should always list when they report earnings, on
# top of large caps (calendar_events.CARE_FLOOR), the morning's gappers and the
# alerts focus watchlist.
CALENDAR_WATCHLIST = []

# Model names passed to `claude -p`.
DIGEST_MODEL = "sonnet"
WEEKLY_MODEL = "sonnet"
JUDGE_MODEL = "haiku"

try:
    import local_settings as _local
except ImportError:
    _local = None
if _local is not None:
    globals().update({k: v for k, v in vars(_local).items() if k.isupper()})

# --- Derived ------------------------------------------------------------------

LOCAL = ZoneInfo(TIMEZONE)
ET = ZoneInfo("America/New_York")

OUTPUT_DIR = Path(os.environ.get("SRB_OUTPUT_DIR", "").strip()
                  or DATA / "reports").expanduser()
THEME_DB = Path(os.environ.get("SRB_THEME_DB", "").strip()
                or DATA / "themes.sqlite").expanduser()
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "").strip() or "claude"
YT_TRANSCRIPT_BIN = os.environ.get("YT_TRANSCRIPT_BIN", "").strip()


def sec_user_agent():
    """
    The User-Agent SEC EDGAR requires: a name and a contact email.

    SEC blocks generic or missing agents, so there is no default -- an
    anonymous agent would only fail later with a less useful 403.
    """
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        raise RuntimeError(
            "SEC_USER_AGENT is not set. SEC EDGAR requires a User-Agent naming "
            "you and a contact email, e.g. SEC_USER_AGENT=\"Jane Doe "
            "jane@example.com\". Put it in .env or ~/.config/stock-research-bot.env.")
    return ua


def et_hours_local(start_h, end_h, day=None):
    """An ET hour window as local hours, e.g. 4-5am ET -> "3–4am CT"."""
    day = day or datetime.now(ET).date()

    def conv(h):
        t = datetime(day.year, day.month, day.day, h, tzinfo=ET).astimezone(LOCAL)
        return t.hour

    a, b = conv(start_h), conv(end_h)
    fmt = lambda h: str(h % 12 or 12)  # noqa: E731
    ap = lambda h: "am" if h < 12 else "pm"  # noqa: E731
    first = fmt(a) if ap(a) == ap(b) else fmt(a) + ap(a)
    return f"{first}–{fmt(b)}{ap(b)} {TIMEZONE_LABEL}"
