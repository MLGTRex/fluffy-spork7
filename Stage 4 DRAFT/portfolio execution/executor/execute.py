"""
Executor (functional).

Pure logic for translating a target portfolio into Alpaca orders. No file I/O,
no env loading, no freshness checks — those are the workflow script's job.

Inputs:
    - target_portfolio: dict with positions list (ticker, allocation_pct)
    - trading_client: an alpaca-py TradingClient instance
    - data_client: an alpaca-py StockHistoricalDataClient instance (reserved for
                   future use; not currently called in this stage)
    - config: dict with DRY_RUN, DRIFT_TOLERANCE_PCT, MAX_SINGLE_ORDER_PCT, ALLOW_LIVE

Returns:
    A structured result dict matching the per-run output schema, ready to be
    written to disk by the workflow.

Key responsibilities:
    1. Verify we are connected to a paper account (unless ALLOW_LIVE is set)
    2. Pull account state (equity) and current positions
    3. Check tradability of each target ticker; skip + flag untradable ones (e.g. ASX)
    4. Detect market state (open / queued-for-open / no-trading-day-imminent)
    5. Cancel-on-rerun rule: if account has zero positions, cancel all open orders
       before submitting; otherwise leave open orders alone (rebalance case)
    6. Compute target-vs-actual diff in dollars; filter out positions within drift tolerance
    7. Construct orders: notional market orders, DAY TIF, sells first then buys.
       Alpaca requires DAY TIF for any fractional order (regardless of market state);
       DAY orders submitted outside RTH are automatically queued for the next open.
    8. Submit orders (skipped if dry_run=True)
    9. Return a fully populated result dict
"""

import logging
from datetime import datetime, timezone

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient

logger = logging.getLogger(__name__)


# ============ MARKET STATE ============

def detect_market_state(trading_client: TradingClient) -> dict:
    """
    Determine the order submission strategy.

    Two paths exist:
        - RTH open: DAY market orders execute immediately
        - Outside RTH but next session imminent: DAY market orders are queued
          by Alpaca and execute at the next open

    Alpaca requires DAY TIF for any fractional order, so we always use DAY.
    The only thing to detect is whether submitting now is sensible at all
    (i.e. a trading day is imminent and the queued order will execute soon).

    Returns:
        {
            "state": "rth_open" | "queued_for_next_open" | "no_imminent_session",
            "next_open": ISO datetime string,
            "is_open": bool,
            "now_utc": ISO datetime string,
        }
    """
    clock = trading_client.get_clock()
    now_utc = clock.timestamp  # tz-aware datetime in UTC
    is_open = clock.is_open
    next_open = clock.next_open  # tz-aware datetime in UTC

    if is_open:
        state = "rth_open"
    else:
        # If the next session is within ~72 hours (covers a Friday-evening
        # submission for Monday open), submitting a DAY order now is fine —
        # Alpaca will queue it. Otherwise (e.g. extended exchange holidays),
        # bail out so the user is aware.
        seconds_to_next_open = (next_open - now_utc).total_seconds()
        if 0 < seconds_to_next_open <= 72 * 3600:
            state = "queued_for_next_open"
        else:
            state = "no_imminent_session"

    return {
        "state": state,
        "next_open": next_open.isoformat(),
        "is_open": is_open,
        "now_utc": now_utc.isoformat(),
    }


# ============ TRADABILITY ============

def check_tradability(trading_client: TradingClient, ticker: str) -> tuple:
    """
    Verify the ticker exists on Alpaca, is tradable, and is fractionable.

    Returns:
        (is_tradable: bool, reason: str or None)
        reason is populated only when is_tradable is False.
    """
    try:
        asset = trading_client.get_asset(ticker)
    except Exception as e:
        return False, f"Alpaca asset lookup failed: {e}"

    if not asset.tradable:
        return False, f"Asset {ticker} is not tradable on Alpaca"
    if not asset.fractionable:
        return False, f"Asset {ticker} is not fractionable (fractional shares required)"
    if str(asset.status).lower() != "active" and "active" not in str(asset.status).lower():
        return False, f"Asset {ticker} status is {asset.status}, not active"

    return True, None


# ============ DIFF COMPUTATION ============

