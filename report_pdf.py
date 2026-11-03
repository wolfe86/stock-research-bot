#!/usr/bin/env python3
"""
Render the morning's premarket report to PDF.

Optional. Runs after premarket_report.py as an ExecStartPost of
premarket-report.service (the leading "-" there means a failure here never
fails the report itself). Unlike the rest of the project this is not
stdlib-only: it needs `markdown` and `weasyprint` (pip install markdown
weasyprint, or your distribution's python-markdown / python-weasyprint).
ah_watch.py uses it for the evening digest PDF and falls back to a plain
text post when it is missing.

The markdown report stays the record; this is a print layout of it. The
masthead tiles are read back out of the report's own text, so a section the
scanner skipped simply drops its tile. PDFs go to SRB_OUTPUT_DIR/pdf/.

Usage:
    python3 report_pdf.py                # today's report
    python3 report_pdf.py --open         # ...then open it (xdg-open)
    python3 report_pdf.py PATH.md        # a specific report
"""

import html
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import markdown
from weasyprint import HTML

import settings

DAILY = settings.OUTPUT_DIR / "daily"
ATTACH = settings.OUTPUT_DIR / "attachments"
OUT_DIR = settings.OUTPUT_DIR / "pdf"

# Table columns whose +/- values are moves, coloured green/red. Everything else
# (an economic "Expected -0.2%", say) is a level and stays ink-coloured.
MOVE_COLUMNS = {"Change", "Gap %", "Open→HOD", "Open→Close"}

CSS = """
@page {
  size: Letter; margin: 16mm 14mm 18mm;
  @bottom-left { content: string(kind) " · " string(rtitle); font: 7.5pt "Noto Sans"; color: #7a8394; }
  @bottom-right { content: "Page " counter(page) " of " counter(pages);
                  font: 7.5pt "Noto Sans"; color: #7a8394; }
}
@page :first { margin-top: 10mm; }

:root { --ink: #1b2230; --muted: #667085; --navy: #0f1b2d; --gold: #c9a227;
        --rule: #e3e7ee; --zebra: #f7f8fa; --up: #087443; --down: #b42318; }

body { font-family: "Noto Sans", sans-serif; font-size: 9.5pt; color: var(--ink);
       line-height: 1.45; font-variant-numeric: tabular-nums; }

/* --- masthead --- */
.mast { background: var(--navy); color: #fff; border-radius: 6px;
        padding: 14pt 16pt 12pt; margin-bottom: 10pt; border-bottom: 3px solid var(--gold); }
.eyebrow { font-size: 7.5pt; letter-spacing: 2.5pt; text-transform: uppercase;
           color: var(--gold); font-weight: 700; string-set: kind content(); }
.mast h1 { font-family: "Noto Serif Display", "Noto Serif", serif; font-weight: 600;
           font-size: 22pt; margin: 2pt 0 2pt; color: #fff; border: 0;
           string-set: rtitle content(); }
.mast .sub { font-size: 8pt; color: #b8c2d3; }

.tiles { display: flex; gap: 6pt; margin: 0 0 12pt; }
.tile { flex: 1; border: 1px solid var(--rule); border-radius: 5px; padding: 6pt 7pt;
        border-top: 3px solid var(--navy); }
.tile .k { font-size: 6.5pt; letter-spacing: 1pt; text-transform: uppercase;
           color: var(--muted); font-weight: 700; }
.tile .v { font-size: 13pt; font-weight: 700; margin-top: 1pt; line-height: 1.15; }
.tile .s { font-size: 7pt; color: var(--muted); margin-top: 1pt; }
.tile.up { border-top-color: var(--up); } .tile.up .v { color: var(--up); }
.tile.down { border-top-color: var(--down); } .tile.down .v { color: var(--down); }

/* --- headings --- */
h2 { font-size: 12pt; color: var(--navy); margin: 16pt 0 6pt; padding-bottom: 3pt;
     border-bottom: 1.5px solid var(--navy); break-after: avoid; }
h3 { font-size: 10.5pt; color: var(--navy); margin: 12pt 0 4pt; break-after: avoid; }
p { margin: 4pt 0 6pt; }
p > strong:first-child { color: var(--navy); }
p.lead { break-after: avoid; }
p.note { font-size: 8pt; color: var(--muted); font-style: italic; }
hr { display: none; }
a { color: #1d4ed8; text-decoration: none; }
code { font-size: 8pt; background: #f1f3f6; padding: 0 2pt; border-radius: 2px; }
img { max-width: 100%; border: 1px solid var(--rule); border-radius: 4px; margin: 4pt 0; }

/* --- tables --- */
table { border-collapse: collapse; margin: 4pt 0 10pt; font-size: 8.5pt; width: auto; }
thead { display: table-header-group; }
th { font-size: 6.8pt; letter-spacing: .6pt; text-transform: uppercase; color: var(--muted);
     text-align: left; font-weight: 700; padding: 4pt 7pt; white-space: nowrap; border-bottom: 1.5px solid var(--navy); }
td { padding: 3pt 7pt; border-bottom: 1px solid var(--rule); vertical-align: top; }
tbody tr:nth-child(even) td { background: var(--zebra); }
tr { break-inside: avoid; }
table.wide { font-size: 7.4pt; width: 100%; }
table.wide th { font-size: 6pt; padding: 3pt 4pt; }
table.wide td { padding: 2.5pt 4pt; white-space: nowrap; }
.up { color: var(--up); font-weight: 600; }
.down { color: var(--down); font-weight: 600; }

/* --- callouts --- */
.callout { margin: 8pt 0 10pt; padding: 7pt 10pt; border-radius: 5px; break-inside: avoid;
           border-left: 4px solid #2563eb; background: #eff4ff; }
.callout p { margin: 3pt 0; }
.callout-title { font-weight: 700; font-size: 8pt; letter-spacing: .8pt;
                 text-transform: uppercase; margin-bottom: 3pt; color: #1e3a8a; }
.callout.important { border-color: var(--gold); background: #fbf6e6; }
.callout.important .callout-title { color: #7a5c00; }
.callout.tip { border-color: #0e7c66; background: #e9f6f2; }
.callout.tip .callout-title { color: #0b5e4e; }
.callout.tip p { font-size: 9.5pt; }
.callout.warning, .callout.caution { border-color: #c2410c; background: #fdf1e9; }
.callout.warning .callout-title { color: #9a3412; }
"""


