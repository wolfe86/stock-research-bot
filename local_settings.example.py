"""Copy this file to local_settings.py and edit it. local_settings.py is
git-ignored, so your sources, watchlist and lean stay out of any commit.

Anything UPPER_CASE you set here replaces the same setting in settings.py or
market_alerts_sources.py. Lists are replaced whole, so copy the default list
and edit it rather than listing only the additions.

Credentials do NOT go here -- they go in .env (see .env.example).
"""

# --- General (settings.py) ---------------------------------------------------

TIMEZONE = "America/New_York"
TIMEZONE_LABEL = "ET"

# Tickers the earnings calendar should always list when they report.
CALENDAR_WATCHLIST = ["AAPL", "MSFT"]

# --- Alerts: core sources (market_alerts_sources.py) -------------------------

CHANNEL_NAME = "Market Alerts"
READER = "The reader trades US small caps intraday and invests long-term."

# (display name, YouTube channel ID)
CORE_YOUTUBE = [
    ("Patrick Boyle", "UCASM0cgfkJxQ1ICmRilfHLw"),
    ("Forward Guidance", "UCkrwgzhIBKccuDsi_SvZtnQ"),
]

# Anonymous Reddit RSS, rotated a couple per run. Leave empty to keep it off.
CORE_REDDIT = ["stocks", "investing"]

# (name, RSS URL)
CORE_NEWSLETTERS = [
    ("Apricitas Economics", "https://www.apricitas.io/feed"),
]

# (display name, Truth Social numeric account ID)
TRUTH = [("Donald J. Trump", "107780257626128497")]

# (display name, 10-digit CIK) -- 13F holdings and 13D/13G stakes.
HOLDERS = [
    ("Pershing Square (Ackman)", "0001336528"),
    ("Bridgewater (Dalio)", "0001350694"),
]

# --- Alerts: the focus -------------------------------------------------------

FOCUS_NAME = "AI infrastructure"
FOCUS_LEAN = """\
The AI data-center buildout is running ahead of the revenue that pays for it.
Hunt for: capex guidance changes, financing strain, power and grid limits,
project delays or cancellations.
Would break the lean: AI revenue growth keeping pace with capex, new
financing closing easily, hyperscalers raising guidance on demand.
"""
FOCUS_NEWS = [
    ("Data Center Dynamics", "https://www.datacenterdynamics.com/en/rss/", False),
    ("Tom's Hardware", "https://www.tomshardware.com/feeds/all", True),
]
FOCUS_GNEWS = ['hyperscaler capex', '"AI bubble"']
FOCUS_KEYWORDS = [r"data ?cent(er|re)s?", r"hyperscal", r"\bGPUs?\b", r"\bHBM",
                  r"nvidia", r"capex"]
FOCUS_EDGAR_TICKERS = ["NVDA", "AMD", "AVGO", "TSM", "MU"]

# Optional: notes the digest/weekly runs read first, and a folder of focus
# notes they may update (append-only tables, a thesis ledger, a prediction
# scoreboard). Absolute paths.
# FOCUS_READ_FIRST = ["/home/you/notes/ai-thesis.md"]
# FOCUS_AREA = "/home/you/notes/ai-focus"

# --- Alerts: posting ---------------------------------------------------------

# Extra alert keywords on top of the market-moving defaults: copy the default
# ALERT_KEYWORDS list from market_alerts_sources.py and append to it.

# New full-length uploads from these channels post immediately.
# AUTO_POST_CHANNELS = {"Patrick Boyle"}

# No alerts between these local times.
ALERT_QUIET = ("21:00", "05:45")
