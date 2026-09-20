"""Build a single self-contained HTML dashboard from a batch run's reports.

A batch run leaves one markdown tree per ticker under ``<results_dir>/reports``,
which is fine for grepping and painful for reviewing 50 of them. This collapses
a run into one offline HTML file: the ratings at a glance, how often each data
source actually reached the final report, and every report readable inline.

It is deliberately a local file, not a served app. There is nothing to install,
nothing to keep running, and the analysis never leaves the machine — open it
with a double-click. Nothing here calls an LLM or the network, so it costs no
API quota.

    python scripts/build_report_dashboard.py                  # today
    python scripts/build_report_dashboard.py --date 2026-09-20
    python scripts/build_report_dashboard.py --open           # and open it
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import webbrowser
from pathlib import Path

from tradingagents.dataflows.utils import get_current_date
from tradingagents.default_config import DEFAULT_CONFIG

# Each data point the India layer injects, with a pattern that recognises it in
# a finished report. Used for the coverage bar: "did this actually get used?"
COVERAGE = [
    ("FII/DII flows", r"\bFII\b|\bDII\b"),
    ("Promoter holding", r"[Pp]romoter"),
    ("India VIX", r"India VIX"),
    ("Put-call ratio", r"put-call|\bPCR\b"),
    ("RBI policy rate", r"repo rate|\bRBI\b"),
    ("NSE announcements", r"Allotment|Credit Rating|NSE announcement"),
]
RATING_ORDER = ["Strong Buy", "Overweight", "Buy", "Hold", "Underweight", "Sell", "Strong Sell"]


def _md_to_html(md: str) -> str:
    """Minimal, dependency-free markdown rendering.

    Only what these reports actually use: headings, tables, bold, italics,
    bullets and paragraphs. Everything is escaped first, so report text can
    never inject markup into the page.
    """
    out: list[str] = []
    in_table = in_list = False
    for raw in md.splitlines():
        line = html.escape(raw.rstrip())
        if not line.strip():
            if in_table:
                out.append("</tbody></table>")
                in_table = False
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        # Tables: a row of pipes, with the |---| separator skipped.
        if line.lstrip().startswith("|") and line.count("|") >= 2:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells if c):
                continue
            if not in_table:
                out.append("<table><tbody>")
                in_table = True
            out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</tbody></table>")
            in_table = False
        heading = re.match(r"^(#{1,6})\s+(.*)", line)
        if heading:
            if in_list:
                out.append("</ul>")
                in_list = False
            level = min(len(heading.group(1)) + 1, 6)
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue
        bullet = re.match(r"^\s*[-*]\s+(.*)", line)
        if bullet:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(bullet.group(1))}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        out.append(f"<p>{_inline(line)}</p>")
    if in_table:
        out.append("</tbody></table>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def _inline(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"`([^`]+?)`", r"<code>\1</code>", text)
    # An "<x unavailable: ...>" sentinel is escaped by now; highlight it so a
    # failed fetch is obvious rather than buried in prose.
    return re.sub(r"(&lt;[^&]*?unavailable[^&]*?&gt;)", r'<span class="sentinel">\1</span>', text)


def collect(date: str) -> tuple[list[dict], dict]:
    results_dir = Path(DEFAULT_CONFIG["results_dir"])
    reports_dir = results_dir / "reports"
    csv_path = results_dir / "india_universe_runs.csv"

    rows_by_ticker: dict[str, dict] = {}
    if csv_path.exists():
        with csv_path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("date") == date:
                    # Keep the last row per ticker: a re-run supersedes a failure.
                    rows_by_ticker[row["ticker"]] = row

    entries: list[dict] = []
    for ticker, row in sorted(rows_by_ticker.items()):
        folder = reports_dir / f"{ticker.replace('/', '_')}_{date}"
        report = folder / "complete_report.md"
        text = report.read_text(encoding="utf-8", errors="replace") if report.exists() else ""
        entries.append({
            "ticker": ticker,
            "company": row.get("company_name") or ticker,
            "status": row.get("status", "?"),
            "signal": row.get("signal") or ("ERROR" if row.get("status") != "ok" else "?"),
            "elapsed": row.get("elapsed_seconds") or "",
            "error": row.get("error") or "",
            "body": _md_to_html(text) if text else "",
            "coverage": [name for name, pat in COVERAGE if text and re.search(pat, text)],
            "sentinels": len(re.findall(r"<[^>]*?unavailable", text)) if text else 0,
        })

    ok = [e for e in entries if e["status"] == "ok"]
    stats = {
        "total": len(entries),
        "ok": len(ok),
        "failed": len(entries) - len(ok),
        "ratings": {r: sum(1 for e in ok if e["signal"] == r) for r in RATING_ORDER},
        "coverage": {
            name: sum(1 for e in ok if name in e["coverage"]) for name, _ in COVERAGE
        },
        "sentinels": sum(e["sentinels"] for e in entries),
    }
    stats["ratings"] = {k: v for k, v in stats["ratings"].items() if v}
    other = [e["signal"] for e in ok if e["signal"] not in RATING_ORDER]
    for sig in other:
        stats["ratings"][sig] = stats["ratings"].get(sig, 0) + 1
    return entries, stats


# CSS lives outside the f-string template so its braces need no escaping --
# doubling every brace by hand is how the first version of this broke.
_CSS = """
:root{--bg:#fbfaf9;--fg:#1a1915;--mut:#6b6862;--line:#e5e2dd;--card:#fff;--acc:#3d6b8c;
--hold:#8a7a52;--buy:#2f6b4a;--err:#a8442a;--sent:#fdf0d5}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#16150f;--fg:#ecebe6;
--mut:#a5a199;--line:#31302a;--card:#1f1e18;--sent:#3a3113}}
:root[data-theme=dark]{--bg:#16150f;--fg:#ecebe6;--mut:#a5a199;--line:#31302a;--card:#1f1e18;--sent:#3a3113}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:1.6rem;margin:0 0 4px}
.sub{color:var(--mut);margin:0 0 28px;font-size:.92rem}
h2{font-size:1.05rem;margin:34px 0 12px}
.grid{display:flex;flex-wrap:wrap;gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;min-width:104px}
.num{font-size:1.5rem;font-weight:650;font-variant-numeric:tabular-nums}
.lbl{color:var(--mut);font-size:.78rem;text-transform:uppercase;letter-spacing:.05em}
.cov{margin:9px 0}
.cov-h{display:flex;justify-content:space-between;font-size:.87rem;margin-bottom:3px}
.cov-n{color:var(--mut);font-variant-numeric:tabular-nums}
.bar{height:7px;background:var(--line);border-radius:4px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--acc)}
table{width:100%;border-collapse:collapse;font-size:.9rem}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:.76rem;text-transform:uppercase;letter-spacing:.05em}
tbody tr{cursor:pointer} tbody tr:hover{background:var(--card)}
.tk{font-weight:600;white-space:nowrap} .num-c,.cvc{font-variant-numeric:tabular-nums;color:var(--mut)}
.pill{display:inline-block;padding:1px 9px;border-radius:99px;font-size:.78rem;font-weight:600;background:var(--line)}
.p-hold{background:color-mix(in srgb,var(--hold) 22%,transparent);color:var(--hold)}
.p-overweight,.p-buy,.p-strong{background:color-mix(in srgb,var(--buy) 20%,transparent);color:var(--buy)}
.p-err{background:color-mix(in srgb,var(--err) 18%,transparent);color:var(--err)}
tr.err .tk{color:var(--err)}
dialog{border:1px solid var(--line);border-radius:12px;background:var(--card);color:var(--fg);
max-width:840px;width:94vw;max-height:88vh;padding:0}
dialog::backdrop{background:rgba(0,0,0,.45)}
.dh{position:sticky;top:0;background:var(--card);border-bottom:1px solid var(--line);
padding:14px 20px;display:flex;justify-content:space-between;align-items:center;gap:12px}
.dh h3{margin:0;font-size:1.05rem}
.db{padding:4px 22px 26px;overflow:auto;max-height:calc(88vh - 58px)}
.db table{font-size:.84rem;margin:10px 0} .db h3,.db h4{margin:20px 0 6px;font-size:.98rem}
.db code{background:var(--line);padding:1px 5px;border-radius:4px;font-size:.88em}
.sentinel{background:var(--sent);padding:1px 4px;border-radius:4px}
button{font:inherit;cursor:pointer;background:var(--line);color:var(--fg);border:0;
border-radius:7px;padding:5px 13px}
.note{color:var(--mut);font-size:.85rem;margin-top:8px}
"""

def render(entries: list[dict], stats: dict, date: str) -> str:
    n = max(stats["ok"], 1)
    cards = "".join(
        f'<div class="card"><div class="num">{v}</div><div class="lbl">{html.escape(k)}</div></div>'
        for k, v in (
            ("analysed", stats["ok"]),
            ("failed", stats["failed"]),
            ("failed fetches", stats["sentinels"]),
        )
    )
    ratings = "".join(
        f'<div class="card"><div class="num">{v}</div><div class="lbl">{html.escape(k)}</div></div>'
        for k, v in stats["ratings"].items()
    )
    coverage = "".join(
        f'<div class="cov"><div class="cov-h"><span>{html.escape(name)}</span>'
        f'<span class="cov-n">{v}/{stats["ok"]}</span></div>'
        f'<div class="bar"><i style="width:{v * 100 // n}%"></i></div></div>'
        for name, v in stats["coverage"].items()
    )
    rows = "".join(
        f'<tr class="{"err" if e["status"] != "ok" else ""}" data-i="{i}">'
        f'<td class="tk">{html.escape(e["ticker"])}</td>'
        f'<td>{html.escape(e["company"])}</td>'
        f'<td><span class="pill {"p-err" if e["status"] != "ok" else "p-" + e["signal"].split()[0].lower()}">'
        f'{html.escape(e["signal"])}</span></td>'
        f'<td class="num-c">{html.escape(str(e["elapsed"]))}</td>'
        f'<td class="cvc">{len(e["coverage"])}/{len(COVERAGE)}</td></tr>'
        for i, e in enumerate(entries)
    )
    payload = json.dumps([
        {"t": e["ticker"], "c": e["company"], "s": e["signal"], "b": e["body"], "e": e["error"]}
        for e in entries
    ])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Batch Review</title>
<style>{_CSS}</style></head><body><div class="wrap">
<h1>Batch review &mdash; {html.escape(date)}</h1>
<p class="sub">{stats['ok']} of {stats['total']} tickers analysed &middot; generated offline from saved reports &middot; no API calls</p>
<div class="grid">{cards}</div>
<h2>Ratings</h2><div class="grid">{ratings}</div>
<h2>Did the India data reach the final report?</h2>{coverage}
<p class="note">Share of successful reports that cite each source. A low bar is not
necessarily a fault &mdash; the put-call ratio and repo rate are situational.</p>
<h2>Tickers <span class="note">&mdash; click a row to read the full report</span></h2>
<table><thead><tr><th>Ticker</th><th>Company</th><th>Rating</th><th>Secs</th><th>Sources</th></tr></thead>
<tbody>{rows}</tbody></table>
</div>
<dialog id="d"><div class="dh"><h3 id="dt"></h3><button id="x">Close</button></div>
<div class="db" id="dbody"></div></dialog>
<script>
const R={payload};
const d=document.getElementById('d');
document.getElementById('x').onclick=()=>d.close();
document.querySelectorAll('tbody tr').forEach(tr=>tr.onclick=()=>{{
  const r=R[+tr.dataset.i];
  document.getElementById('dt').textContent=r.t+' — '+r.s;
  document.getElementById('dbody').innerHTML=r.b||('<p>No report saved. '+(r.e?'<code>'+r.e+'</code>':'')+'</p>');
  d.showModal();
}});
</script></body></html>"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--date", default=None, help="Run date, yyyy-mm-dd (default: today).")
    ap.add_argument("--out", default=None, help="Output HTML path.")
    ap.add_argument("--open", action="store_true", help="Open it in your browser.")
    args = ap.parse_args(argv)

    date = args.date or get_current_date()
    entries, stats = collect(date)
    if not entries:
        print(f"No runs recorded for {date}. Use --date to pick another run.")
        return 1

    out = Path(args.out) if args.out else Path(DEFAULT_CONFIG["results_dir"]) / f"review_{date}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(entries, stats, date), encoding="utf-8")
    print(f"Wrote {out}")
    print(f"  {out.stat().st_size / 1024:.0f} KB | {stats['ok']}/{stats['total']} analysed | "
          f"ratings: {stats['ratings']}")
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
