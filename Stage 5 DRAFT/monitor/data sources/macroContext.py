"""
Macro context fetcher — market-wide data for the Stage 5 monitor.

Fetches current and historical prices for:
  - Broad market index (SPY)
  - Sector ETFs (XLV, XLK, XLF, XLY, XLP, XLI, XLE, XLB, XLU, XLRE, XLC)
  - Volatility index (VIX)
  - Australian broad-market fallback for ASX names (IOZ.AX)

Used for:
  - Input to Gate 0 (mechanical filter): compares ticker move against
    expected move from market + sector beta
  - Context for LLM Call 1: "today's market was down 3.5%, healthcare sector
    was down 5%" provided as neutral data, not framing

Macro indicators are NOT triggers in themselves. Macro moves don't fire the
LLM gate; ticker-specific moves do, and macro context informs the
investigation.

Designed to be:
  - Independent: standalone via CLI
  - Reusable: importable by orchestrator
  - Once-per-run: fetches a fixed set of symbols regardless of how many
    tickers the monitor is watching. No per-ticker work.

Usage:
    from macroContext import fetch_macro_context
    result = fetch_macro_context()
    if result["status"] == "ok":
        spy_move = result["indicators"]["SPY"]["daily_change_pct"]

CLI:
    python3 macroContext.py
    python3 macroContext.py --lookback-days 120
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone


# ============ CONFIGURATION ============

DEFAULT_LOOKBACK_DAYS = 90
RETRY_DELAYS_SECONDS = [2, 5, 15]

# Symbols to fetch. Keys are stable internal labels; values are yfinance
# ticker symbols. Adding/removing here is the supported way to extend.
MACRO_SYMBOLS = {
    # Broad markets
    "SPY": "SPY",            # S&P 500 ETF
    "IOZ.AX": "IOZ.AX",      # ASX 200 ETF (fallback for ASX tickers)
    "VIX": "^VIX",           # Volatility index

    # Sector ETFs (SPDR sector series)
    "XLV": "XLV",   # Healthcare
    "XLK": "XLK",   # Technology
    "XLF": "XLF",   # Financials
    "XLY": "XLY",   # Consumer Discretionary
    "XLP": "XLP",   # Consumer Staples
    "XLI": "XLI",   # Industrials
    "XLE": "XLE",   # Energy
    "XLB": "XLB",   # Materials
    "XLU": "XLU",   # Utilities
    "XLRE": "XLRE", # Real Estate
    "XLC": "XLC",   # Communication Services
}


# ============ LOGGER ============

_logger = logging.getLogger("monitor.macroContext")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)


# ============ RESULT SHAPE ============

def _empty_indicator(label: str) -> dict:
    return {
        "label": label,
        "current_price": None,
        "previous_close": None,
        "daily_change_pct": None,
        "daily_change_abs": None,
        "historical": {
            "daily_closes": [],  # [{"date": "YYYY-MM-DD", "close": float}, ...]
            "lookback_days_received": 0,
        },
        "data_quality_flags": [],
    }


def _empty_result(symbols: dict, lookback_days: int) -> dict:
    return {
        "data_source": "macroContext",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "status": "unknown",
        "lookback_days_requested": lookback_days,
        "indicators": {label: _empty_indicator(label) for label in symbols},
        "data_quality_flags": [],
    }


# ============ YFINANCE FETCHING ============

def _import_yfinance():
    try:
        import yfinance as yf
        return yf
    except ImportError as e:
        raise ImportError(
            "yfinance is required for macroContext but is not installed. "
            "Install with: pip install yfinance"
        ) from e


def _fetch_one_indicator(yf_module, label: str, symbol: str,
                          lookback_days: int) -> dict:
    """
    Fetch one indicator's price history and current price.

    Returns a populated indicator dict (matching _empty_indicator shape).
    Failure surfaces in data_quality_flags; we still return the dict.
    """
    indicator = _empty_indicator(label)

    try:
        ticker_obj = yf_module.Ticker(symbol)
    except Exception as e:
        indicator["data_quality_flags"].append(
            f"Could not create yfinance Ticker for {symbol}: {e}"
        )
        return indicator

    # Daily history with retries
    period_str = f"{lookback_days}d"
    last_error = None
    daily_bars = []
    for attempt_idx, delay in enumerate([0] + RETRY_DELAYS_SECONDS):
        if delay > 0:
            time.sleep(delay)
        try:
            hist = ticker_obj.history(period=period_str, interval="1d",
                                       auto_adjust=False, raise_errors=True)
        except Exception as e:
            last_error = f"{symbol} daily fetch raised: {e}"
            _logger.warning(
                f"{label} daily fetch attempt {attempt_idx + 1} failed: {e}"
            )
            continue

        if hist.empty:
            last_error = f"{symbol} returned empty history"
            _logger.warning(f"{label} daily empty on attempt {attempt_idx + 1}")
            continue

        for date_idx, row in hist.iterrows():
            try:
                close_val = row["Close"]
                if close_val != close_val:  # NaN
                    continue
                daily_bars.append({
                    "date": date_idx.strftime("%Y-%m-%d"),
                    "close": float(close_val),
                })
            except (ValueError, KeyError):
                continue
        break  # success

    indicator["historical"]["daily_closes"] = daily_bars
    indicator["historical"]["lookback_days_received"] = len(daily_bars)

    if not daily_bars:
        indicator["data_quality_flags"].append(
            last_error or f"{symbol}: no daily data after all retries"
        )
        return indicator

    # Determine previous close and current price
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    last_bar = daily_bars[-1]
    if last_bar["date"] == today_str:
        # Today's bar present (post-close): previous close is the one before
        indicator["current_price"] = last_bar["close"]
        if len(daily_bars) >= 2:
            indicator["previous_close"] = daily_bars[-2]["close"]
    else:
        # Today's bar not present: most recent bar is the previous close
        indicator["previous_close"] = last_bar["close"]
        # Need to fetch current price separately (live, not from daily history)
        # Try intraday or fast_info
        try:
            intra = ticker_obj.history(period="1d", interval="5m",
                                        auto_adjust=False, raise_errors=False)
            if not intra.empty:
                # Last intraday bar's close = current
                last_intra = intra.iloc[-1]
                last_close = last_intra["Close"]
                if last_close == last_close:  # not NaN
                    indicator["current_price"] = float(last_close)
        except Exception as e:
            _logger.debug(f"{label} intraday fetch failed: {e}")

        if indicator["current_price"] is None:
            # Fall back to fast_info
            try:
                fast = ticker_obj.fast_info
                last_price = getattr(fast, "last_price", None)
                if last_price is not None and isinstance(last_price, (int, float)) and last_price > 0:
                    indicator["current_price"] = float(last_price)
            except Exception as e:
                _logger.debug(f"{label} fast_info failed: {e}")

    # Compute daily change
    if (indicator["current_price"] is not None
            and indicator["previous_close"] is not None
            and indicator["previous_close"] != 0):
        cur = indicator["current_price"]
        prev = indicator["previous_close"]
        indicator["daily_change_abs"] = cur - prev
        indicator["daily_change_pct"] = ((cur - prev) / prev) * 100.0

    if indicator["current_price"] is None:
        indicator["data_quality_flags"].append(
            f"{symbol}: could not determine current price"
        )

    return indicator


# ============ MAIN FETCH ============

def fetch_macro_context(symbols: dict = None,
                        lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> dict:
    """
    Fetch macro context for all symbols.

    Args:
        symbols: dict of {label: yfinance_symbol}. Defaults to MACRO_SYMBOLS.
        lookback_days: how many days of daily history per indicator.

    Returns: a result dict (see module docstring for shape).

    Never raises. Per-symbol failures appear in indicator-level data quality
    flags. Overall status is 'ok' if all symbols fetched, 'partial' if some
    fetched, 'fetch_failed' if none.
    """
    if symbols is None:
        symbols = MACRO_SYMBOLS

    result = _empty_result(symbols, lookback_days)

    try:
        yf = _import_yfinance()
    except ImportError as e:
        result["status"] = "fetch_failed"
        result["data_quality_flags"].append(str(e))
        return result

    succeeded = 0
    failed = 0
    for label, symbol in symbols.items():
        indicator = _fetch_one_indicator(yf, label, symbol, lookback_days)
        result["indicators"][label] = indicator
        if indicator["current_price"] is not None:
            succeeded += 1
        else:
            failed += 1

    if succeeded == len(symbols):
        result["status"] = "ok"
    elif succeeded > 0:
        result["status"] = "partial"
        result["data_quality_flags"].append(
            f"Partial fetch: {succeeded}/{len(symbols)} indicators succeeded, {failed} failed"
        )
    else:
        result["status"] = "fetch_failed"
        result["data_quality_flags"].append(
            f"All {len(symbols)} indicator fetches failed"
        )

    return result


# ============ CLI ============

def _cli():
    parser = argparse.ArgumentParser(description="Fetch macro context indicators")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--brief", action="store_true",
                        help="Omit per-indicator historical bars from output")
    args = parser.parse_args()

    result = fetch_macro_context(lookback_days=args.lookback_days)

    if args.brief:
        for label, ind in result["indicators"].items():
            n = len(ind["historical"]["daily_closes"])
            ind["historical"]["daily_closes"] = f"<{n} bars omitted>"

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["status"] in ("ok", "partial") else 1


if __name__ == "__main__":
    sys.exit(_cli())