def compute_diff(
    target_positions: list,
    current_positions: dict,
    equity: float,
    drift_tolerance_pct: float,
) -> list:
    """
    For each target position, compute target $, actual $, diff $, and action.

    Args:
        target_positions: [{"ticker": str, "allocation_pct": float (percent, e.g. 11.614)}]
        current_positions: {ticker: market_value_float}  current Alpaca holdings
        equity: account equity (float)
        drift_tolerance_pct: positions with absolute diff < drift_tolerance_pct of equity
                             are treated as no-op (action="hold")

    Returns:
        list of dicts:
            {
                "ticker": str,
                "target_pct": float,
                "actual_pct": float,
                "target_dollars": float,
                "actual_dollars": float,
                "diff_dollars": float,    # +ve = need to buy, -ve = need to sell
                "diff_pct": float,
                "action": "buy" | "sell" | "hold",
            }
    """
    drift_tolerance_dollars = (drift_tolerance_pct / 100.0) * equity

    diffs = []
    for pos in target_positions:
        ticker = pos["ticker"]
        target_pct = pos["allocation_pct"]
        target_dollars = (target_pct / 100.0) * equity
        actual_dollars = current_positions.get(ticker, 0.0)
        actual_pct = (actual_dollars / equity * 100.0) if equity > 0 else 0.0
        diff_dollars = target_dollars - actual_dollars
        diff_pct = target_pct - actual_pct

        if abs(diff_dollars) < drift_tolerance_dollars:
            action = "hold"
        elif diff_dollars > 0:
            action = "buy"
        else:
            action = "sell"

        diffs.append({
            "ticker": ticker,
            "target_pct": round(target_pct, 4),
            "actual_pct": round(actual_pct, 4),
            "target_dollars": round(target_dollars, 2),
            "actual_dollars": round(actual_dollars, 2),
            "diff_dollars": round(diff_dollars, 2),
            "diff_pct": round(diff_pct, 4),
            "action": action,
        })

    # Also handle positions in account but NOT in target (full liquidation)
    target_tickers = {p["ticker"] for p in target_positions}
    for ticker, market_value in current_positions.items():
        if ticker in target_tickers:
            continue
        if market_value <= 0:
            continue
        actual_pct = (market_value / equity * 100.0) if equity > 0 else 0.0
        diffs.append({
            "ticker": ticker,
            "target_pct": 0.0,
            "actual_pct": round(actual_pct, 4),
            "target_dollars": 0.0,
            "actual_dollars": round(market_value, 2),
            "diff_dollars": round(-market_value, 2),
            "diff_pct": round(-actual_pct, 4),
            "action": "sell",
        })

    return diffs


# ============ ORDER CONSTRUCTION ============

def build_orders(diffs: list, market_state: str) -> list:
    """
    Convert diff entries into MarketOrderRequest objects, sells first.

    Alpaca requires DAY TIF for any fractional order. DAY orders submitted
    outside RTH are queued and execute at the next open — functionally
    equivalent to MOO for our purposes, but compatible with fractional shares
    and notional sizing.

    Returns:
        list of (ticker, MarketOrderRequest, dollar_amount, side_str), sells first.
    """
    if market_state not in ("rth_open", "queued_for_next_open"):
        raise ValueError(f"Cannot build orders in market state '{market_state}'")

    sells = []
    buys = []

    for d in diffs:
        if d["action"] == "hold":
            continue

        ticker = d["ticker"]
        dollar_amount = abs(d["diff_dollars"])
        if dollar_amount <= 0:
            continue

        side = OrderSide.SELL if d["action"] == "sell" else OrderSide.BUY
        side_str = "sell" if d["action"] == "sell" else "buy"

        # Notional market order, DAY TIF — works for fractional, in RTH or
        # queued for next open. extended_hours is False (default) since we
        # want next regular-session open, not extended-hours fill.
        order_req = MarketOrderRequest(
            symbol=ticker,
            notional=round(dollar_amount, 2),
            side=side,
            time_in_force=TimeInForce.DAY,
        )

        entry = (ticker, order_req, dollar_amount, side_str)
        if side == OrderSide.SELL:
            sells.append(entry)
        else:
            buys.append(entry)

    # Sells first, then buys
    return sells + buys


# ============ MAIN ENTRY ============

