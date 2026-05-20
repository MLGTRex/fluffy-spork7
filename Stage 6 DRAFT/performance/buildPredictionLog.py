"""
Internal prediction log builder.

Stage 5's prediction_log.jsonl is no longer maintained, so Stage 6 reconstructs
its own from the data it already owns:
    - backups/snapshots/*/stage4/consolidation_portfolio.json   (latest target)
    - backups/snapshots/*/stage4/portfolio_history/*.json        (every prior
                                                                  reconciliation)
    - backups/snapshots/*/stage3/{TICKER}_research.json          (the forecast
                                                                  current at that
                                                                  snapshot time)

Each (ticker, entry_date, entry_price) triple seen in any snapshot becomes one
log entry, joined against the matching Stage 3 dossier. The log is append-only;
deleting the file forces a full rebuild on the next run.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

import config

logger = logging.getLogger(__name__)


# ============ Snapshot walking ============

def _list_snapshots() -> list[str]:
    if not os.path.isdir(config.SNAPSHOTS_DIR):
        return []
    return sorted(
        d for d in os.listdir(config.SNAPSHOTS_DIR)
        if os.path.isdir(os.path.join(config.SNAPSHOTS_DIR, d))
    )


def _load_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Could not parse %s: %s", path, e)
        return None


def _portfolios_in_snapshot(snapshot_dir: str) -> list[dict]:
    """Latest consolidation_portfolio.json + every portfolio_history file."""
    out: list[dict] = []
    consol = _load_json(os.path.join(snapshot_dir, "stage4", "consolidation_portfolio.json"))
    if consol:
        out.append(consol)
    history_dir = os.path.join(snapshot_dir, "stage4", "portfolio_history")
    if os.path.isdir(history_dir):
        for p in sorted(glob.glob(os.path.join(history_dir, "*.json"))):
            d = _load_json(p)
            if d:
                out.append(d)
    return out


def _index_stage3_by_ticker(snapshot_dir: str) -> dict[str, dict]:
    """Build {TICKER: stage3_dossier} for a snapshot's stage3/ section."""
    out: dict[str, dict] = {}
    pattern = os.path.join(snapshot_dir, "stage3", "*_research.json")
    for p in glob.glob(pattern):
        d = _load_json(p)
        if not d:
            continue
        ticker = d.get("ticker")
        if not ticker:
            # Fall back to filename suffix: ..._{TICKER}_research.json
            base = os.path.basename(p)
            m = re.match(r".+_([A-Z][A-Z0-9.\-]*)_research\.json$", base)
            if m:
                ticker = m.group(1)
        if ticker:
            out[ticker] = d
    return out


# ============ Existing log loading ============

