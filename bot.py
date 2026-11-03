#!/usr/bin/env python3
"""
Telegram command bot for the channel's linked discussion group.

Long-polls getUpdates and answers commands. Runs as a systemd user service --
unlike the reports, which fire and exit, this one stays up.

Commands:
    /pillars <ticker>   Eight Pillars of Fundamental Valuation, from SEC EDGAR filings
    /fundamentals <ticker>
                        Briefing in chat + full report attached and saved
    /history <ticker>   Past lookups for a ticker, as recorded at the time
    /news [N|ticker]    Most recent market headlines, or one company's
    /calendar [days]    The week's most significant scheduled events -- rate
                        decisions, inflation and jobs prints, big reporters --
                        with the full calendar attached
    /price <ticker> [peers...]
                        DCF, target prices for a 10%/15% return, reverse DCF,
                        and a comparables table against any peers named
    /help               What the bot can do

> The channel itself is broadcast-only: subscribers cannot post there, so
> commands are read from the linked discussion group (and from direct messages
> when the sender is allowed). Results are posted back to wherever the command
> came from, never to the channel.

Safety, because anyone in the group can type here:
  * Tickers are validated against a strict pattern before any lookup; nothing
    a user types is ever interpolated into a shell, a path or a URL unescaped.
  * Per-user rate limit, so one person cannot pin the SEC endpoints.
  * Chat allowlist: the bot ignores chats it was not configured for.
  * Only known commands are answered; anything else is silently ignored, so the
    bot never chats in a group it happens to be in.

  * Lookup failures are caught per command: one bad ticker never takes the
    service down, and the user gets a one-line "could not build" reply.

Environment (.env, ~/.config/stock-research-bot.env, or the process env):
    TELEGRAM_BOT_TOKEN   required
    TELEGRAM_GROUP_ID    the discussion group; commands are accepted here
    TELEGRAM_OWNER_ID    optional, allows that user to DM the bot
    SEC_USER_AGENT       required by /pillars, /price and /fundamentals

If neither TELEGRAM_GROUP_ID nor TELEGRAM_OWNER_ID is set, the allowlist is
empty and the bot answers in any chat it is added to -- set at least one.

Stdlib only.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from datetime import datetime

import cache
import calendar_events as cal
import dcf as dcf_mod
import fundamentals as fund_mod
import news
import notify
import pillars
import quote
import settings
import store

API = "https://api.telegram.org/bot{token}/{method}"
STATE = settings.DATA / "bot_state"
POLL_TIMEOUT = 50           # seconds held open by Telegram per getUpdates call
MAX_REPLY = 3900

# A ticker, and nothing that could be anything else. Length capped well below
# anything that could be abusive; dots and dashes allowed for class shares.
TICKER_RE = re.compile(r"^[A-Za-z][A-Za-z.\-]{0,6}$")

RATE_N, RATE_WINDOW = 5, 60   # 5 commands per user per minute
_recent = defaultdict(lambda: deque(maxlen=RATE_N))

HELP = (
    "<b>Stock Research Bot</b>\n\n"
    "<b>/pillars TICKER</b> — the Eight Pillars of Fundamental Valuation, "
    "scored from SEC EDGAR 10-K filings. Example: <code>/pillars ADBE</code>\n"
    "<b>/price TICKER [PEER...]</b> — valuation: a discounted cash flow, target "
    "prices for a 10% and 15% return over 5 and 10 years, what growth today's "
    "price already assumes, and a comparables table against any peers you name. "
    "Example: <code>/price ADBE CRM</code>\n"
    "<b>/fundamentals TICKER</b> — a briefing: pillars, valuation, why any "
    "pillar failed, and a company headline — with the full report attached as "
    "a file. Example: <code>/fundamentals ADBE</code>\n"
    "<b>/history TICKER</b> — what this bot has recorded for a ticker over "
    "time, as it stood on each date. Example: <code>/history ADBE</code>\n"
    "<b>/calendar</b> — the week's most significant scheduled events: rate "
    "decisions, inflation and jobs numbers, and the big names reporting, with "
    "what the market expects from each. The full week comes attached.\n"
    "<b>/news [N|TICKER]</b> — the most recent market headlines. "
    "<code>/news</code> gives five, <code>/news 10</code> gives ten, and "
    "<code>/news AAPL</code> gives that company's news, its own announcements "
    "first.\n"
    "<b>/help</b> — this message\n\n"
    "<i>Tickers work with or without the $, in any case. "
    "<code>/dcf</code> still works as the old name for <code>/price</code>.</i>\n\n"
    "<i>All of it is a screen, not an analysis, and not investment advice. The "
    "pillars come from filings; the valuations are assumptions, printed beside "
    "every number so you can disagree with them. Where a figure cannot be "
    "computed the bot says so rather than inventing one \u2014 banks and insurers "
    "report no capex, so the pillar and valuation commands do not apply to "
    "them and say so.</i>"
)


def api(method, **params):
    """Call the Bot API. Returns the `result`, or None. Never raises."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("! TELEGRAM_BOT_TOKEN not set", file=sys.stderr)
        return None
    data = urllib.parse.urlencode(params).encode() if params else None
    req = urllib.request.Request(API.format(token=token, method=method), data=data)
    try:
        with urllib.request.urlopen(req, timeout=POLL_TIMEOUT + 15) as r:
            body = json.loads(r.read().decode("utf-8", errors="ignore"))
        if body.get("ok"):
            return body.get("result")
        # A 200 carrying ok:false used to return None without a word, so a
        # rejected post looked exactly like a delivered one in the log.
        print(f"! {method} rejected: {str(body)[:200]}", file=sys.stderr)
        return None
    except urllib.error.HTTPError as e:
        # Status and body only -- the URL carries the token.
        print(f"! {method} HTTP {e.code}: "
              f"{e.read().decode('utf-8', 'ignore')[:200]}", file=sys.stderr)
    except Exception as e:
        print(f"! {method} failed: {e}", file=sys.stderr)
    return None


