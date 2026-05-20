"""
Portfolio overview — the dashboard root for the UI.

Joins:
  - Target portfolio from the latest snapshot's stage4/consolidation_portfolio.json
  - Live Alpaca positions (qty, market_value, unrealized_pl, current_price, ...)
  - Stage 3 per-ticker thesis fields (conviction, thesis_summary, expected_return_12m)
  - Performance summary block from computePortfolioPerf.compute()
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

import config

logger = logging.getLogger(__name__)


def _load_json(path: str) -> Optional[dict]:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Could not parse %s: %s", path, e)
        return None


def _stage3_by_ticker_in_snapshot(snapshot_dir: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    pattern = os.path.join(snapshot_dir, "stage3", "*_research.json")
    for p in glob.glob(pattern):
        d = _load_json(p)
        if not d:
            continue
        ticker = d.get("ticker")
        if not ticker:
            m = re.match(r".+_([A-Z][A-Z0-9.\-]*)_research\.json$", os.path.basename(p))
            if m:
                ticker = m.group(1)
        if ticker:
            out[ticker] = d
    return out


def _position_status(target_in: bool, alpaca_qty: float) -> str:
    if target_in and alpaca_qty > 0:
        return "held"
    if target_in and alpaca_qty == 0:
        return "pending_buy"
    if not target_in and alpaca_qty > 0:
        return "untracked"
    return "absent"


def _safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build(
    snapshot_dir: Optional[str],
    snapshot_id: Optional[str],
    alpaca_bundle: dict,
    performance_block: dict,
) -> dict:
    """
    Args:
        snapshot_dir: path to the current (or newest) snapshot directory, or None
        snapshot_id:  matching id (for citing in the overview)
        alpaca_bundle: result from live.fetchAlpaca.fetch_all()
        performance_block: result from performance.computePortfolioPerf.compute()
    """
    now = datetime.now(timezone.utc).isoformat()
    overview: dict = {
        "as_of": now,
        "current_snapshot_id": snapshot_id,
        "target_portfolio_source_file": None,
        "account": None,
        "pipeline_run": None,
        "positions": [],
        "performance": performance_block.get("performance", {}),
    }

    # Account block
    account = alpaca_bundle.get("account") or {}
    if account:
        overview["account"] = {
            "equity": _safe_float(account.get("equity")),
            "cash": _safe_float(account.get("cash")),
            "buying_power": _safe_float(account.get("buying_power")),
            "portfolio_value": _safe_float(account.get("portfolio_value")),
            "last_equity": _safe_float(account.get("last_equity")),
            "source": "stale_cache" if alpaca_bundle.get("account_stale") else "alpaca",
        }

    # Target portfolio + Stage 3 thesis
    target_portfolio = None
    stage3_by_ticker: dict[str, dict] = {}
    if snapshot_dir and os.path.isdir(snapshot_dir):
        consol_path = os.path.join(snapshot_dir, "stage4", "consolidation_portfolio.json")
        target_portfolio = _load_json(consol_path)
        if target_portfolio:
            overview["target_portfolio_source_file"] = consol_path
        stage3_by_ticker = _stage3_by_ticker_in_snapshot(snapshot_dir)

    if target_portfolio:
        overview["pipeline_run"] = {
            "latest_consolidation_date": target_portfolio.get("consolidation_date"),
            "latest_reconciliation_date": target_portfolio.get("reconciled_date"),
            "status": target_portfolio.get("status"),
            "positions_count": len(
                (target_portfolio.get("portfolio") or {}).get("positions") or []
            ),
        }

    # Build positions block
    target_positions: list[dict] = (
        (target_portfolio or {}).get("portfolio") or {}
    ).get("positions") or []
    target_by_ticker = {p["ticker"]: p for p in target_positions if p.get("ticker")}

    alpaca_positions: list[dict] = alpaca_bundle.get("positions") or []
    alpaca_by_ticker = {
        p.get("symbol"): p for p in alpaca_positions if p.get("symbol")
    }

    all_tickers = set(target_by_ticker.keys()) | set(alpaca_by_ticker.keys())
    equity = (overview.get("account") or {}).get("equity")

    rows: list[dict] = []
    for ticker in sorted(all_tickers):
        target = target_by_ticker.get(ticker, {})
        alpaca = alpaca_by_ticker.get(ticker, {})
        stage3 = stage3_by_ticker.get(ticker, {})

        market_value = _safe_float(alpaca.get("market_value")) or 0.0
        actual_pct = (
            (market_value / equity * 100.0) if (equity and equity > 0) else None
        )
        target_pct = _safe_float(target.get("allocation_pct"))
        drift_pct = (
            (actual_pct - target_pct)
            if (actual_pct is not None and target_pct is not None)
            else None
        )

        qty = _safe_float(alpaca.get("qty")) or 0.0
        alpaca_avg_entry = _safe_float(alpaca.get("avg_entry_price"))
        entry_price_pipeline = _safe_float(target.get("entry_price"))
        row = {
            "ticker": ticker,
            "sector": target.get("sector") or stage3.get("valuation_metrics", {}).get("sector"),
            "target_allocation_pct": target_pct,
            "actual_allocation_pct": actual_pct,
            "drift_pct": drift_pct,
            "qty": qty,
            "market_value": market_value if alpaca else None,
            "cost_basis": _safe_float(alpaca.get("cost_basis")),
            "current_price": _safe_float(alpaca.get("current_price")),
            "lastday_price": _safe_float(alpaca.get("lastday_price")),
            "unrealized_pl": _safe_float(alpaca.get("unrealized_pl")),
            "unrealized_plpc": _safe_float(alpaca.get("unrealized_plpc")),
            "entry_date_pipeline": target.get("entry_date"),
            "entry_price_pipeline": entry_price_pipeline,
            # Prefer Alpaca's actual fill average; fall back to the pipeline
            # snapshot for pending buys or positions missing from Alpaca.
            "entry_price": alpaca_avg_entry if alpaca_avg_entry is not None else entry_price_pipeline,
            "expected_return_12m": _safe_float(
                target.get("expected_return_12m")
                if target.get("expected_return_12m") is not None
                else stage3.get("expected_return_12m")
            ),
            "conviction": stage3.get("conviction"),
            "thesis_summary": stage3.get("thesis_summary"),
            "status": _position_status(ticker in target_by_ticker, qty),
        }
        rows.append(row)

    overview["positions"] = rows
    return overview
