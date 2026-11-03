# Stock Research Bot

**A Telegram research bot, a premarket gapper report and a market-news alerts feed — in plain Python.** Ask the bot for a company's fundamentals and it answers from SEC filings in seconds. Every weekday morning it posts what is gapping up before the open, whether those companies can dump shares into the move, and what is on the calendar. Through the day it watches the news and posts what matters.

- **Python standard library only** for everything except the optional PDF. No `pip install` to get started. SQLite keeps the history; systemd timers run the schedule.
- **Free, public data.** SEC EDGAR for filings, plus free web sources for prices and news (see [Data sources](#data-sources)).
- **Honest numbers.** A figure that cannot be computed says "not recorded" instead of being guessed, and every valuation prints the assumptions behind it.

> Not investment advice. Everything here is a screen and a set of assumptions, not a recommendation to buy or sell anything.

## What it does

### 1. The command bot

People in your channel's discussion group type commands; the bot replies in the group.

| Command | Answers with |
|---|---|
| `/pillars TICKER` | The **Eight Pillars of Fundamental Valuation**, scored from the company's 10-K filings |
| `/price TICKER [PEER...]` | A discounted cash flow, target prices for a 10% and 15% yearly return over 5 and 10 years, a reverse DCF (what growth today's price assumes), and a comparables table against any peers named |
| `/fundamentals TICKER` | A short briefing — pillars, valuation, why any pillar failed, a company headline — with the full report attached as a markdown file |
| `/history TICKER` | What the bot has recorded for a ticker over time, as it stood on each date |
| `/news [N\|TICKER]` | The latest market headlines, or one company's news with its own announcements first |
| `/calendar [days]` | The week's most significant scheduled events — rate decisions, inflation and jobs prints, big reporters — with consensus figures, and the full calendar attached |
| `/help` | The command list |

Tickers work with or without `$`, in any case. `/dcf` is an alias of `/price`.

Example — `/pillars AAPL`:

```
📊 Apple Inc. (AAPL)
6 / 8 pillars
$331.58 · $4,837.8B mcap

❌ 5-Year P/E 45.2x (< 22.5)
✅ ROIC 101.2% (>= 9.0%)
✅ Share count 16326M → 14720M (decreasing)
✅ FCF growth $111.44B → $136.06B (growing)
✅ Net income growth $99.80B → $132.18B (growing)
✅ Revenue growth $394.33B → $478.83B (growing)
✅ LT liabilities / avg FCF 1.10 (< 5.0)
❌ Price / FCF 35.6 (< 22.5)

Pillar 1 = market cap ÷ 5-yr average net income. Data: SEC EDGAR 10-K filings.
A screen, not an analysis. Not investment advice.
```

**The eight pillars:** 5-year P/E (market cap over five-year average net income) under 22.5 · ROIC at least 9% · share count falling over 5 years · free cash flow growing · net income growing · revenue growing · long-term liabilities under 5× average FCF · price to FCF under 22.5. Banks and insurers report no capex, so the pillar and valuation commands say they do not apply rather than print nonsense.

**Built to sit in a public group.** Anyone in the group can type, so:

- **Ticker validation.** Every ticker is checked against a strict pattern (`^[A-Za-z][A-Za-z.\-]{0,6}$`) before any lookup. Nothing a user types reaches a shell or a file path, and nothing reaches a URL without passing that check.
- **Per-user rate limit.** Five commands per user per minute; extra commands are dropped silently, so a flood cannot amplify itself into replies.
- **Chat allowlist.** The bot answers only in `TELEGRAM_GROUP_ID` and in direct messages from `TELEGRAM_OWNER_ID`. Set at least one: with neither set the bot refuses to start (pass `--allow-all-chats` to override).
- **Unknown commands are ignored.** The bot never chats, so it cannot be drawn into a conversation.
- **One bad lookup never takes it down.** Each command is wrapped; a failure costs that reply ("could not build a valuation for X") and nothing else.
- **Results go back where the command came from**, never to the broadcast channel.

### 2. The premarket report

A weekday-morning report, written as markdown to `SRB_OUTPUT_DIR/daily/` and posted to the channel as a phone-sized digest (long digests are split into several posts rather than trimmed). Sections:

| Section | What is in it |
|---|---|
| **Theme Watch** | What recent runners have been running on — established, warming or cooling themes. Needs a theme archive (see below); otherwise it says "not available" |
| **After-Hours Watch** | Last night's after-hours runner and whether the 4–5am ET hour cleared the price it has to hold |
| **Market** | Trend (the 20% rule against the 52-week high), CNN Fear & Greed with its components, the AAII survey, VIX, oil, gold, bitcoin, Treasury yields |
| **Today's calendar** | Economic releases with consensus and prior, then the day's significant earnings reporters |
| **Premarket gappers** | Up 10%+ on 50k+ premarket volume, under $2B: gap, price, float, **float rotation** (premarket volume ÷ float), short interest, relative volume, dilution risk, shelf registrations, last offering, SSR status, reverse splits, country |
| **Auto-flags** | Sub-2M floats, short interest over 15%, floats rotated more than once, country clusters, no press release since the close |
| **Press releases** | Wire-service releases since the last close for the top gappers |
| **Trust check** | Who can sell into the spike: recent offerings plus live shelves (EXTREME/HIGH), clean filers, names under SSR |

Every gapper is also appended to `data/movers.csv`, and `--telegram-only` re-posts a morning's digest from that log without scraping again.

**The after-hours watch** (`ah_watch.py`, run each evening after 20:00 ET) ranks the after-hours gainers, skips thin, fading and deal-pinned moves, saves the pick for the morning report, and posts an evening digest. Its rule comes from a study of 330 after-hours runs of +10% or more (Aug–Oct 2026): a stock that traded above its 8pm close between 4 and 5am ET kept at least half its move at 7am ET 62–80% of the time; one that never got above it, 25–36%. Treat it as beta.

**The theme archive is optional.** Nothing in this repository writes it: it is meant for a SQLite file your own screener fills during the day. Point `SRB_THEME_DB` at it; the expected `thememembers` table is documented at the top of `themes.py`.

### 3. The alerts feed

`market_alerts_collect.py` polls RSS and public APIs every 15 minutes and keeps only what is new: market news desks, Google News searches, Google Trends, and — once you list them — YouTube channels, newsletters, Truth Social accounts, subreddits, SEC filings for a watchlist and 13F/13D filings by the funds you follow. Each item is labelled **📊 Data** (filings, government data, company releases) or **💬 Opinion** (everything else).

Two layers: **core** is the broad market every day; **focus** is one topic you are leaning into, with its own feeds, keywords, EDGAR watchlist and a *lean* — your thesis in your own words, which steers what gets posted.

| When | What | Needs Claude Code? |
|---|---|---|
| Every 15 min | **Midday alerts** — cheap keyword rules pick candidates, one short `claude -p` call picks at most 3 worth posting and writes a sentence or two each, posted as one message. At least 30 min between posts, nothing in quiet hours (21:00–05:45 local) | Yes, for judged alerts. Videos from `AUTO_POST_CHANNELS` post without it |
| Every evening | **Evening digest** — everything collected that day, summarised against your lean, written as a markdown note and a Telegram post | Yes; `--bundle-only` gives you the day's items as markdown without it |
| Fridays | **Weekly update** — the week's moves computed exactly from saved closes, the week's digests summarised, an optional thesis ledger and prediction scoreboard kept up to date | Yes; `--bundle-only` gives you the moves and file list without it |

Without [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) the collector still runs, auto-post channels still post, and the bundles are still written; rule-picked candidates are simply never posted (they age out and land in the evening bundle).

**The Claude runs are fenced.** Their input is strangers' text, which is a prompt-injection surface. They run with `--permission-mode dontAsk`, no shell, no web, and write access only to `SRB_OUTPUT_DIR/market-alerts/` (digests or weekly), the run's own output folder, and `FOCUS_AREA` if you set one. The prompts tell the model the material is data, never instructions, and to list anything that looks like an instruction under *Oddities*. The model reports the weekly numbers; it never computes them.

## Quick start

You need Python 3.9+, a Telegram account and somewhere to run systemd timers (any Linux box or VM).

**1. Make a bot, a channel and a discussion group.**
- Message [@BotFather](https://t.me/BotFather), send `/newbot`, keep the token.
- Create a channel, then in its settings → *Discussion* create or link a group. The channel is broadcast-only; commands are typed in the group.
- Add the bot to the channel as an administrator (permission to post) and to the group as a member. If it does not see plain `/pillars` commands in the group, turn privacy mode off in BotFather (`/setprivacy` → *Disable*); it ignores everything that is not a known command either way.
- Get the IDs: post in the group, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and read `chat.id` (private channels and groups start with `-100`).

**2. Get the code and set credentials.**
```
git clone https://github.com/wolfe86/stock-research-bot.git ~/stock-research-bot
cd ~/stock-research-bot
cp .env.example .env && chmod 600 .env
nano .env        # token, channel ID, group ID, SEC_USER_AGENT
```
SEC requires every automated request to name you and give a contact email, e.g. `SEC_USER_AGENT="Jane Doe jane@example.com"` ([SEC guidance](https://www.sec.gov/os/accessing-edgar-data)). Lookups that touch EDGAR stop with a clear error until it is set.

**3. Make it yours (optional for the bot, worth it for the alerts).**
```
cp local_settings.example.py local_settings.py
nano local_settings.py   # time zone, sources, focus, lean
```

**4. Check it from the command line** — nothing here posts:
```
python3 pillars.py AAPL --telegram
python3 dcf.py AAPL --telegram
python3 premarket_report.py --dry-run          # prints the report and the digest
python3 market_alerts_collect.py && python3 market_alerts_collect.py --status
python3 notify.py --text "hello" --dry-run
```
Then a real post: `python3 notify.py --text "<b>hello</b>"`.

**5. Schedule it.**
```
cp examples/systemd/* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now stock-research-bot.service
systemctl --user enable --now premarket-report.timer ah-watch.timer market-alerts-collect.timer
# optional, need Claude Code:
systemctl --user enable --now market-alerts-digest.timer market-alerts-weekly.timer
```
Timer times are in the machine's local time — read the comment at the top of each `.timer` and **set the time before the first enable**: editing `OnCalendar` later on a `Persistent=true` timer can fire a "missed" run as soon as you `daemon-reload`. On a headless server, run `loginctl enable-linger $USER` once so user units run without a login.

## Configuration

**Credentials and paths** — environment variables, read from the process environment, then `.env` next to the scripts, then `~/.config/stock-research-bot.env` (the environment wins):

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From @BotFather. Without it nothing is ever sent — reports are still written |
| `TELEGRAM_CHAT_ID` | The broadcast channel for reports and alerts |
| `TELEGRAM_GROUP_ID` | The discussion group the bot answers commands in |
| `TELEGRAM_OWNER_ID` | Optional: a user who may DM the bot |
| `SEC_USER_AGENT` | Required for SEC EDGAR: `"Name email@example.com"` |
| `SRB_OUTPUT_DIR` | Where reports and notes go (default `data/reports`). Point it at a notes folder (Obsidian, say) to read them there |
| `SRB_THEME_DB` | Optional theme archive for the Theme Watch (default `data/themes.sqlite`) |
| `CLAUDE_BIN` | Claude Code CLI for the alert judge, digest and weekly (default `claude` on `PATH`) |
| `YT_TRANSCRIPT_BIN` | Optional: a command taking `-p -o OUT.txt -- VIDEO_ID` that writes a YouTube transcript (a small yt-dlp wrapper). Without it videos are summarised from their title and description |

**Settings** — defaults live in `settings.py` and `market_alerts_sources.py`; anything you set in the git-ignored `local_settings.py` overrides them. Lists are replaced whole: copy the default, then edit it.

| Setting | What it does |
|---|---|
| `TIMEZONE` · `TIMEZONE_LABEL` | Local time zone for file dates, quiet hours and printed times (default `America/New_York`, `ET`) |
| `CALENDAR_WATCHLIST` | Tickers the earnings calendar always lists, on top of large caps, the morning's gappers and the focus watchlist |
| `CHANNEL_NAME` · `READER` | The feed's name in posts, and who it is for (steers the alert judge) |
| `CORE_YOUTUBE` · `CORE_NEWSLETTERS` · `TRUTH` · `HOLDERS` · `CORE_REDDIT` | Core sources — all empty by default |
| `CORE_NEWS` · `CORE_GNEWS` · `NOISE` | Market news desks, Google News queries, and headline words to drop |
| `FOCUS_NAME` · `FOCUS_LEAN` | The focus topic and your lean on it |
| `FOCUS_YOUTUBE` · `FOCUS_NEWS` · `FOCUS_GOV` · `FOCUS_GNEWS` · `FOCUS_REDDIT` · `FOCUS_KEYWORDS` · `FOCUS_EDGAR_TICKERS` | The focus layer's sources, keywords and filings watchlist |
| `FOCUS_READ_FIRST` · `FOCUS_AREA` | Optional notes the Claude runs read first, and a folder of focus notes they may update |
| `ALERT_KEYWORDS` · `ALERT_NOISE` · `ALERT_ALWAYS_KINDS` · `ALERT_ALWAYS_SOURCES` | What makes an item an alert candidate |
| `AUTO_POST_CHANNELS` · `AUTO_POST_HEADING` · `AUTO_POST_WATCHLIST_HEADING` | YouTube channels posted without judging |
| `ALERT_MAX_PER_RUN` · `ALERT_MIN_GAP_MIN` · `ALERT_QUIET` | How much, how often, and when not |
| `DIGEST_MODEL` · `WEEKLY_MODEL` · `JUDGE_MODEL` | Models passed to `claude -p` (default sonnet, sonnet, haiku) |

`local_digest_prompt.md` and `local_weekly_prompt.md`, if present, replace the shipped prompts (same `{placeholders}`).

Reddit and X: anonymous Reddit RSS is supported but off by default (Reddit rate-limits anonymous reads hard). There is no X/Twitter collector.

## Files

| File | Role |
|---|---|
| `bot.py` | The command bot (long-polls Telegram) |
| `pillars.py` · `dcf.py` · `fundamentals.py` · `history.py` · `quote.py` | The research commands |
| `news.py` · `calendar_events.py` · `market.py` | Headlines, the economic/earnings calendar, market context |
| `dilution.py` · `finviz_shares.py` | Shelf, offering, reverse-split and SSR checks |
| `premarket_report.py` · `ah_watch.py` · `themes.py` · `report_pdf.py` | The premarket report, the after-hours watch, Theme Watch, the PDF |
| `market_alerts_*.py` · `market_alerts_*prompt.md` | The alerts feed |
| `notify.py` · `store.py` · `cache.py` · `settings.py` | Telegram delivery, research history (`data/research.db`), HTTP cache, settings |

## Requirements

- Python 3.9+ (uses `zoneinfo`). Everything runs on the standard library.
- **Optional:** `markdown` and `weasyprint` for the PDF copies of the premarket report and evening digest (`pip install markdown weasyprint`, or your distribution's `python-markdown` / `python-weasyprint`). Without them the report is still written and the evening digest is posted as text.
- **Optional:** [Claude Code](https://docs.claude.com/en/docs/claude-code/overview), installed and signed in, for judged alerts, the evening digest and the weekly update.

## Data sources

- **SEC EDGAR** (company facts, submissions, full-text search) — public US government data; SEC asks for a descriptive User-Agent and fewer than 10 requests a second, which the code keeps to.
- **Nasdaq** (economic and earnings calendar, Nasdaq Trader SSR list), **Yahoo Finance** and **CNBC** (quotes and bars), **StockAnalysis** (premarket/after-hours movers, historical ratios), **Finviz** (float, short interest, news), **CNN Fear & Greed**, **AAII**, and public **RSS** feeds (news desks, Google News, Google Trends, YouTube, newsletters, Reddit) and the Truth Social public API.

These are free sources read the way a browser reads them. **Their terms of use apply to whoever runs this code**, and any of them can change or block access without notice — every fetch fails soft, so a dead source costs a section, not the report. Check each site's terms before you run this, and especially before you republish what it collects.

## License

MIT — see [LICENSE](LICENSE). Not investment advice.
