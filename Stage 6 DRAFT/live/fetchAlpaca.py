"""
Live Alpaca pull: account, positions, daily portfolio history.

Every call falls back to the corresponding cache/alpaca_*.json on failure so
the rest of the Stage 6 run can still produce UI output (marked stale via
data_quality.json).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import config

logger = logging.getLogger(__name__)


# ============ Client construction ============

def _build_trading_client():
    from alpaca.trading.client import TradingClient
    api_key = os.getenv(config.ALPACA_API_KEY_ENV)
    secret = os.getenv(config.ALPACA_SECRET_KEY_ENV)
    if not api_key or not secret:
        raise RuntimeError(
            f"{config.ALPACA_API_KEY_ENV}/{config.ALPACA_SECRET_KEY_ENV} not set"
        )
    return TradingClient(api_key, secret, paper=config.ALPACA_PAPER)


# ============ Serialization helpers ============

def _to_dict(obj):
    """Best-effort: pydantic model -> dict, else dict, else str."""
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "dict"):
        return obj.dict()
    if isinstance(obj, list):
        return [_to_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    return obj


def _read_cache(path: str) -> Optional[dict]:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Could not parse cache %s: %s", path, e)
        return None


def _write_cache(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, path)


# ============ Individual pulls ============

def fetch_account(client) -> dict:
    raw = client.get_account()
    data = _to_dict(raw)
    return data


def fetch_positions(client) -> list:
    raw = client.get_all_positions()
    return [_to_dict(p) for p in raw]


def fetch_portfolio_history(client) -> dict:
    from alpaca.trading.requests import GetPortfolioHistoryRequest
    req = GetPortfolioHistoryRequest(
        period=config.PORTFOLIO_HISTORY_PERIOD,
        timeframe=config.PORTFOLIO_HISTORY_TIMEFRAME,
    )
    raw = client.get_portfolio_history(history_filter=req)
    return _to_dict(raw)


# ============ Orchestrator ============

def fetch_all() -> dict:
    """
    Returns:
        {
            "fetched_at": ISO timestamp,
            "account": {...} or None,
            "account_stale": bool,
            "positions": [...] or None,
            "positions_stale": bool,
            "portfolio_history": {...} or None,
            "portfolio_history_stale": bool,
            "errors": [...],
        }
    """
    now = datetime.now(timezone.utc).isoformat()
    result = {
        "fetched_at": now,
        "account": None,
        "account_stale": False,
        "positions": None,
        "positions_stale": False,
        "portfolio_history": None,
        "portfolio_history_stale": False,
        "errors": [],
    }

    try:
        client = _build_trading_client()
    except Exception as e:
        logger.warning("Could not build Alpaca client: %s — falling back to caches", e)
        result["errors"].append(f"client_build_failed: {e}")
        return _fall_back_all(result)

    # Account
    try:
        account = fetch_account(client)
        _write_cache(config.ALPACA_ACCOUNT_CACHE, {"fetched_at": now, "data": account})
        result["account"] = account
    except Exception as e:
        logger.warning("fetch_account failed: %s — using cache", e)
        result["errors"].append(f"account: {e}")
        cached = _read_cache(config.ALPACA_ACCOUNT_CACHE)
        result["account"] = (cached or {}).get("data")
        result["account_stale"] = True

    # Positions
    try:
        positions = fetch_positions(client)
        _write_cache(config.ALPACA_POSITIONS_CACHE, {"fetched_at": now, "data": positions})
        result["positions"] = positions
    except Exception as e:
        logger.warning("fetch_positions failed: %s — using cache", e)
        result["errors"].append(f"positions: {e}")
        cached = _read_cache(config.ALPACA_POSITIONS_CACHE)
        result["positions"] = (cached or {}).get("data")
        result["positions_stale"] = True

    # Portfolio history
    try:
        history = fetch_portfolio_history(client)
        _write_cache(
            config.ALPACA_PORTFOLIO_HISTORY_CACHE,
            {"fetched_at": now, "data": history},
        )
        result["portfolio_history"] = history
    except Exception as e:
        logger.warning("fetch_portfolio_history failed: %s — using cache", e)
        result["errors"].append(f"portfolio_history: {e}")
        cached = _read_cache(config.ALPACA_PORTFOLIO_HISTORY_CACHE)
        result["portfolio_history"] = (cached or {}).get("data")
        result["portfolio_history_stale"] = True

    return result


def _fall_back_all(result: dict) -> dict:
    """Used when no client could even be built — read all caches at once."""
    for key, cache_path in [
        ("account", config.ALPACA_ACCOUNT_CACHE),
        ("positions", config.ALPACA_POSITIONS_CACHE),
        ("portfolio_history", config.ALPACA_PORTFOLIO_HISTORY_CACHE),
    ]:
        cached = _read_cache(cache_path)
        result[key] = (cached or {}).get("data")
        result[f"{key}_stale"] = True
    return result