def reply(chat_id, text, reply_to=None):
    params = {"chat_id": chat_id, "text": text[:MAX_REPLY], "parse_mode": "HTML",
              "disable_web_page_preview": "true"}
    if reply_to:
        params["reply_to_message_id"] = reply_to
    api("sendMessage", **params)


def allowed_chats():
    ids = set()
    for key in ("TELEGRAM_GROUP_ID", "TELEGRAM_OWNER_ID"):
        v = os.environ.get(key, "").strip()
        if v:
            ids.add(v)
    return ids


def rate_limited(user_id):
    now = time.monotonic()
    q = _recent[user_id]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_N:
        return True
    q.append(now)
    return False


def handle_ticker_cmd(cmd, chat_id, msg_id, arg, user_id=None):
    """Shared path for /pillars and /dcf: same validation, same failure mode."""
    module, what = ((pillars, "scorecard") if cmd == "/pillars"
                    else (dcf_mod, "valuation"))
    if not arg:
        reply(chat_id, f"Usage: <code>{cmd} ADBE</code>", msg_id)
        return

    # First word is the subject; any further words are peers to compare against
    # (/price only). Every one is validated the same way -- a peer list is user
    # input too.
    words = [w.lstrip("$").upper() for w in arg.split()][:4]
    bad = [w for w in words if not TICKER_RE.match(w)]
    if bad or not words:
        reply(chat_id, "That does not look like a ticker. "
                       f"Example: <code>{cmd} ADBE</code>", msg_id)
        return
    tick, peers = words[0], words[1:]

    print(f"  {cmd} {tick}" + (f" vs {','.join(peers)}" if peers else ""), flush=True)
    # The disk cache still serves; this only stops a long-lived process pinning
    # one price in memory for hours.
    cache.clear_memory()
    try:
        if module is dcf_mod:
            r = module.analyse(tick, quote.get(tick), peers=peers)
        else:
            r = module.analyse(tick, quote.get(tick))
        reply(chat_id, module.telegram(r), msg_id)
        # Record after replying: the history is a by-product, never a
        # precondition for the answer.
        if module is dcf_mod:
            store.record_price(r, user_id)
        else:
            store.record_pillars(r, user_id)
    except Exception as e:
        # One bad ticker must never take the service down.
        print(f"  ! {cmd} {tick} failed: {e}", file=sys.stderr)
        reply(chat_id, f"⚠️ Could not build a {what} for {tick}.", msg_id)


