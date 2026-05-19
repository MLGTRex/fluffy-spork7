"""
Workflow script for Executor.

Reads:
    - consolidation_portfolio.json (Stage 4 output)
    - Latest execution_*.json (for freshness check)

Orchestrates:
    1. Freshness: skip if latest execution log's source_portfolio_date >= current
       consolidation_date
    2. Load Alpaca credentials from env vars
    3. Instantiate paper TradingClient + StockHistoricalDataClient
    4. Call functional execute()
    5. Write timestamped output JSON

Freshness: re-runs if no execution log exists OR if the latest log's
source_portfolio_date is older than consolidation_portfolio.json's
consolidation_date.
"""

import os
import sys
import json
import asyncio
import argparse
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# Allow importing siblings
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from execute import execute

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient


# ============ CONFIG ============

# Toggle dry-run from here, or override via --dry-run / --live CLI flags.
# Default is DRY_RUN=True for safety.
DRY_RUN = False

# Drift tolerance: positions with absolute target-vs-actual diff smaller than
# this percent of equity are skipped (no order). 0.5% means a $500 diff on a
# $100K account is ignored.
DRIFT_TOLERANCE_PCT = 0.5

# Safety cap: no single order may exceed this percent of equity.
MAX_SINGLE_ORDER_PCT = 25.0

# Safety cap: total buy notional may exceed equity by at most this percent.
MAX_TOTAL_NOTIONAL_BUFFER_PCT = 5.0

# Hard safety toggle: refuse to run against a non-paper endpoint unless True.
# Leave False indefinitely until live trading is genuinely intended.
ALLOW_LIVE = False


# ============ PATHS ============

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EXECUTION_ROOT = os.path.join(SCRIPT_DIR, "..")
OUTPUT_DIR = os.path.join(EXECUTION_ROOT, "output")

# Stage 4's consolidation portfolio (this folder now lives inside Stage 4 DRAFT)
STAGE4_OUTPUT_DIR = os.path.normpath(
    os.path.join(EXECUTION_ROOT, "..", "output")
)
CONSOLIDATION_PATH = os.path.join(STAGE4_OUTPUT_DIR, "consolidation_portfolio.json")


# ============ FRESHNESS ============

def _list_execution_logs() -> list:
    """Return execution_*.json files in the output dir, sorted oldest -> newest."""
    if not os.path.isdir(OUTPUT_DIR):
        return []
    files = [
        os.path.join(OUTPUT_DIR, f)
        for f in os.listdir(OUTPUT_DIR)
        if f.startswith("execution_") and f.endswith(".json")
    ]
    files.sort()
    return files


def _latest_execution_log() -> dict:
    """Return the most recent execution log (by filename sort), or None."""
    files = _list_execution_logs()
    if not files:
        return None
    try:
        with open(files[-1], "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[Execution] WARNING: could not parse latest log {files[-1]}: {e}")
        return None


def is_execution_stale(target_portfolio: dict) -> bool:
    """
    Stale if:
      - No prior execution log exists, OR
      - Latest log's source_portfolio_date < current consolidation_date, OR
      - Latest log's status is not 'success' or 'success_dry_run' — failed and
        partial runs both mean some orders did not go out, so a re-run should
        pick up the missing ones.
    """
    target_date = target_portfolio.get("consolidation_date", "")
    if not target_date:
        print("[Execution] WARNING: target portfolio has no consolidation_date — treating as stale")
        return True

    latest = _latest_execution_log()
    if latest is None:
        print("[Execution] No prior execution log — will execute.")
        return True

    prior_status = latest.get("status", "")
    prior_source_date = latest.get("source_portfolio_date", "")

    # Successful prior run against an equal-or-newer source = no work needed.
    # Note: 'partial' is deliberately excluded — partial means some orders
    # failed to submit, so a re-run should retry the missing ones.
    if prior_status == "success" and prior_source_date >= target_date:
        print(
            f"[Execution] Latest log (status={prior_status}, "
            f"source_portfolio_date={prior_source_date}) covers current "
            f"consolidation_date={target_date} — skipping."
        )
        return False

    # Dry-run completed against the same portfolio: we have not actually executed,
    # so a real (non-dry-run) invocation should proceed. But if the current run is
    # ALSO dry-run with the same source date, treat as already done to avoid loops.
    if prior_status == "success_dry_run" and prior_source_date >= target_date:
        if DRY_RUN:
            print(
                f"[Execution] Latest log is a dry-run against the same "
                f"consolidation_date={target_date}, and current invocation is also dry-run — skipping."
            )
            return False
        else:
            print(
                f"[Execution] Latest log is a dry-run; current invocation is live — proceeding."
            )
            return True

    print(
        f"[Execution] Latest log (status={prior_status}, "
        f"source_portfolio_date={prior_source_date}) does not cover "
        f"current consolidation_date={target_date} — will execute."
    )
    return True


# ============ HELPERS ============

def load_target_portfolio() -> dict:
    if not os.path.exists(CONSOLIDATION_PATH):
        print(f"[Execution] ERROR: consolidation portfolio not found at {CONSOLIDATION_PATH}")
        sys.exit(1)
    with open(CONSOLIDATION_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("status") != "optimal":
        print(
            f"[Execution] ERROR: consolidation_portfolio.json has status="
            f"'{data.get('status')}', not 'optimal'. Refusing to execute."
        )
        sys.exit(1)
    return data


def build_clients() -> tuple:
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        print("[Execution] ERROR: ALPACA_API_KEY and/or ALPACA_SECRET_KEY not set in environment")
        sys.exit(1)

    # Paper unless ALLOW_LIVE is True
    paper = not ALLOW_LIVE
    trading_client = TradingClient(api_key, secret_key, paper=paper)
    data_client = StockHistoricalDataClient(api_key, secret_key)
    return trading_client, data_client


def write_output(result: dict) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    # Filename uses exact UTC timestamp to keep audit trail of all attempts
    ts = result.get("execution_timestamp", datetime.now(timezone.utc).isoformat())
    safe_ts = ts.replace(":", "-").replace("+", "_")
    filename = f"execution_{safe_ts}.json"
    out_path = os.path.join(OUTPUT_DIR, filename)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=4, ensure_ascii=False)
    return out_path


