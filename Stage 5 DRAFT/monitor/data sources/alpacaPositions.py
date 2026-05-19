"""
Alpaca positions fetcher — live brokerage position data for the Stage 5 monitor.

Fetches the account's currently-held positions from Alpaca, exposing each
position's real average cost basis and P&L:
  - avg_entry_price : scaling-adjusted weighted average cost
  - qty             : current share quantity
  - market_value    : current dollar value of the position
  - current_price   : Alpaca's current mark
  - unrealized_pl   : unrealized P&L in dollars
  - unrealized_plpc : unrealized P&L as a fraction of cost basis

Used for:
  - Seeding the cumulative-signal evaluation anchor for held positions that
    have not yet been rerun by the pipeline invoker. Without a seeded anchor
    the slow-erosion (cumulative drift) detector cannot fire at all.

Designed to be:
  - Independent: standalone via CLI
  - Reusable: importable by the monitor orchestrator
  - Once-per-run: a single get_all_positions() call regardless of universe size
  - Best-effort: never raises; failures surface in the "errors" list and the
    caller degrades gracefully to file-only anchors.

Usage:
    from alpacaPositions import fetch_positions
    result = fetch_positions()
    if result["status"] == "ok":
        cost = result["positions"]["AAPL"]["avg_entry_price"]

CLI:
    python3 alpacaPositions.py
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv


# ============ CONFIGURATION ============

# Read positions from the paper account. This must match the account the
# executor trades on (execution DRAFT/executor/runExecute.py uses
# paper = not ALLOW_LIVE, with ALLOW_LIVE=False). Revisit if the executor is
# ever switched to live trading.
PAPER = True

# Load credentials from the project-root .env regardless of cwd.
# This file: Stage 5 DRAFT/monitor/data sources/alpacaPositions.py
_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
load_dotenv()  # also honor a .env discoverable from cwd


# ============ LOGGER ============

_logger = logging.getLogger("monitor.alpacaPositions")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)


# ============ HELPERS ============

def _normalize_symbol(symbol: str) -> str:
    """
    Normalize an Alpaca symbol to the repo's ticker form.

    Stage 1's universe builder converts '.' to '-' (e.g. BRK.B -> BRK-B) for
    yfinance compatibility, and that form propagates through every downstream
    stage. Alpaca reports class-share symbols with a dot, so normalize here so
    keys match the monitor's portfolio tickers.
    """
    return (symbol or "").strip().upper().replace(".", "-")


def _to_float(value):
    """Convert an Alpaca string field to float; return None if not parseable."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _empty_result() -> dict:
    return {
        "data_source": "alpacaPositions",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "status": "unknown",
        "paper": PAPER,
        "positions": {},
        "errors": [],
    }


# ============ MAIN FETCH ============

def fetch_positions() -> dict:
    """
    Fetch all held positions from the Alpaca account.

    Returns a result dict (see module docstring for shape). Never raises:
    any failure (missing SDK, missing credentials, API error) leaves
    positions empty and records a message in "errors", with status
    "fetch_failed".
    """
    result = _empty_result()

    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        result["status"] = "fetch_failed"
        result["errors"].append(
            "ALPACA_API_KEY and/or ALPACA_SECRET_KEY not set in environment"
        )
        _logger.warning(result["errors"][-1])
        return result

    try:
        from alpaca.trading.client import TradingClient
    except ImportError as e:
        result["status"] = "fetch_failed"
        result["errors"].append(
            f"alpaca-py is required for alpacaPositions but is not installed: {e}"
        )
        _logger.warning(result["errors"][-1])
        return result

    try:
        client = TradingClient(api_key, secret_key, paper=PAPER)
        positions_raw = client.get_all_positions()
    except Exception as e:
        result["status"] = "fetch_failed"
        result["errors"].append(f"Failed to fetch Alpaca positions: {e}")
        _logger.warning(result["errors"][-1])
        return result

    for p in positions_raw:
        try:
            ticker = _normalize_symbol(getattr(p, "symbol", None))
            if not ticker:
                continue
            result["positions"][ticker] = {
                "avg_entry_price": _to_float(getattr(p, "avg_entry_price", None)),
                "qty": _to_float(getattr(p, "qty", None)),
                "market_value": _to_float(getattr(p, "market_value", None)),
                "current_price": _to_float(getattr(p, "current_price", None)),
                "unrealized_pl": _to_float(getattr(p, "unrealized_pl", None)),
                "unrealized_plpc": _to_float(getattr(p, "unrealized_plpc", None)),
            }
        except Exception as e:
            result["errors"].append(f"Could not parse a position: {e}")
            _logger.warning(result["errors"][-1])

    result["status"] = "ok"
    _logger.info(
        f"Fetched {len(result['positions'])} Alpaca position(s) "
        f"(paper={PAPER})"
    )
    return result


# ============ CLI ============

def _cli():
    parser = argparse.ArgumentParser(description="Fetch Alpaca account positions")
    parser.parse_args()

    result = fetch_positions()
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(_cli())
