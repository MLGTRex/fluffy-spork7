"""
Public-facing summary file for the shareable UI.

Strict subset of Stage 6's data: aggregated performance metrics + the
normalised portfolio/benchmark value series. Explicitly excludes any
ticker, allocation, thesis text, prediction log entry, account dollar
amount, sector exposure or pipeline count.

Output: `Stage 6 DRAFT/output/public_summary.json`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def build(
    performance_block: dict,
    portfolio_value_history: dict,
    benchmark_history: dict,
    snapshot_id: Optional[str],
    live_stale: bool,
) -> dict:
    """
    Args:
        performance_block:        from computePortfolioPerf.compute()["performance"]
        portfolio_value_history:  same module's "portfolio_value_history" block
        benchmark_history:        same module's "benchmark_history" block
        snapshot_id:              current Stage 6 snapshot id, for the footer
        live_stale:               True if Alpaca live data couldn't be refreshed
                                  this run (so the UI can show a banner)

    Returns the dict to write as public_summary.json. Note: the
    portfolio_value_history.series and benchmark_history.series already
    contain a "normalised" field (and only that and the date are emitted).
    Raw dollar amounts in `value` are stripped here for safety.
    """
    perf = performance_block or {}

    # Extra "total" against SPY computed from the underlying series — keeps the
    # public UI honest about excess return without recomputing on the client.
    spy_total_return_pct = _series_total_return(benchmark_history)
    portfolio_total_return_pct = perf.get("total_return_pct")
    if portfolio_total_return_pct is None:
        portfolio_total_return_pct = _series_total_return(portfolio_value_history)

    summary_perf = {
        "since_inception_days": perf.get("since_inception_days"),
        "total_return_pct": portfolio_total_return_pct,
        "spy_total_return_pct": spy_total_return_pct,
        "max_drawdown_pct": perf.get("max_drawdown_pct"),
        "current_drawdown_pct": perf.get("current_drawdown_pct"),
        "rolling_30d_sharpe": perf.get("rolling_30d_sharpe"),
        "mtd_return": perf.get("mtd_return"),
        "ytd_return": perf.get("ytd_return"),
        "m1_return": perf.get("m1_return"),
        "m3_return": perf.get("m3_return"),
        "m6_return": perf.get("m6_return"),
        "m12_return": perf.get("m12_return"),
        "spy_mtd": perf.get("spy_mtd"),
        "spy_ytd": perf.get("spy_ytd"),
        "spy_m1": perf.get("spy_m1"),
        "spy_m3": perf.get("spy_m3"),
        "spy_m6": perf.get("spy_m6"),
        "spy_m12": perf.get("spy_m12"),
    }

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "snapshot_id": snapshot_id,
        "live_stale": bool(live_stale),
        "performance": summary_perf,
        "value_history": _safe_series(portfolio_value_history),
        "benchmark_history": _safe_series(
            benchmark_history,
            attach_symbol=(benchmark_history or {}).get("symbol") or "SPY",
        ),
    }


def _safe_series(history_block: Optional[dict], attach_symbol: Optional[str] = None) -> list[dict]:
    """Returns [{date, normalised, symbol?}, ...] — drops the raw `value` field."""
    series = ((history_block or {}).get("series")) or []
    out: list[dict] = []
    for entry in series:
        rec = {
            "date": entry.get("date"),
            "normalised": entry.get("normalised"),
        }
        if attach_symbol:
            rec["symbol"] = attach_symbol
        out.append(rec)
    return out


def _series_total_return(history_block: Optional[dict]) -> Optional[float]:
    """Compute total return from the normalised series."""
    series = ((history_block or {}).get("series")) or []
    if not series:
        return None
    first = series[0].get("normalised")
    last = series[-1].get("normalised")
    if first in (None, 0) or last is None:
        return None
    try:
        return (last / first) - 1.0
    except Exception:
        return None
