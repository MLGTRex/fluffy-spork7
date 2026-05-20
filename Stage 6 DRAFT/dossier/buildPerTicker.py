"""
Per-ticker dossier — one JSON per ticker for the UI's per-ticker page.

Stitches Stage 1 (scores), Stage 3 (research + scenarios + valuation +
consolidation), Stage 4 (candidate summary + target portfolio entry), Alpaca
current position, and ledger history into a single file at
output/per_ticker_dossier/{TICKER}.json.
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
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _stage3_by_ticker(snapshot_dir: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in glob.glob(os.path.join(snapshot_dir, "stage3", "*_research.json")):
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


def _stage1_by_ticker(snapshot_dir: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    pattern = os.path.join(snapshot_dir, "stage1", "company_data", "*.json")
    for p in glob.glob(pattern):
        d = _load_json(p)
        if not d:
            continue
        ticker = d.get("ticker") or os.path.basename(p).replace(".json", "")
        out[ticker] = d
    return out


def _candidate_summaries_in_snapshot(snapshot_dir: str) -> tuple[dict[str, dict], Optional[dict]]:
    """
    Returns (summaries_by_ticker, summary_meta).

    summaries_by_ticker: {TICKER: {summary, source_date, error}}
    summary_meta:        the top-level analysis_date / model / counts, or None
                         when the file is missing.
    """
    path = os.path.join(snapshot_dir, "stage4", "candidate_summaries.json")
    raw = _load_json(path)
    if not raw:
        return {}, None
    summaries = raw.get("summaries") or {}
    meta = {
        "analysis_date": raw.get("analysis_date"),
        "model": raw.get("model"),
        "n_candidates": raw.get("n_candidates"),
        "n_summaries": raw.get("n_summaries"),
    }
    return summaries, meta


def _atomic_write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def build(
    snapshot_dir: Optional[str],
    snapshot_id: Optional[str],
    alpaca_bundle: dict,
    ledger: dict,
    portfolio_overview: dict,
) -> dict:
    """
    Writes one file per ticker into config.PER_TICKER_DOSSIER_DIR.

    Returns:
        {
            "as_of": ISO,
            "tickers_written": [...],
            "tickers_missing_stage3": [...],
            "tickers_missing_stage1": [...],
        }
    """
    config.ensure_stage6_dirs()
    now = datetime.now(timezone.utc).isoformat()
    written: list[str] = []
    missing_s3: list[str] = []
    missing_s1: list[str] = []

    if not snapshot_dir or not os.path.isdir(snapshot_dir):
        return {
            "as_of": now,
            "tickers_written": [],
            "tickers_missing_stage3": [],
            "tickers_missing_stage1": [],
            "warning": "no_snapshot_dir",
        }

    stage3 = _stage3_by_ticker(snapshot_dir)
    stage1 = _stage1_by_ticker(snapshot_dir)
    candidate_summaries, candidate_summary_meta = _candidate_summaries_in_snapshot(snapshot_dir)

    # Ticker universe: anything in current target, Alpaca, or ledger
    tickers: set[str] = set()
    for row in portfolio_overview.get("positions", []):
        if row.get("ticker"):
            tickers.add(row["ticker"])
    for p in alpaca_bundle.get("positions") or []:
        s = p.get("symbol")
        if s:
            tickers.add(s)
    for e in ledger.get("entries", []):
        if e.get("ticker"):
            tickers.add(e["ticker"])

    alpaca_by_ticker = {
        p.get("symbol"): p for p in (alpaca_bundle.get("positions") or [])
    }
    overview_by_ticker = {
        r["ticker"]: r for r in portfolio_overview.get("positions", [])
    }

    # Group ledger entries by ticker
    ledger_by_ticker: dict[str, list[dict]] = {}
    for e in ledger.get("entries", []):
        ledger_by_ticker.setdefault(e["ticker"], []).append(e)

    for ticker in sorted(tickers):
        s3 = stage3.get(ticker)
        s1 = stage1.get(ticker)
        if not s3:
            missing_s3.append(ticker)
        if not s1:
            missing_s1.append(ticker)
        alpaca = alpaca_by_ticker.get(ticker)
        overview_row = overview_by_ticker.get(ticker)

        dossier = _assemble_dossier(
            ticker=ticker,
            stage1=s1,
            stage3=s3,
            alpaca=alpaca,
            overview_row=overview_row,
            ledger_entries=ledger_by_ticker.get(ticker, []),
            candidate_summary=candidate_summaries.get(ticker),
            candidate_summary_meta=candidate_summary_meta,
            snapshot_id=snapshot_id,
            generated_at=now,
        )

        target_path = os.path.join(config.PER_TICKER_DOSSIER_DIR, f"{ticker}.json")
        _atomic_write_json(target_path, dossier)
        written.append(ticker)

    return {
        "as_of": now,
        "tickers_written": written,
        "tickers_missing_stage3": missing_s3,
        "tickers_missing_stage1": missing_s1,
    }


def _assemble_dossier(
    *,
    ticker: str,
    stage1: Optional[dict],
    stage3: Optional[dict],
    alpaca: Optional[dict],
    overview_row: Optional[dict],
    ledger_entries: list[dict],
    candidate_summary: Optional[dict],
    candidate_summary_meta: Optional[dict],
    snapshot_id: Optional[str],
    generated_at: str,
) -> dict:
    """One ticker's UI page in JSON."""
    s1 = stage1 or {}
    s3 = stage3 or {}
    cs = candidate_summary or {}

    return {
        "ticker": ticker,
        "generated_at": generated_at,
        "snapshot_id": snapshot_id,
        "company_name": s3.get("company_name") or s1.get("company_name"),

        "stage1_scores": {
            "financial_score": s1.get("financial_score"),
            "financial_subscores": s1.get("financial_subscores"),
            "financial_drivers": s1.get("financial_drivers"),
            "professional_score": s1.get("professional_score"),
            "professional_subscores": s1.get("professional_subscores"),
            "professional_drivers": s1.get("professional_drivers"),
            "news_sentiment_score": s1.get("news_sentiment_score"),
            "news_sentiment_subscores": s1.get("news_sentiment_subscores"),
            "news_sentiment_drivers": s1.get("news_sentiment_drivers"),
            "composite_score": s1.get("composite_score"),
            "composite_rank": s1.get("composite_rank"),
            "composite_metadata": s1.get("composite_metadata"),
            "qualified": s1.get("qualified"),
            "disqualifier_flags": s1.get("disqualifier_flags"),
        },

        "stage2_debate": {
            "finance_research_report": s3.get("finance_research_report"),
            "finance_research_report_date": s3.get("finance_research_report_date"),
            "news_research_report": s3.get("news_research_report"),
            "news_research_report_date": s3.get("news_research_report_date"),
            "environment_research_report": s3.get("environment_research_report"),
            "environment_research_report_date": s3.get("environment_research_report_date"),
            "bull_case": s3.get("bull_case"),
            "bull_case_date": s3.get("bull_case_date"),
            "bear_case": s3.get("bear_case"),
            "bear_case_date": s3.get("bear_case_date"),
            "bull_rebuttal": s3.get("bull_rebuttal"),
            "bull_rebuttal_date": s3.get("bull_rebuttal_date"),
            "bear_rebuttal": s3.get("bear_rebuttal"),
            "bear_rebuttal_date": s3.get("bear_rebuttal_date"),
            "synthesis": s3.get("synthesis"),
            "synthesis_date": s3.get("synthesis_date"),
            "synthesis_score": s3.get("synthesis_score"),
            "synthesis_categorical": s3.get("synthesis_categorical"),
            "synthesis_score_confidence": s3.get("synthesis_score_confidence"),
        },

        "stage3_scenarios_and_valuation": {
            "scenario_bull": s3.get("scenario_bull"),
            "scenario_bear": s3.get("scenario_bear"),
            "scenario_base_initial": s3.get("scenario_base_initial"),
            "scenario_bull_rebuttal": s3.get("scenario_bull_rebuttal"),
            "scenario_bear_rebuttal": s3.get("scenario_bear_rebuttal"),
            "scenario_base_final": s3.get("scenario_base_final"),
            "valuation_metrics": s3.get("valuation_metrics"),
            "consolidation": s3.get("consolidation"),
            "consolidation_date": s3.get("consolidation_date"),
            "current_price_at_consolidation": s3.get("current_price"),
            "current_price_at_consolidation_date": s3.get("current_price_date"),
            "price_targets": {
                "bull": {h: s3.get(f"price_target_bull_{h}") for h in ("1m", "3m", "6m", "12m")},
                "base": {h: s3.get(f"price_target_base_{h}") for h in ("1m", "3m", "6m", "12m")},
                "bear": {h: s3.get(f"price_target_bear_{h}") for h in ("1m", "3m", "6m", "12m")},
            },
            "scenario_probabilities": {
                "bull": s3.get("scenario_probability_bull"),
                "base": s3.get("scenario_probability_base"),
                "bear": s3.get("scenario_probability_bear"),
            },
            "conviction": s3.get("conviction"),
            "thesis_summary": s3.get("thesis_summary"),
            "key_invalidation_triggers": s3.get("key_invalidation_triggers"),
            "expected_returns": {h: s3.get(f"expected_return_{h}") for h in ("1m", "3m", "6m", "12m")},
            "upside_return_12m": s3.get("upside_return_12m"),
            "base_return_12m": s3.get("base_return_12m"),
            "downside_return_12m": s3.get("downside_return_12m"),
        },

        "stage4_candidate_summary": (
            {
                "summary": cs.get("summary"),
                "source_date": cs.get("source_date"),
                "error": cs.get("error"),
                "analysis_date": (candidate_summary_meta or {}).get("analysis_date"),
                "model": (candidate_summary_meta or {}).get("model"),
            }
            if candidate_summary
            else None
        ),

        "stage4_portfolio_status": (
            {
                "in_current_target": overview_row.get("status") in ("held", "pending_buy"),
                "status": overview_row.get("status"),
                "target_allocation_pct": overview_row.get("target_allocation_pct"),
                "actual_allocation_pct": overview_row.get("actual_allocation_pct"),
                "drift_pct": overview_row.get("drift_pct"),
                "entry_date_pipeline": overview_row.get("entry_date_pipeline"),
                "entry_price_pipeline": overview_row.get("entry_price_pipeline"),
            }
            if overview_row
            else {"in_current_target": False, "status": "absent"}
        ),

        "live_position": (
            {
                "qty": _flt(alpaca.get("qty")),
                "market_value": _flt(alpaca.get("market_value")),
                "cost_basis": _flt(alpaca.get("cost_basis")),
                "current_price": _flt(alpaca.get("current_price")),
                "lastday_price": _flt(alpaca.get("lastday_price")),
                "unrealized_pl": _flt(alpaca.get("unrealized_pl")),
                "unrealized_plpc": _flt(alpaca.get("unrealized_plpc")),
            }
            if alpaca
            else None
        ),

        "history": ledger_entries,
    }


def _flt(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
