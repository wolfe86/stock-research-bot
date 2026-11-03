"""Minimal Finviz shares-outstanding lookup (used by dilution.py's self-test)."""
import re, urllib.request
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

def shares_out(t):
    try:
        req = urllib.request.Request(f"https://finviz.com/quote.ashx?t={t}", headers=UA)
        h = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
    except Exception:
        return None
    cells = [re.sub(r"<[^>]+>", "", c).strip()
             for c in re.findall(r"<td[^>]*>(.*?)</td>", h, re.S)]
    for i, c in enumerate(cells):
        if c == "Shs Outstand" and i + 1 < len(cells):
            v = cells[i + 1].replace(",", "")
            if v in ("-", ""):
                return None
            m = {"K": 1e3, "M": 1e6, "B": 1e9}.get(v[-1:], 1)
            try:
                return float(v[:-1] if m > 1 else v) * m
            except ValueError:
                return None
    return None
