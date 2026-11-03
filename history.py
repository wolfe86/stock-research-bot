#!/usr/bin/env python3
"""
Historical fiscal-year ratios from stockanalysis.com.

EDGAR carries the financials but no prices, so anything built on a multiple --
a five-year average P/E, price to free cash flow -- cannot come from filings
alone. This reads the per-fiscal-year series the ratios page already computes.

The values match a hand-computed check: ADBE's FY2021-2025 P/Es come back
60.8 / 33.4 / 51.4 / 40.8 / 18.8, averaging 41.1x against ~42x worked out by
hand from the same fiscal-year-end prices.

Parsed out of the page's embedded JS payload rather than its HTML table: the
payload is stable, already numeric, and needs no column alignment.

Stdlib only.

Usage:
    python3 history.py ADBE
"""

import json
import re
import sys
import urllib.error
import urllib.request

import cache

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

URL = "https://stockanalysis.com/stocks/{sym}/financials/ratios/"

# Series worth having. The first column of each is TTM, not a fiscal year.
FIELDS = ("fiscalYear", "marketcap", "pe", "ps", "pfcf", "roic")


def _series(blob, key):
    """One `key:[...]` array from the payload, as floats (None where blank)."""
    m = re.search(rf'\b{key}:\[([^\]]*)\]', blob)
    if not m:
        return []
    out = []
    for part in m.group(1).split(","):
        part = part.strip().strip('"')
        if not part or part.lower() in ("null", "undefined", "-"):
            out.append(None)
            continue
        try:
            out.append(float(part))
        except ValueError:
            out.append(None)
    return out


def get(ticker):
    """
    {"years": [...], "pe": [...], ...} newest fiscal year first, TTM removed.

    Returns None on any failure -- the caller degrades to filings-only rather
    than guessing a multiple.
    """
    sym = ticker.lower().strip().lstrip("$").replace(".", "-")
    try:
        html = cache.fetch(URL.format(sym=sym), cache.TTL_RATIOS,
                           headers={"User-Agent": UA})
    except Exception as e:
        print(f"  ! ratio history failed for {sym}: {e}", file=sys.stderr)
        return None

    i = html.find("{datekey:[")
    if i < 0:
        return None
    blob = html[i:i + 4000]

    years = _series(blob, "fiscalYear")
    if not years:
        return None

    # The leading column is TTM (its datekey is the literal "TTM"), so drop it
    # wherever it is present; a partial current year is not a fiscal year.
    dk = re.search(r'datekey:\[([^\]]*)\]', blob)
    drop = 1 if (dk and dk.group(1).split(",")[0].strip().strip('"') == "TTM") else 0

    out = {"years": [int(y) for y in years[drop:] if y is not None]}
    for f in FIELDS:
        if f == "fiscalYear":
            continue
        vals = _series(blob, f)
        out[f] = vals[drop:] if vals else []
    return out


def avg(vals, n=5):
    """Mean of the first n non-null values, or None."""
    good = [v for v in (vals or [])[:n] if v is not None]
    return sum(good) / len(good) if good else None


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    h = get(sys.argv[1])
    if not h:
        print("no history")
        sys.exit(1)
    print(json.dumps(h, indent=2))
    print(f"\n5-year average P/E: {avg(h.get('pe')):.2f}")
