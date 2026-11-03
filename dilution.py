#!/usr/bin/env python3
"""
Dilution / borrow risk enrichment.

Answers the question "how much do I trust this spike?" using only public
sources that work unattended before the open -- no broker login.

Sources:
  SEC EDGAR submissions API  -> shelf registrations (S-3/F-3) + actual
                                offerings (424B*). Free, no key, authoritative.
  SEC EDGAR full-text search -> reverse split history.
  Nasdaq Trader daily file   -> short sale restriction (SSR) list.

NOT available unattended: real-time borrow availability / easy-to-borrow.
That is broker-gated.
"""

import json
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

import settings

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "data" / "cache"

# SEC requires a descriptive UA with contact info, and <10 req/sec. The agent
# comes from SEC_USER_AGENT (see settings.sec_user_agent) and is read per
# request, so importing this module never needs it.
SEC_DELAY = 0.15

SHELF_FORMS = ("S-3", "F-3", "S-1", "F-1")          # authorization to sell later
OFFERING_FORMS = ("424B",)                           # actually selling now


def _get(url, headers=None, timeout=25, retries=3):
    # Resolved before the retry loop: a missing SEC_USER_AGENT is a setup
    # error to report at once, not a network blip to retry.
    headers = headers or {"User-Agent": settings.sec_user_agent()}
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="ignore")
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def load_cik_map(max_age_days=7):
    """Ticker -> zero-padded CIK. Cached; the file is ~800KB."""
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / "cik_map.json"
    fresh = p.exists() and (
        datetime.now() - datetime.fromtimestamp(p.stat().st_mtime)
    ).days < max_age_days
    if fresh:
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    try:
        raw = json.loads(_get("https://www.sec.gov/files/company_tickers.json"))
        m = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in raw.values()}
        p.write_text(json.dumps(m))
        return m
    except Exception as e:
        if isinstance(e, RuntimeError):
            print(f"  ! {e}", file=sys.stderr)
        return json.loads(p.read_text()) if p.exists() else {}


def load_ssr_list(day=None):
    """
    Symbols currently under Short Sale Restriction (Reg SHO rule 201).

    SSR triggers on a 10% drop and persists through the NEXT session, so a
    name that collapsed yesterday is still SSR-protected today. Shorts can
    only hit the offer, not the bid -- meaningfully helps a squeeze.
    """
    day = day or datetime.now()
    for d in (day, day - timedelta(days=1), day - timedelta(days=2)):
        url = ("https://www.nasdaqtrader.com/dynamic/symdir/shorthalts/"
               f"shorthalts{d.strftime('%Y%m%d')}.txt")
        try:
            txt = _get(url, headers={"User-Agent": "Mozilla/5.0"})
            syms = set()
            for line in txt.splitlines()[1:]:
                if line.strip():
                    syms.add(line.split(",")[0].strip().upper())
            if syms:
                return syms, d.strftime("%Y-%m-%d")
        except Exception:
            continue
    return set(), None


SHARE_CONCEPTS = [
    ("dei", "EntityCommonStockSharesOutstanding"),
    ("us-gaap", "CommonStockSharesOutstanding"),
    ("us-gaap", "CommonStockSharesIssued"),
]


def share_count_history(cik):
    """Historical shares-outstanding points from SEC XBRL, oldest -> newest."""
    for ns, tag in SHARE_CONCEPTS:
        try:
            url = (f"https://data.sec.gov/api/xbrl/companyconcept/"
                   f"CIK{cik}/{ns}/{tag}.json")
            d = json.loads(_get(url))
            pts = sorted({(x["end"], x["val"]) for x in d["units"]["shares"]})
            if pts:
                return pts
        except Exception:
            continue
        finally:
            time.sleep(SEC_DELAY)
    return []


def detect_reverse_split(cik, current_shares_out, min_ratio=3.0):
    """
    Detect a reverse split by share-count collapse.

    EDGAR full-text search is unusable here -- "reverse stock split" appears as
    boilerplate in nearly every micro-cap risk-factors section, and it missed
    WETO's real 1-for-100 outright. Comparing the current share count against
    SEC's own XBRL history is deterministic instead.

    XBRL lags (it updates at the next 10-Q/10-K), which is exactly what makes
    this work: a split done after the last filing shows up as a large gap
    between the filed count and today's count.
    """
    out = {"rs_detected": None, "rs_ratio": None, "rs_prior_shares": None,
           "rs_asof": None}
    if not current_shares_out or current_shares_out <= 0:
        return out

    pts = share_count_history(cik)
    if not pts:
        return out

    peak_date, peak = max(pts, key=lambda p: p[1])
    ratio = peak / current_shares_out
    out["rs_prior_shares"] = peak
    out["rs_asof"] = peak_date
    out["rs_ratio"] = round(ratio, 1)
    out["rs_detected"] = ratio >= min_ratio
    return out