def find_report(day):
    """Newest report for the day; the scanner writes a -auto variant if one exists."""
    hits = sorted(DAILY.glob(f"Premarket_Report_{day}*.md"),
                  key=lambda p: p.stat().st_mtime)
    return hits[-1] if hits else None


def _num(s):
    return float(s.replace(",", "").rstrip("%"))


def tiles(text):
    """Masthead tiles read from the note: (key, value, sub, tone) each."""
    out = []
    m = re.search(r"\*\*Trend — ([A-Z ]+?)\.\*\* S&P 500 at ([\d,.]+), ([\d.]+% \w+)", text)
    if m:
        tone = "up" if "BULL" in m.group(1) else "down" if "BEAR" in m.group(1) else ""
        out.append(("Trend", m.group(1).title().replace(" Market", ""),
                    f"S&P {m.group(2)} · {m.group(3)} high", tone))
    m = re.search(r"\*\*Sentiment — ([A-Z ]+?) \((\d+)/100\)", text)
    if m:
        tone = "down" if "FEAR" in m.group(1) else "up" if "GREED" in m.group(1) else ""
        out.append(("Fear & Greed", m.group(2), m.group(1).title(), tone))
    m = re.search(r"\*\*VIX ([\d.]+)\*\* \(([+-][\d.]+%)\)(?: — (\w+))?", text)
    if m:
        out.append(("VIX", m.group(1), f"{m.group(2)} · {m.group(3) or ''}".strip(" ·"), ""))
    m = re.search(r"\(AAII, weekly\):\*\* ([\d.]+)% bullish · [\d.]+% neutral · ([\d.]+)% bearish", text)
    if m:
        spread = _num(m.group(1)) - _num(m.group(2))
        out.append(("AAII bull–bear", f"{spread:+.1f}",
                    f"{m.group(1)}% bull · {m.group(2)}% bear", "up" if spread > 0 else "down"))
    m = re.search(r"^\| US 10Y \| ([\d.]+%) \| ([+-][\d.]+ bp) \|", text, re.M)
    if m:
        out.append(("US 10-year", m.group(1), m.group(2), ""))
    m = re.search(r"^\| 1 \| \*\*([A-Z.]+)\*\*[^|]*\| (\+[\d.]+%) \| (\$[\d.]+)", text, re.M)
    if m:
        n = len(re.findall(r"^\| \d+ \| \*\*[A-Z.]+\*\*", text, re.M))
        out.append(("Top gapper", m.group(1), f"{m.group(2)} at {m.group(3)} · {n} on list", "up"))
    # Evening digest: the board's top after-hours gainer (the table has a Company column).
    m = re.search(r"^\| 1 \| \*\*([A-Z.]+)\*\*[^|]*\| [^|]+ \| ([+-][\d.]+%) \| \$[\d.]+ \| \*\*(\$[\d.]+)\*\* \|.*$", text, re.M)
    if m:
        pinned = "pinned" in m.group(0)
        out.append(("Top after hours", m.group(1),
                    f"{m.group(2)} to {m.group(3)}" + (" · deal-pinned" if pinned else ""),
                    "" if pinned else "up"))
    # After-Hours Watch, morning and evening: the ticker and the price it must hold.
    m = re.search(r"<!-- ahwatch ([A-Z.]+) ([\d.]+) (\w+) -->", text)
    if m:
        sym, lvl, state = m.group(1), m.group(2), m.group(3)
        sub, tone = {
            "go": (f"✓ holding above ${lvl}", "up"),
            "lost": (f"pushed, then lost ${lvl}", "down"),
            "pass": (f"never cleared ${lvl}", "down"),
            "evening": (f"hold ${lvl} · {settings.et_hours_local(4, 5)}", ""),
        }.get(state, (f"hold ${lvl}", ""))
        out.append(("AH watch", sym, sub, tone))
    return out


