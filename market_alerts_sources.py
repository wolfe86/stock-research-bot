#!/usr/bin/env python3
"""
What the market alerts feed watches -- the always-on core layer, plus a focus.

Two layers:

  core   -- the broad market, every day, whatever the topic. A market-moving
            story reaches the channel even when it has nothing to do with the
            focus.
  focus  -- one topic you are leaning into (FOCUS_* below). Its feeds, search
            queries, keywords and EDGAR watchlist are polled on top of the
            core, and FOCUS_LEAN tells the Claude runs what to hunt for.
            Changing topic means changing the FOCUS_* settings -- nothing else.

Read by market_alerts_collect.py, market_alerts_alert.py, the digest and the
weekly update, and by calendar_events.py (the focus EDGAR watchlist).

Labels: every item posted is marked Data or Opinion, and both are delivered.
News reporting is opinion too; only primary records -- filings, government
data, a company's own release -- default to Data.

Anything UPPER_CASE here can be overridden in a git-ignored local_settings.py
(see local_settings.example.py). Lists are replaced whole: copy a default list
and edit it rather than listing only the additions.
"""

from types import SimpleNamespace

DATA = "data"
OPINION = "opinion"

YOUTUBE_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
REDDIT_FEED = "https://www.reddit.com/r/{}/top/.rss?t=day"
GNEWS_FEED = "https://news.google.com/rss/search?q={}&hl=en-US&gl=US&ceid=US:en"

# Only items published within this window are kept, so a first run or a new
# feed does not backfill a month of stories into one digest.
MAX_AGE_HOURS = 36

# Shown in the Telegram posts and the Claude prompts.
CHANNEL_NAME = "Market Alerts"
# Who the alerts are for, in one or two sentences, for the alert judge.
READER = "The reader trades US stocks intraday and invests long-term."

# --- Core: YouTube -----------------------------------------------------------
# (display name, channel ID). The ID is the UC... string in the channel page's
# source ("externalId"); check https://www.youtube.com/feeds/videos.xml?channel_id=ID
# answers before adding one. Empty by default -- see local_settings.example.py.
CORE_YOUTUBE = []

# --- Core: Reddit (off by default) -------------------------------------------
# Anonymous RSS reads get HTTP 429 after one or two quick requests, so each
# run reads only REDDIT_PER_RUN subreddits, rotating through the list. Reddit
# posts never alert; they reach the evening digest only. Empty = disabled.
CORE_REDDIT = []
REDDIT_PER_RUN = 2
REDDIT_GAP = 60      # seconds between the two; 40 s spacing answered 3 of 4

# --- Core: market news -------------------------------------------------------
# The same desks the /news command uses (news.py), chosen for freshness.
# filter=False: take everything. filter=True: drop only the NOISE list
# (politics, sport, lifestyle).
CORE_NEWS = [
    ("Seeking Alpha", "https://seekingalpha.com/market_currents.xml", False),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories", False),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_bulletins", False),
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html", True),
    ("CNBC", "https://www.cnbc.com/id/10000664/device/rss/rss.html", True),
]

# Newsletters and podcasts -- long-form opinion, (name, RSS URL).
CORE_NEWSLETTERS = []

# --- Core: Google News -------------------------------------------------------
CORE_GNEWS = [
    '"stock market" (selloff OR rally OR plunge OR surge)',
    '"Federal Reserve" (rates OR Powell OR FOMC)',
    '(CPI OR "jobs report" OR payrolls OR PCE) market',
    '"small caps" OR "Russell 2000"',
    '(margin debt OR "market breadth" OR "market concentration")',
]

NOISE = [
    r"\b(election|midterm|campaign trail|senator|congressman|congresswoman",
    r"impeach|indict|pardon|dinner|wedding|royal|celebrity|recipe",
    r"vacation|travel guide|holiday gift|horoscope|obituary",
    r"super bowl|nfl|nba|world cup|olympic|movie review|box office)\b",
]

# --- Truth Social ------------------------------------------------------------
# Public accounts read through the Mastodon-style API, which answers without a
# key: (display name, numeric account ID). Media-only posts carry no text and
# are skipped. Empty by default.
TRUTH_API = ("https://truthsocial.com/api/v1/accounts/{id}/statuses"
             "?limit=20&exclude_replies=true")
TRUTH = []

# --- Google Trends -----------------------------------------------------------
# Today's top US trending searches (official RSS). Mostly sport and
# celebrities, so only market terms are kept -- a ticker or "stock market
# crash" breaking into the national top list is a retail-attention signal.
TRENDS_RSS = "https://trends.google.com/trending/rss?geo=US"
TRENDS_KEEP = [
    r"stock|market|\bdow\b|nasdaq|s&p|crash|recession|inflation|\bfed\b|\brates?\b",
    r"bitcoin|crypto|\bgold\b|\boil\b|tariff|layoff|bankrupt|\bipo\b|earnings",
    r"nvidia|tesla|apple|microsoft|amazon|meta|google|oracle|openai|anthropic",
    r"micron|amd|broadcom|coreweave|palantir|\bai\b",
]

# --- Big-investor filings ----------------------------------------------------
# Quarterly holdings (13F) and activist stakes (13D/13G) from the funds you
# follow: (display name, 10-digit CIK from EDGAR). Empty by default.
HOLDERS = []
HOLDER_FORMS = ("13F-HR", "SC 13D", "SC 13G", "SCHEDULE 13D", "SCHEDULE 13G")