def sec_profile(ticker, cik_map, lookback_days=365, shares_out=None,
                want_splits=True):
    """Shelf + offering + reverse-split profile for one ticker."""
    out = {
        "cik": None, "shelf_count": None, "shelf_last": None,
        "offering_count": None, "offering_last": None,
        "days_since_offering": None, "rs_detected": None, "rs_ratio": None,
        "rs_prior_shares": None, "rs_asof": None,
    }
    cik = cik_map.get(ticker.upper())
    if not cik:
        return out
    out["cik"] = cik

    try:
        d = json.loads(_get(f"https://data.sec.gov/submissions/CIK{cik}.json"))
    except Exception:
        return out
    time.sleep(SEC_DELAY)

    rec = d.get("filings", {}).get("recent", {})
    forms, dates = rec.get("form", []), rec.get("filingDate", [])
    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    shelf, offer = [], []
    for f, dt in zip(forms, dates):
        if dt < cutoff:
            continue
        if f.startswith(SHELF_FORMS):
            shelf.append(dt)
        elif f.startswith(OFFERING_FORMS):
            offer.append(dt)

    out["shelf_count"] = len(shelf)
    out["shelf_last"] = max(shelf) if shelf else None
    out["offering_count"] = len(offer)
    out["offering_last"] = max(offer) if offer else None
    if out["offering_last"]:
        out["days_since_offering"] = (
            datetime.now() - datetime.strptime(out["offering_last"], "%Y-%m-%d")
        ).days

    if want_splits and shares_out:
        out.update(detect_reverse_split(cik, shares_out))
    return out


def dilution_risk(p):
    """
    Collapse the filing profile into a trust signal.

    The trader question is: can this company dump shares into my spike?
      EXTREME - sold stock within 90 days AND has a live shelf
      HIGH    - sold within 180 days, or a serial issuer (5+ offerings/yr)
      MEDIUM  - shelf on file but not recently fired
      LOW     - no shelf, no offerings on file
    """
    sc = p.get("shelf_count")
    oc = p.get("offering_count") or 0
    dso = p.get("days_since_offering")

    if sc is None:
        return "UNKNOWN", []

    flags = []
    if dso is not None and dso <= 90:
        flags.append(f"sold stock {dso}d ago")
    if oc >= 5:
        flags.append(f"serial issuer ({oc} offerings/12mo)")
    if sc and sc > 0:
        flags.append(f"live shelf ({sc})")
    if p.get("rs_detected"):
        flags.append(f"reverse split ~1-for-{p['rs_ratio']:.0f} "
                     f"(since {p['rs_asof']})")

    if dso is not None and dso <= 90 and sc > 0:
        return "EXTREME", flags
    if (dso is not None and dso <= 180) or oc >= 5:
        return "HIGH", flags
    if sc > 0:
        return "MEDIUM", flags
    return "LOW", flags


if __name__ == "__main__":
    tickers = sys.argv[1:] or ["GPRO", "SSM", "WETO", "FLYE", "LIDR"]
    print("Loading CIK map...")
    cm = load_cik_map()
    ssr, ssr_day = load_ssr_list()
    print(f"CIKs: {len(cm):,} | SSR list: {len(ssr)} symbols ({ssr_day})\n")
    import finviz_shares
    for t in tickers:
        so = finviz_shares.shares_out(t)
        p = sec_profile(t, cm, shares_out=so)
        risk, flags = dilution_risk(p)
        print(f"{t:6s} risk={risk:8s} SSR={'YES' if t.upper() in ssr else 'no ':3s} "
              f"shelf={p['shelf_count']} last={p['shelf_last']} "
              f"offerings={p['offering_count']} last={p['offering_last']} "
              f"rs={p['rs_detected']} ({p['rs_ratio']}x)")
        if flags:
            print(f"       flags: {'; '.join(flags)}")
