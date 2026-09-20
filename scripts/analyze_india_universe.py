"""Batch-run TradingAgentsGraph over a universe of Indian tickers.

Each ticker/date pair is a full multi-agent pipeline run — with default
settings (4 analysts, 1 debate round, 1 risk round) that's roughly 15-25 LLM
calls, so this WILL cost real API spend and take real wall time per ticker.
There is no "analyze all Indian stocks" mode here on purpose: NSE alone
lists ~2,000 companies, and running this pipeline on all of them is not a
software gap, it's what the architecture costs per ticker multiplied by a
few thousand. Use --limit and --dry-run to control spend before a real run.

Usage:
    # Preview the plan without touching the network or an LLM (free):
    python scripts/analyze_india_universe.py --dry-run

    # Confirm every ticker in the universe still resolves on yfinance before
    # spending money on it (a demerger/rename can silently 404 a "verified"
    # ticker — see tradingagents/dataflows/india_universe.py):
    python scripts/analyze_india_universe.py --verify-only

    # Small, cheap live test (needs an LLM key configured in .env):
    python scripts/analyze_india_universe.py --limit 3 --date 2026-09-15

    # Full Nifty-50-ish run, paced 5s apart, resumable if interrupted:
    python scripts/analyze_india_universe.py --date 2026-09-15 --delay 5 --resume

    # Your own list instead of the built-in universe:
    python scripts/analyze_india_universe.py --tickers RELIANCE.NS,TCS.NS,INFY.NS
    python scripts/analyze_india_universe.py --tickers-file my_tickers.txt

Output: one row per ticker appended to --output (CSV), written incrementally
so a crash or Ctrl-C never loses completed work. Each ticker's full report
tree is also still written under results_dir by TradingAgentsGraph itself,
exactly as a single-ticker run would.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

from tradingagents.agents.utils.rating import is_review
from tradingagents.dataflows.india_universe import NIFTY_50_APPROX, verify_universe
from tradingagents.dataflows.utils import get_current_date, safe_ticker_component
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

CSV_FIELDS = [
    "ticker", "company_name", "date", "status", "signal",
    "decision_excerpt", "elapsed_seconds", "error", "run_at",
]


def _resolve_universe(args) -> dict[str, str]:
    """Return {ticker: display_name} from --tickers / --tickers-file / --universe."""
    if args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
        return {t: t for t in tickers}
    if args.tickers_file:
        path = Path(args.tickers_file)
        tickers = [
            line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        return {t: t for t in tickers}
    if args.universe == "nifty50":
        return dict(NIFTY_50_APPROX)
    raise ValueError(f"Unknown universe: {args.universe!r}")


def _already_done(output_path: Path, date: str) -> set[str]:
    """Tickers with a completed (status != error) row for this date, for --resume."""
    if not output_path.exists():
        return set()
    done = set()
    with output_path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("date") == date and row.get("status") == "ok":
                done.add(row["ticker"])
    return done


def _append_row(output_path: Path, row: dict) -> None:
    is_new = not output_path.exists()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Batch-run TradingAgentsGraph over an Indian ticker universe.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--universe", default="nifty50", choices=["nifty50"],
                         help="Built-in universe to use (default: nifty50).")
    parser.add_argument("--tickers", default=None,
                         help="Comma-separated ticker list, overrides --universe.")
    parser.add_argument("--tickers-file", default=None,
                         help="File with one ticker per line, overrides --universe.")
    parser.add_argument("--date", default=None,
                         help="Analysis date, YYYY-MM-DD (default: today).")
    parser.add_argument("--limit", type=int, default=None,
                         help="Only analyze the first N tickers (after --resume filtering).")
    parser.add_argument("--delay", type=float, default=3.0,
                         help="Seconds to pace between tickers (default: 3.0).")
    parser.add_argument("--output", default=None,
                         help="CSV path (default: <results_dir>/india_universe_runs.csv).")
    parser.add_argument("--resume", action="store_true",
                         help="Skip tickers with an existing successful row for this date.")
    parser.add_argument("--analysts", default="market,social,news,fundamentals",
                         help="Comma-separated analyst keys (default: all four).")
    parser.add_argument("--debate-rounds", type=int, default=None,
                         help="Override max_debate_rounds (default: config default).")
    parser.add_argument("--risk-rounds", type=int, default=None,
                         help="Override max_risk_discuss_rounds (default: config default).")
    parser.add_argument("--provider", default=None,
                         help="Override llm_provider (default: config default, 'openai').")
    parser.add_argument("--deep-model", default=None,
                         help="Override deep_think_llm.")
    parser.add_argument("--quick-model", default=None,
                         help="Override quick_think_llm.")
    parser.add_argument("--dry-run", action="store_true",
                         help="Print the plan and exit. No network or LLM calls.")
    parser.add_argument("--verify-only", action="store_true",
                         help="Live-check every ticker resolves on yfinance, then exit.")
    args = parser.parse_args()

    universe = _resolve_universe(args)
    date = args.date or get_current_date()

    if args.verify_only:
        print(f"Verifying {len(universe)} tickers against yfinance...")
        good, bad = verify_universe(universe)
        print(f"OK: {len(good)}  FAILED: {len(bad)}")
        if bad:
            print("Failed tickers (delisted, renamed, or typo'd):")
            for t in bad:
                print(f"  {t}  ({universe.get(t, '?')})")
        return 1 if bad else 0

    tickers = list(universe.keys())
    output_path = Path(args.output) if args.output else (
        Path(DEFAULT_CONFIG["results_dir"]) / "india_universe_runs.csv"
    )

    skip = _already_done(output_path, date) if args.resume else set()
    plan = [t for t in tickers if t not in skip]
    if args.limit is not None:
        plan = plan[: args.limit]

    print(f"Universe: {len(tickers)} tickers | date: {date} | "
          f"skipping {len(skip)} already-done | will run: {len(plan)}")
    print(f"Output: {output_path}")

    if args.dry_run:
        print("\n--dry-run: no network or LLM calls. Plan:")
        for i, t in enumerate(plan, 1):
            print(f"  [{i}/{len(plan)}] {t}  ({universe.get(t, '?')})")
        return 0

    selected_analysts = tuple(a.strip() for a in args.analysts.split(",") if a.strip())
    config = DEFAULT_CONFIG.copy()
    if args.debate_rounds is not None:
        config["max_debate_rounds"] = args.debate_rounds
    if args.risk_rounds is not None:
        config["max_risk_discuss_rounds"] = args.risk_rounds
    if args.provider is not None:
        config["llm_provider"] = args.provider
    if args.deep_model is not None:
        config["deep_think_llm"] = args.deep_model
    if args.quick_model is not None:
        config["quick_think_llm"] = args.quick_model

    # One graph instance, reused across tickers — propagate() reassigns
    # self.ticker per call by design, so this avoids rebuilding LLM clients
    # for every ticker in the batch.
    ta = TradingAgentsGraph(selected_analysts=selected_analysts, config=config)

    tally: dict[str, int] = {}
    for i, ticker in enumerate(plan, 1):
        name = universe.get(ticker, ticker)
        print(f"\n[{i}/{len(plan)}] {ticker} ({name}) — {date}", flush=True)
        started = time.monotonic()
        row = {
            "ticker": ticker, "company_name": name, "date": date,
            "run_at": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            final_state, signal = ta.propagate(ticker, date)
            elapsed = time.monotonic() - started
            decision = (final_state.get("final_trade_decision") or "")[:200]
            report_path = ta.save_reports(
                final_state,
                ticker,
                save_path=Path(DEFAULT_CONFIG["results_dir"]) / "reports"
                / f"{safe_ticker_component(ticker)}_{date}",
            )
            row.update({
                "status": "ok", "signal": signal, "decision_excerpt": decision,
                "elapsed_seconds": round(elapsed, 1), "error": "",
            })
            tag = "REVIEW (no parseable rating)" if is_review(signal) else signal
            print(f"  -> {tag}  ({elapsed:.0f}s)  report: {report_path}")
            tally[signal] = tally.get(signal, 0) + 1
        except Exception as exc:  # noqa: BLE001 — one bad ticker must not kill the batch
            elapsed = time.monotonic() - started
            row.update({
                "status": "error", "signal": "", "decision_excerpt": "",
                "elapsed_seconds": round(elapsed, 1), "error": f"{type(exc).__name__}: {exc}",
            })
            print(f"  -> ERROR: {type(exc).__name__}: {exc}")
            tally["ERROR"] = tally.get("ERROR", 0) + 1
        _append_row(output_path, row)

        if i < len(plan) and args.delay:
            time.sleep(args.delay)

    print(f"\nDone. {len(plan)} tickers processed.")
    for label, count in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {label}: {count}")
    print(f"Results: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