def handle_fundamentals(chat_id, msg_id, tick, user_id=None):
    """Briefing in the chat, full report as an attachment and on disk."""
    print(f"  /fundamentals {tick}", flush=True)
    cache.clear_memory()
    try:
        r = fund_mod.analyse(tick)
    except Exception as e:
        print(f"  ! /fundamentals {tick} failed: {e}", file=sys.stderr)
        reply(chat_id, f"\u26a0\ufe0f Could not build a briefing for {tick}.", msg_id)
        return

    reply(chat_id, fund_mod.telegram(r), msg_id)
    if "error" in r:
        return

    # The attachment and the saved copy are extras: a failure in either must
    # not cost the briefing that has already been delivered.
    try:
        body = fund_mod.markdown(r)
        day = datetime.now().strftime("%Y-%m-%d")
        notify._post_document(
            os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), chat_id,
            f"{tick}_Fundamentals_{day}.md", body,
            caption=f"<b>{tick}</b> — full fundamentals report", reply_to=msg_id)
    except Exception as e:
        print(f"  ! attachment failed: {e}", file=sys.stderr)
    try:
        path = fund_mod.write_report(r)
        if path:
            print(f"    saved: {path}")
    except Exception as e:
        print(f"  ! report write failed: {e}", file=sys.stderr)
    try:
        store.record_pillars(r["pillars"], user_id)
        if r.get("value"):
            store.record_price(r["value"], user_id)
    except Exception as e:
        print(f"  ! history write failed: {e}", file=sys.stderr)


def handle_calendar(chat_id, msg_id, arg=""):
    """
    The week's most significant events, with the full calendar attached.

    Takes an optional number of days, so `/calendar 14` looks further out than
    the default week. The digest is capped at TOP_N events whatever the window:
    the point of the command is what matters, not what exists.
    """
    days = 7
    m = re.match(r"^\s*(\d{1,2})\s*$", arg or "")
    if m:
        days = max(1, min(31, int(m.group(1))))
    print(f"  /calendar {days}d", flush=True)
    cache.clear_memory()
    try:
        # Today's gappers, so a name already on the screen that reports this
        # week is ranked as the catalyst it is rather than on its size.
        watch = [g["ticker"] for g in store.todays_gappers()]
        weekly = cal.week(days=days, watchlist=watch)
    except Exception as e:
        print(f"  ! /calendar failed: {e}", file=sys.stderr)
        reply(chat_id, "⚠️ Could not build the calendar.", msg_id)
        return

    reply(chat_id, cal.telegram_week(weekly), msg_id)

    # The attachment is an extra: losing it must not cost the digest that has
    # already been delivered.
    try:
        start = weekly[0][0] if weekly else datetime.now().date()
        notify._post_document(
            os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), chat_id,
            f"Economic_Calendar_{start:%Y-%m-%d}.md", cal.markdown_week(weekly),
            caption="📅 Full calendar — every qualifying event",
            reply_to=msg_id)
    except Exception as e:
        print(f"  ! calendar attachment failed: {e}", file=sys.stderr)