def execute(
    target_portfolio: dict,
    trading_client: TradingClient,
    data_client: StockHistoricalDataClient,
    config: dict,
) -> dict:
    """
    Top-level execution. Performs all checks, builds and submits orders,
    returns the full result dict.

    Args:
        target_portfolio: parsed consolidation_portfolio.json
        trading_client: alpaca-py TradingClient (paper unless ALLOW_LIVE set)
        data_client: alpaca-py StockHistoricalDataClient
        config: {
            "DRY_RUN": bool,
            "DRIFT_TOLERANCE_PCT": float,
            "MAX_SINGLE_ORDER_PCT": float,     # safety cap, % of equity
            "MAX_TOTAL_NOTIONAL_BUFFER_PCT": float,  # how far over equity total orders may go
            "ALLOW_LIVE": bool,
        }
    """
    now = datetime.now(timezone.utc)
    result = {
        "execution_date": now.date().isoformat(),
        "execution_timestamp": now.isoformat(),
        "source_portfolio_date": target_portfolio.get("consolidation_date", ""),
        "dry_run": bool(config.get("DRY_RUN", True)),
        "account_equity_at_execution": None,
        "market_state": None,
        "order_submission_strategy": None,
        "cancelled_open_orders": [],
        "target_positions": [],
        "current_positions": {},
        "diff": [],
        "orders_submitted": [],
        "skipped_tickers": [],
        "data_quality_flags": [],
        "status": "unknown",
    }

    # ============ Step 0: refuse live unless explicitly allowed ============
    account = trading_client.get_account()
    # Both `account_number` prefix and the SDK's session config can indicate paper.
    # The cleanest check: the SDK was constructed with paper=True, which the workflow
    # script controls. We additionally inspect the trading_client.endpoint_url for safety.
    base_url = getattr(trading_client, "_base_url", None) or getattr(trading_client, "base_url", "")
    base_url_str = str(base_url).lower()
    is_paper_endpoint = "paper" in base_url_str
    if not is_paper_endpoint and not config.get("ALLOW_LIVE", False):
        result["status"] = "failed"
        result["data_quality_flags"].append(
            f"Refusing to run against non-paper endpoint ({base_url_str}). "
            f"Set ALLOW_LIVE=True only after deliberate review."
        )
        return result

    equity = float(account.equity)
    result["account_equity_at_execution"] = equity

    if equity <= 0:
        result["status"] = "failed"
        result["data_quality_flags"].append(f"Account equity is non-positive: {equity}")
        return result

    # ============ Step 1: pull current positions ============
    try:
        positions_raw = trading_client.get_all_positions()
    except Exception as e:
        result["status"] = "failed"
        result["data_quality_flags"].append(f"Failed to fetch current positions: {e}")
        return result

    current_positions = {p.symbol: float(p.market_value) for p in positions_raw}
    result["current_positions"] = current_positions

    # ============ Step 2: detect market state ============
    state_info = detect_market_state(trading_client)
    result["market_state"] = state_info["state"]

    if state_info["state"] == "no_imminent_session":
        result["status"] = "failed"
        result["data_quality_flags"].append(
            f"Market is closed and no trading session is imminent "
            f"(next open: {state_info['next_open']}). "
            f"Re-run when the next session is within ~72 hours."
        )
        return result

    result["order_submission_strategy"] = (
        "market_day" if state_info["state"] == "rth_open" else "market_day_queued_for_open"
    )

    # ============ Step 3: filter target positions for tradability ============
    raw_positions = (target_portfolio.get("portfolio") or {}).get("positions", [])
    if not raw_positions:
        result["status"] = "failed"
        result["data_quality_flags"].append(
            "consolidation_portfolio.json has no positions under portfolio.positions"
        )
        return result

    tradable_positions = []
    for p in raw_positions:
        ticker = p.get("ticker")
        if not ticker:
            result["skipped_tickers"].append({
                "ticker": None,
                "reason": "Position entry missing ticker field",
                "allocation_pct": p.get("allocation_pct"),
            })
            continue
        is_tradable, reason = check_tradability(trading_client, ticker)
        if not is_tradable:
            result["skipped_tickers"].append({
                "ticker": ticker,
                "reason": reason,
                "allocation_pct": p.get("allocation_pct"),
            })
            result["data_quality_flags"].append(
                f"Skipped {ticker} ({p.get('allocation_pct')}%): {reason}"
            )
            continue
        tradable_positions.append({
            "ticker": ticker,
            "allocation_pct": float(p["allocation_pct"]),
        })

    result["target_positions"] = tradable_positions

    if not tradable_positions:
        result["status"] = "failed"
        result["data_quality_flags"].append("No tradable positions remained after filtering")
        return result

    # ============ Step 4: cancel-on-rerun rule ============
    # If account has zero positions, this is effectively a fresh start — cancel any
    # leftover open orders. If account has positions, leave open orders alone (Stage 5
    # rebalance case: a swap order may be sitting in the queue legitimately).
    has_positions = len(current_positions) > 0
    if not has_positions:
        try:
            open_orders_req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
            open_orders = trading_client.get_orders(filter=open_orders_req)
        except Exception as e:
            result["data_quality_flags"].append(f"Could not list open orders: {e}")
            open_orders = []

        for o in open_orders:
            if config.get("DRY_RUN", True):
                result["cancelled_open_orders"].append({
                    "order_id": str(o.id),
                    "symbol": o.symbol,
                    "side": str(o.side),
                    "status_before": str(o.status),
                    "cancelled": False,
                    "dry_run": True,
                })
                continue
            try:
                trading_client.cancel_order_by_id(o.id)
                result["cancelled_open_orders"].append({
                    "order_id": str(o.id),
                    "symbol": o.symbol,
                    "side": str(o.side),
                    "status_before": str(o.status),
                    "cancelled": True,
                })
            except Exception as e:
                result["cancelled_open_orders"].append({
                    "order_id": str(o.id),
                    "symbol": o.symbol,
                    "side": str(o.side),
                    "status_before": str(o.status),
                    "cancelled": False,
                    "error": str(e),
                })
                result["data_quality_flags"].append(
                    f"Could not cancel open order {o.id} ({o.symbol}): {e}"
                )

    # ============ Step 5: compute diff ============
    drift_tol = float(config.get("DRIFT_TOLERANCE_PCT", 0.5))
    diffs = compute_diff(tradable_positions, current_positions, equity, drift_tol)
    result["diff"] = diffs

    # ============ Step 6: safety caps ============
    max_single_pct = float(config.get("MAX_SINGLE_ORDER_PCT", 25.0))
    max_single_dollars = (max_single_pct / 100.0) * equity
    over_cap = []
    for d in diffs:
        if d["action"] == "hold":
            continue
        if abs(d["diff_dollars"]) > max_single_dollars:
            over_cap.append(
                f"{d['ticker']} order ${abs(d['diff_dollars']):.2f} exceeds "
                f"max-single-order cap ${max_single_dollars:.2f} ({max_single_pct}% of equity)"
            )
    if over_cap:
        result["status"] = "failed"
        for msg in over_cap:
            result["data_quality_flags"].append(msg)
        return result

    total_buy_dollars = sum(d["diff_dollars"] for d in diffs if d["action"] == "buy")
    buffer_pct = float(config.get("MAX_TOTAL_NOTIONAL_BUFFER_PCT", 5.0))
    max_total = (1.0 + buffer_pct / 100.0) * equity
    if total_buy_dollars > max_total:
        result["status"] = "failed"
        result["data_quality_flags"].append(
            f"Total buy notional ${total_buy_dollars:.2f} exceeds equity ${equity:.2f} "
            f"by more than {buffer_pct}% buffer (max ${max_total:.2f})"
        )
        return result

    # ============ Step 7: build orders ============
    try:
        ordered_requests = build_orders(diffs, state_info["state"])
    except Exception as e:
        result["status"] = "failed"
        result["data_quality_flags"].append(f"Order construction failed: {e}")
        return result

    # ============ Step 8: submit (or dry-run) ============
    dry_run = bool(config.get("DRY_RUN", True))
    submission_errors = 0

    for ticker, order_req, dollar_amount, side_str in ordered_requests:
        record = {
            "ticker": ticker,
            "side": side_str,
            "dollar_amount": round(dollar_amount, 2),
            "time_in_force": str(order_req.time_in_force),
            "notional": getattr(order_req, "notional", None),
            "qty": getattr(order_req, "qty", None),
            "order_id": None,
            "submission_status": None,
            "submitted_at": None,
            "dry_run": dry_run,
            "error": None,
        }

        if dry_run:
            record["submission_status"] = "dry_run_skipped"
            result["orders_submitted"].append(record)
            continue

        try:
            order = trading_client.submit_order(order_data=order_req)
            record["order_id"] = str(order.id)
            record["submission_status"] = str(order.status)
            record["submitted_at"] = datetime.now(timezone.utc).isoformat()
        except Exception as e:
            record["submission_status"] = "submission_failed"
            record["error"] = str(e)
            submission_errors += 1
            result["data_quality_flags"].append(
                f"Failed to submit {side_str} order for {ticker}: {e}"
            )

        result["orders_submitted"].append(record)

    # ============ Step 9: final status ============
    if dry_run:
        result["status"] = "success_dry_run"
    elif submission_errors == 0:
        result["status"] = "success"
    elif submission_errors < len(ordered_requests):
        result["status"] = "partial"
    else:
        result["status"] = "failed"

    return result