def print_summary(result: dict) -> None:
    print(f"\n{'='*60}")
    print(f"  Execution complete — status: {result['status']}")
    print(f"{'='*60}\n")
    print(f"Execution timestamp:  {result['execution_timestamp']}")
    print(f"Source portfolio:     {result['source_portfolio_date']}")
    print(f"Dry run:              {result['dry_run']}")
    print(f"Account equity:       ${result['account_equity_at_execution']:,.2f}"
          if result['account_equity_at_execution'] else
          "Account equity:       (unknown)")
    print(f"Market state:         {result['market_state']}")
    print(f"Order strategy:       {result['order_submission_strategy']}")

    if result["cancelled_open_orders"]:
        print(f"\nCancelled open orders: {len(result['cancelled_open_orders'])}")
        for o in result["cancelled_open_orders"]:
            print(f"  - {o.get('symbol', '?')} {o.get('side', '?')} (id={o.get('order_id')})")

    if result["skipped_tickers"]:
        print(f"\nSkipped tickers: {len(result['skipped_tickers'])}")
        for s in result["skipped_tickers"]:
            print(f"  - {s.get('ticker')}: {s.get('reason')}")

    diff = result.get("diff", [])
    actionable = [d for d in diff if d["action"] != "hold"]
    if diff:
        print(f"\nDiff ({len(actionable)} actionable, {len(diff) - len(actionable)} hold):")
        for d in sorted(diff, key=lambda x: -abs(x["diff_dollars"])):
            arrow = {"buy": "BUY ", "sell": "SELL", "hold": "hold"}[d["action"]]
            print(
                f"  {arrow} {d['ticker']:6s}  "
                f"target={d['target_pct']:5.2f}% (${d['target_dollars']:>10,.2f})  "
                f"actual={d['actual_pct']:5.2f}% (${d['actual_dollars']:>10,.2f})  "
                f"diff=${d['diff_dollars']:>+10,.2f}"
            )

    orders = result.get("orders_submitted", [])
    if orders:
        print(f"\nOrders ({len(orders)}):")
        for o in orders:
            qty_or_notional = (
                f"qty={o['qty']}" if o["qty"] is not None
                else f"notional=${o['notional']:.2f}"
            )
            status_str = o["submission_status"]
            id_str = f"id={o['order_id']}" if o["order_id"] else ""
            print(
                f"  {o['side'].upper():4s}  {o['ticker']:6s}  "
                f"{qty_or_notional:25s}  TIF={o['time_in_force'].split('.')[-1]:8s}  "
                f"{status_str}  {id_str}"
            )

    if result["data_quality_flags"]:
        print(f"\nData quality flags ({len(result['data_quality_flags'])}):")
        for f in result["data_quality_flags"]:
            print(f"  - {f}")


# ============ MAIN ============

async def main():
    global DRY_RUN

    parser = argparse.ArgumentParser(description="Execute the consolidation portfolio on Alpaca")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="Force dry-run mode")
    group.add_argument("--live", action="store_true",
                       help="Force real submission (overrides DRY_RUN=True at top of script)")
    args, _ = parser.parse_known_args()

    if args.dry_run:
        DRY_RUN = True
    elif args.live:
        DRY_RUN = False

    print(f"[Execution] Starting (DRY_RUN={DRY_RUN}, ALLOW_LIVE={ALLOW_LIVE})...")

    # Step 1: load target portfolio
    target_portfolio = load_target_portfolio()
    print(
        f"[Execution] Loaded target portfolio "
        f"(consolidation_date={target_portfolio.get('consolidation_date')}, "
        f"positions={len((target_portfolio.get('portfolio') or {}).get('positions', []))})"
    )

    # Step 2: freshness
    if not is_execution_stale(target_portfolio):
        return

    # Step 3: clients
    trading_client, data_client = build_clients()

    # Step 4: execute
    config = {
        "DRY_RUN": DRY_RUN,
        "DRIFT_TOLERANCE_PCT": DRIFT_TOLERANCE_PCT,
        "MAX_SINGLE_ORDER_PCT": MAX_SINGLE_ORDER_PCT,
        "MAX_TOTAL_NOTIONAL_BUFFER_PCT": MAX_TOTAL_NOTIONAL_BUFFER_PCT,
        "ALLOW_LIVE": ALLOW_LIVE,
    }

    # The functional execute() is synchronous (Alpaca SDK is sync). Run in a thread.
    result = await asyncio.to_thread(execute, target_portfolio, trading_client, data_client, config)

    # Step 5: write output
    out_path = write_output(result)

    # Step 6: summary
    print_summary(result)
    print(f"\nWritten to {out_path}")

    # Exit code reflects outcome
    if result["status"] in ("success", "success_dry_run"):
        return
    elif result["status"] == "partial":
        print("\n[Execution] WARNING: partial success — some orders failed to submit")
        sys.exit(1)
    else:
        print(f"\n[Execution] FAILED: status={result['status']}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())