def colour_moves(text):
    """Wrap +/- values in move columns with up/down spans, table by table."""
    out, cols = [], None
    for line in text.split("\n"):
        if not line.startswith("|"):
            cols = None
            out.append(line)
            continue
        cells = line.strip().strip("|").split("|")
        if cols is None:
            cols = {i for i, c in enumerate(cells) if c.strip() in MOVE_COLUMNS}
        elif cols and not set(line) <= set("|-: "):
            for i in cols:
                if i < len(cells):
                    v = cells[i].strip()
                    if re.match(r"^[+-]\$?[\d.,]+", v) and not re.match(r"^[+-]0(\.0+)?( |%|$)", v):
                        tone = "up" if v[0] == "+" else "down"
                        cells[i] = f' <span class="{tone}">{v}</span> '
            line = "|" + "|".join(cells) + "|"
        out.append(line)
    return "\n".join(out)


def obsidian_to_markdown(text):
    """Strip frontmatter and the header lines the masthead replaces, and turn
    Obsidian-only syntax into plain markdown."""
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)
    text = re.sub(r"^# .*\n", "", text, count=1, flags=re.M)
    text = re.sub(r"^Generated by .*\n", "", text, count=1, flags=re.M)
    text = re.sub(r"^\*\*Generated:\*\*.*\n", "", text, count=1, flags=re.M)

    # Image embeds: ![[file.png]] -> an image from SRB_OUTPUT_DIR/attachments, if it is there.
    def embed(m):
        img = ATTACH / m.group(1).split("|")[0]
        return f"![]({img.as_uri()})" if img.exists() else ""
    text = re.sub(r"!\[\[([^\]]+)\]\]", embed, text)

    # Wikilinks: [[target|label]] -> label, [[target]] -> target.
    text = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)

    text = colour_moves(text)

    # Callouts: a "> [!kind] title" blockquote becomes a styled div.
    out, lines, i = [], text.split("\n"), 0
    while i < len(lines):
        m = re.match(r"^> \[!(\w+)\][+-]?\s*(.*)$", lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        kind, title = m.group(1).lower(), m.group(2)
        body = []
        i += 1
        while i < len(lines) and lines[i].startswith(">"):
            body.append(lines[i][1:].lstrip(" ") if lines[i] != ">" else "")
            i += 1
        inner = markdown.markdown("\n".join(body), extensions=["tables"])
        out += ["", f'<div class="callout {kind}"><div class="callout-title">'
                f'{markdown.markdown(title)[3:-4]}</div>{inner}</div>', ""]
    return "\n".join(out)


def polish_html(body):
    """Small passes markdown cannot express: wide tables, muted footnote lines."""
    def table(m):
        return '<table class="wide">' if m.group(1).count("<th") > 9 else "<table>"
    body = re.sub(r"<table>(.*?</thead>)", lambda m: table(m) + m.group(1), body, flags=re.S)
    body = re.sub(r"<p><em>(.*?)</em></p>", r'<p class="note">\1</p>', body, flags=re.S)
    # A caption line stays on the same page as the table it introduces.
    body = re.sub(r"<p>((?:(?!</p>).)*)</p>(\s*<table)", r'<p class="lead">\1</p>\2', body, flags=re.S)
    return body


def render(md_path):
    raw = md_path.read_text()
    title = re.search(r"^# (?:(.+?) — )?(.*)$", raw, re.M)
    kind = (title.group(1) if title and title.group(1) else "Premarket Report")
    day = title.group(2) if title else md_path.stem
    gen = re.search(r"^\*\*Generated:\*\* (.*)$", raw, re.M)

    tile_html = "".join(
        f'<div class="tile {tone}"><div class="k">{html.escape(k)}</div>'
        f'<div class="v">{html.escape(v)}</div><div class="s">{html.escape(s)}</div></div>'
        for k, v, s, tone in tiles(raw))
    body = polish_html(markdown.markdown(obsidian_to_markdown(raw),
                                         extensions=["tables", "sane_lists"]))
    page = (
        f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body>"
        f"<div class='mast'><div class='eyebrow'>{html.escape(kind)}</div>"
        f"<h1>{html.escape(day)}</h1>"
        f"<div class='sub'>{markdown.markdown(gen.group(1))[3:-4] if gen else ''}</div></div>"
        f"<div class='tiles'>{tile_html}</div>{body}</body></html>")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf = OUT_DIR / md_path.with_suffix(".pdf").name
    HTML(string=page, base_url=str(md_path.parent)).write_pdf(pdf)
    return pdf


def open_pdf(pdf):
    """Hand the PDF to the desktop's default viewer."""
    subprocess.run(["xdg-open", str(pdf)], check=True)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        md_path = Path(args[0])
    else:
        md_path = find_report(datetime.now(settings.LOCAL).strftime("%Y-%m-%d"))
    if not md_path or not md_path.exists():
        print("report_pdf: no premarket report for today", file=sys.stderr)
        return 1
    pdf = render(md_path)
    print(f"report_pdf: wrote {pdf}")
    if "--open" in sys.argv:
        open_pdf(pdf)
    return 0


if __name__ == "__main__":
    sys.exit(main())