def _load_existing_log() -> tuple[list[dict], set[str]]:
    if not os.path.isfile(config.PREDICTION_LOG_PATH):
        return [], set()
    entries: list[dict] = []
    ids: set[str] = set()
    with open(config.PREDICTION_LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            entries.append(entry)
            pid = entry.get("prediction_id")
            if pid:
                ids.add(pid)
    return entries, ids


# ============ Prediction id ============

def _prediction_id(ticker: str, entry_date: str, snapshot_id: str) -> str:
    return f"{ticker}_{entry_date}_{snapshot_id}"


def _horizon_target_dates(entry_date_str: str) -> dict:
    try:
        d = datetime.fromisoformat(entry_date_str).date()
    except Exception:
        return {"1m": None, "3m": None, "6m": None, "12m": None}
    return {
        "1m": (d + timedelta(days=30)).isoformat(),
        "3m": (d + timedelta(days=91)).isoformat(),
        "6m": (d + timedelta(days=182)).isoformat(),
        "12m": (d + timedelta(days=365)).isoformat(),
    }


# ============ Entry construction ============

def _build_entry(
    ticker: str,
    entry_date: str,
    entry_price,
    snapshot_id: str,
    stage3: Optional[dict],
    entry_source: str,
    logged_at: str,
) -> Optional[dict]:
    if not entry_date:
        return None
    if not stage3:
        # Without the Stage 3 dossier we have no forecast to log — skip
        # gracefully and let the caller report a warning.
        return None
    entry = {
        "prediction_id": _prediction_id(ticker, entry_date, snapshot_id),
        "ticker": ticker,
        "logged_at": logged_at,
        "forecast_snapshot_id": snapshot_id,
        "entry_date": entry_date,
        "entry_price": entry_price,
        "entry_source": entry_source,
        "horizon_target_dates": _horizon_target_dates(entry_date),
        "price_targets": {
            "bull": {
                "1m": stage3.get("price_target_bull_1m"),
                "3m": stage3.get("price_target_bull_3m"),
                "6m": stage3.get("price_target_bull_6m"),
                "12m": stage3.get("price_target_bull_12m"),
            },
            "base": {
                "1m": stage3.get("price_target_base_1m"),
                "3m": stage3.get("price_target_base_3m"),
                "6m": stage3.get("price_target_base_6m"),
                "12m": stage3.get("price_target_base_12m"),
            },
            "bear": {
                "1m": stage3.get("price_target_bear_1m"),
                "3m": stage3.get("price_target_bear_3m"),
                "6m": stage3.get("price_target_bear_6m"),
                "12m": stage3.get("price_target_bear_12m"),
            },
        },
        "expected_returns": {
            "1m": stage3.get("expected_return_1m"),
            "3m": stage3.get("expected_return_3m"),
            "6m": stage3.get("expected_return_6m"),
            "12m": stage3.get("expected_return_12m"),
        },
        "scenario_probabilities": {
            "bull": stage3.get("scenario_probability_bull"),
            "base": stage3.get("scenario_probability_base"),
            "bear": stage3.get("scenario_probability_bear"),
        },
        "conviction": stage3.get("conviction"),
        "thesis_summary": stage3.get("thesis_summary"),
        "key_invalidation_triggers": stage3.get("key_invalidation_triggers"),
    }
    return entry


# ============ Entry point ============

def build() -> dict:
    """
    Walks all snapshots in chronological order, emits new prediction log
    entries for any (ticker, entry_date) seen in a snapshot's portfolio files
    that isn't already in the log.

    Returns:
        {
            "log_path": absolute path,
            "entries_total": int,
            "entries_appended": int,
            "skipped_missing_stage3": [{"ticker", "entry_date", "snapshot_id"}],
            "snapshots_visited": int,
        }
    """
    config.ensure_stage6_dirs()
    existing_entries, existing_ids = _load_existing_log()
    snapshots = _list_snapshots()
    appended: list[dict] = []
    skipped: list[dict] = []
    logged_at = datetime.now(timezone.utc).isoformat()

    for snapshot_id in snapshots:
        snapshot_dir = os.path.join(config.SNAPSHOTS_DIR, snapshot_id)
        portfolios = _portfolios_in_snapshot(snapshot_dir)
        if not portfolios:
            continue
        stage3_by_ticker = _index_stage3_by_ticker(snapshot_dir)

        for portfolio in portfolios:
            inner = (portfolio.get("portfolio") or {}).get("positions") or []
            # Distinguish the latest target from a reconciled snapshot for
            # the entry_source label.
            is_reconciled = bool(portfolio.get("reconciled_date"))
            for pos in inner:
                ticker = pos.get("ticker")
                entry_date = pos.get("entry_date")
                entry_price = pos.get("entry_price")
                if not ticker or not entry_date:
                    continue
                pid = _prediction_id(ticker, entry_date, snapshot_id)
                if pid in existing_ids:
                    continue
                stage3 = stage3_by_ticker.get(ticker)
                entry = _build_entry(
                    ticker=ticker,
                    entry_date=entry_date,
                    entry_price=entry_price,
                    snapshot_id=snapshot_id,
                    stage3=stage3,
                    entry_source=(
                        "reconciliation_hold" if is_reconciled else "consolidation"
                    ),
                    logged_at=logged_at,
                )
                if entry is None:
                    skipped.append({
                        "ticker": ticker,
                        "entry_date": entry_date,
                        "snapshot_id": snapshot_id,
                        "reason": "no_stage3_dossier" if not stage3 else "no_entry_date",
                    })
                    continue
                appended.append(entry)
                existing_ids.add(pid)

    if appended:
        os.makedirs(os.path.dirname(config.PREDICTION_LOG_PATH), exist_ok=True)
        with open(config.PREDICTION_LOG_PATH, "a", encoding="utf-8") as f:
            for e in appended:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        logger.info(
            "Appended %d new prediction log entries (skipped %d)",
            len(appended), len(skipped),
        )

    return {
        "log_path": config.PREDICTION_LOG_PATH,
        "entries_total": len(existing_entries) + len(appended),
        "entries_appended": len(appended),
        "skipped_missing_stage3": skipped,
        "snapshots_visited": len(snapshots),
    }


def load_all_entries() -> list[dict]:
    entries, _ = _load_existing_log()
    return entries