def handle_news(chat_id, msg_id, arg=""):
    """
    Market headlines, or one company's.

    `/news` and `/news 10` answer the market; `/news AAPL` answers the company.
    The argument decides which, so there is one command to remember rather than
    two -- a bare number can never be a ticker and a ticker can never be a
    number, so the two cannot collide.
    """
    words = (arg or "").split()[:2]
    count, sym = news.DEFAULT_N, None
    for w in words:
        if w.isdigit():
            count = max(1, min(news.MAX_N, int(w)))
        elif sym is None:
            t = w.lstrip("$").upper()
            if not TICKER_RE.match(t):
                reply(chat_id, "Usage: <code>/news</code>, <code>/news 10</code> "
                               "or <code>/news AAPL</code>", msg_id)
                return
            sym = t

    print(f"  /news {sym or count}", flush=True)
    cache.clear_memory()
    try:
        if sym:
            q, rows = news.ticker(sym, count)
            reply(chat_id, news.telegram_ticker(sym, q, rows), msg_id)
        else:
            reply(chat_id, news.telegram_headlines(news.headlines(count)), msg_id)
    except Exception as e:
        print(f"  ! /news failed: {e}", file=sys.stderr)
        reply(chat_id, "⚠️ Could not fetch headlines.", msg_id)


def handle(update):
    msg = update.get("message") or update.get("edited_message")
    if not msg or "text" not in msg:
        return
    chat = msg["chat"]
    chat_id = str(chat["id"])
    allow = allowed_chats()
    if allow and chat_id not in allow:
        return                      # not a chat this bot serves

    text = msg["text"].strip()
    if not text.startswith("/"):
        return
    head, _, arg = text.partition(" ")
    cmd = head.split("@", 1)[0].lower()   # /pillars@BotName -> /pillars

    user = msg.get("from", {})
    if rate_limited(user.get("id")):
        return                      # silently drop; replying would amplify

    # /dcf kept as an alias so anything already typed keeps working.
    if cmd in ("/pillars", "/price", "/dcf"):
        handle_ticker_cmd(cmd, chat_id, msg["message_id"], arg,
                          user.get("id"))
    elif cmd == "/fundamentals":
        t = (arg.split() or [""])[0].lstrip("$").upper()
        if not TICKER_RE.match(t):
            reply(chat_id, "Usage: <code>/fundamentals ADBE</code>",
                  msg["message_id"])
        else:
            handle_fundamentals(chat_id, msg["message_id"], t, user.get("id"))
    elif cmd == "/history":
        t = (arg.split() or [""])[0].lstrip("$").upper()
        if not TICKER_RE.match(t):
            reply(chat_id, "Usage: <code>/history ADBE</code>", msg["message_id"])
        else:
            print(f"  /history {t}", flush=True)
            reply(chat_id, store.telegram_history(t), msg["message_id"])
    elif cmd == "/news":
        handle_news(chat_id, msg["message_id"], arg)
    elif cmd == "/calendar":
        handle_calendar(chat_id, msg["message_id"], arg)
    elif cmd in ("/help", "/start"):
        reply(chat_id, HELP, msg["message_id"])
    # Unknown commands are ignored on purpose.


def load_offset():
    try:
        return int((STATE / "offset").read_text().strip())
    except Exception:
        return None


def save_offset(v):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / "offset").write_text(str(v))
    except Exception as e:
        print(f"! could not save offset: {e}", file=sys.stderr)


def main():
    # Fail fast on missing setup instead of looping. Every research command
    # reads SEC EDGAR, which refuses requests without a contact User-Agent, so
    # without one every command would answer with a misleading "not found".
    if not os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
        print("! TELEGRAM_BOT_TOKEN is not set. Put it in .env or "
              "~/.config/stock-research-bot.env.", file=sys.stderr)
        return 2
    try:
        settings.sec_user_agent()
    except RuntimeError as e:
        print(f"! {e}", file=sys.stderr)
        return 2
    me = api("getMe") or {}
    print(f"=== bot up as @{me.get('username','?')} · "
          f"chats {sorted(allowed_chats()) or 'ALL'} ===", flush=True)

    offset = load_offset()
    while True:
        params = {"timeout": POLL_TIMEOUT,
                  "allowed_updates": json.dumps(["message", "edited_message"])}
        if offset is not None:
            params["offset"] = offset
        updates = api("getUpdates", **params)
        if updates is None:
            time.sleep(5)          # transient API trouble; back off and retry
            continue
        for up in updates:
            offset = up["update_id"] + 1
            try:
                handle(up)
            except Exception as e:
                print(f"! handler error: {e}", file=sys.stderr)
        if updates:
            save_offset(offset)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nstopped")