# --- SEC EDGAR ---------------------------------------------------------------
# Filings for FOCUS_EDGAR_TICKERS. 6-K / 20-F: foreign filers report on these.
EDGAR_FORMS = ("8-K", "10-Q", "10-K", "6-K", "20-F", "S-1", "S-3", "424B")
EIGHT_K_ITEMS = {
    "1.01": "material agreement",
    "1.02": "agreement terminated",
    "2.02": "results",
    "2.03": "new debt",
    "2.05": "restructuring costs",
    "2.06": "impairment",
    "5.02": "officer change",
    "7.01": "Reg FD disclosure",
    "8.01": "other event",
}

# --- Focus -------------------------------------------------------------------
# The topic the feed leans into. With no focus set, the focus layer is empty
# and everything is core.
FOCUS_NAME = "no focus set"
# Your lean, in your words: the thesis, what would confirm it and what would
# break it. Given to the alert judge and the digest/weekly runs as steering.
FOCUS_LEAN = "No focus is set. Report the broad market only."
FOCUS_YOUTUBE = []       # (name, channel ID)
FOCUS_REDDIT = []        # subreddit names, rotated with CORE_REDDIT
FOCUS_NEWS = []          # (name, RSS URL, filter) -- filter=True keeps keyword hits only
FOCUS_GOV = []           # same shape; labelled Data
FOCUS_GNEWS = []         # Google News queries
FOCUS_KEYWORDS = []      # regexes; a core item matching one counts as focus
FOCUS_EDGAR_TICKERS = []  # filings watchlist; also listed by the earnings calendar
# Optional files the digest and weekly runs read before the bundle (absolute
# paths), and an optional folder of focus notes those runs may update. Both
# are off by default; the runs can write only to the folder named here.
FOCUS_READ_FIRST = []
FOCUS_AREA = None

# --- Midday alerts -----------------------------------------------------------
# Rules pick the candidates (cheap, no Claude); one short `claude -p` decides
# which are worth a post and writes it. Everything else waits for the evening
# digest.
ALERT_ALWAYS_KINDS = {"truth", "youtube"}       # always judged
ALERT_ALWAYS_SOURCES = set()                     # source names always judged
ALERT_EDGAR_ITEMS = {"1.01", "1.02", "2.02", "2.05", "2.06"}
ALERT_KEYWORDS = [
    # market-moving
    r"\bFed\b", r"\bFOMC\b", r"Powell|Warsh", r"rate (hike|cut)", r"\bhikes?\b",
    r"tariff", r"sanction", r"\bCPI\b|inflation", r"jobs report|payrolls",
    r"plunge|plummet|tumble|crash|sell-?off|rout", r"surge|soar|record high",
    r"halt(ed)?\b", r"downgrade", r"default|bankrupt|chapter 11",
    r"Treasury yield|10-year", r"Hormuz|Iran|oil price",
]
# Never alert on these headline shapes -- evergreen opinion and stock-picking
# listicles, not news.
ALERT_NOISE = [
    r"^prediction:", r"should you (buy|sell)", r"\bbuy (now|and hold)\b",
    r"stocks? to (buy|own|watch)", r"is it time to", r"(top|best) \d+ ",
    r"\d+ (stocks?|reasons)", r"motley fool", r"forever", r"millionaire",
    r"warren buffett'?s (top|best) advice", r"here'?s (why|how|what)",
    r"could (soar|skyrocket|double)", r"if you (had )?invested",
]
# YouTube channels (display names from CORE_YOUTUBE / FOCUS_YOUTUBE) whose new
# full-length uploads are posted straight away: no judging, no posting gap,
# quiet hours still apply. Shorts are skipped. The headings take {name}; a
# title containing "watch list" uses the second one.
AUTO_POST_CHANNELS = set()
AUTO_POST_HEADING = "▶️ <b>New from {name}</b>"
AUTO_POST_WATCHLIST_HEADING = "📋 <b>{name}: watch list</b>"

# No daily cap: each post bundles at most ALERT_MAX_PER_RUN items into one
# message, and posts are at least ALERT_MIN_GAP_MIN apart. Items not yet
# posted wait for the next one.
ALERT_MAX_PER_RUN = 3
ALERT_MIN_GAP_MIN = 30
# No alerts in this window (local time, settings.TIMEZONE). What arrives then
# goes into the evening digest.
ALERT_QUIET = ("21:00", "05:45")


# ---------------------------------------------------------------------------
# LOCAL OVERRIDES - local_settings.py (git-ignored) replaces any setting above.
# Then the focus is assembled from whatever the FOCUS_* settings ended up as.
# ---------------------------------------------------------------------------

try:
    import local_settings as _local
except ImportError:
    _local = None
if _local is not None:
    globals().update({k: v for k, v in vars(_local).items() if k.isupper()})

FOCUS = SimpleNamespace(
    NAME=FOCUS_NAME, LEAN=FOCUS_LEAN, YOUTUBE=FOCUS_YOUTUBE,
    REDDIT=FOCUS_REDDIT, NEWS=FOCUS_NEWS, GOV=FOCUS_GOV, GNEWS=FOCUS_GNEWS,
    KEYWORDS=FOCUS_KEYWORDS, EDGAR_TICKERS=FOCUS_EDGAR_TICKERS,
    READ_FIRST=FOCUS_READ_FIRST, AREA=FOCUS_AREA)
