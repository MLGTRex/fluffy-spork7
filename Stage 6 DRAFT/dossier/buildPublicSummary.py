"""
Public-facing summary file for the shareable UI.

Includes:
  - aggregated portfolio + benchmark performance,
  - normalised portfolio/benchmark value series (no dollar amounts),
  - a curated per-ticker positions array with thesis, candidate summary,
    invalidation triggers and live performance numbers.

Excludes operator/methodology detail (Stage 1 scores, Stage 2 debate text,
Stage 3 scenario narratives, valuation metrics, price targets, expected
return tables, prediction-log entries) — those stay admin-only.

Output: `Stage 6 DRAFT/output/public_summary.json`.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def build(
    performance_block: dict,
    portfolio_value_history: dict,
    benchmark_history: dict,
    portfolio_overview: dict,
    dossier_dir: str,
    snapshot_id: Optional[str],
    live_stale: bool,
) -> dict:
    """
    Args:
        performance_block:        from computePortfolioPerf.compute()["performance"]
        portfolio_value_history:  same module's "portfolio_value_history" block
        benchmark_history:        same module's "benchmark_history" block
        portfolio_overview:       output of buildPortfolioOverview.build()
        dossier_dir:              dir of per-ticker dossier JSON files
        snapshot_id:              current Stage 6 snapshot id, for the footer
        live_stale:               True if Alpaca live data couldn't be refreshed
                                  this run (so the UI can show a banner)
    """
    perf = performance_block or {}

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

    positions = _build_positions(portfolio_overview or {}, dossier_dir)

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
        "positions": positions,
    }


def _build_positions(portfolio_overview: dict, dossier_dir: str) -> list[dict]:
    """One row per overview position, joined to its per-ticker dossier."""
    rows = portfolio_overview.get("positions") or []
    out: list[dict] = []
    for p in rows:
        ticker = p.get("ticker")
        if not ticker:
            continue
        dossier = _load_dossier(dossier_dir, ticker)
        out.append(_assemble_position(p, dossier))
    return out


def _load_dossier(dossier_dir: str, ticker: str) -> dict:
    path = os.path.join(dossier_dir, f"{ticker}.json")
    if not os.path.isfile(path):
        logger.warning("Public summary: dossier missing for %s", ticker)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Public summary: could not parse dossier %s: %s", ticker, e)
        return {}


def _assemble_position(overview_row: dict, dossier: dict) -> dict:
    """Curated public per-position record. Dollar amounts kept; methodology not."""
    s3 = dossier.get("stage3_scenarios_and_valuation") or {}
    cs = dossier.get("stage4_candidate_summary") or {}
    return {
        "ticker": overview_row.get("ticker"),
        "company_name": dossier.get("company_name"),
        "sector": overview_row.get("sector"),
        "status": overview_row.get("status"),
        "target_allocation_pct": overview_row.get("target_allocation_pct"),
        "actual_allocation_pct": overview_row.get("actual_allocation_pct"),
        "current_price": overview_row.get("current_price"),
        "lastday_price": overview_row.get("lastday_price"),
        "unrealized_pl": overview_row.get("unrealized_pl"),
        "unrealized_plpc": overview_row.get("unrealized_plpc"),
        "entry_price": overview_row.get("entry_price"),
        "conviction": overview_row.get("conviction") or s3.get("conviction"),
        "thesis_summary": overview_row.get("thesis_summary") or s3.get("thesis_summary"),
        "key_invalidation_triggers": s3.get("key_invalidation_triggers"),
        "candidate_summary": _candidate_summary_block(cs),
    }


def _candidate_summary_block(cs: dict) -> Optional[dict]:
    if not cs or not cs.get("summary"):
        return None
    return {
        "summary": cs.get("summary"),
        "source_date": cs.get("source_date"),
        "analysis_date": cs.get("analysis_date"),
        "model": cs.get("model"),
        "error": cs.get("error"),
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
