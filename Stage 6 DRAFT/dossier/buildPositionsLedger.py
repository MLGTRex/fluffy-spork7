"""
Positions ledger — flat list of every position ever held by the pipeline.

Sourced from the chronological sequence of Stage 4 portfolio history archives
present in the most recent snapshot. One ledger row per (ticker, contiguous
holding period). When a ticker disappears between two consecutive archives,
the row is closed with exit_date == later archive's reconciled_date.
"""

from __future__ import annotations

import glob
import json
import logging
import os
from datetime import datetime
from typing import Optional

import config

logger = logging.getLogger(__name__)


def _load_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _archives_in_snapshot(snapshot_dir: str) -> list[tuple[str, dict]]:
    """Returns [(archive_id, portfolio_dict), ...] in chronological order."""
    history_dir = os.path.join(snapshot_dir, "stage4", "portfolio_history")
    if not os.path.isdir(history_dir):
        return []
    out: list[tuple[str, dict]] = []
    for p in sorted(glob.glob(os.path.join(history_dir, "*.json"))):
        archive_id = os.path.basename(p).replace(".json", "")
        d = _load_json(p)
        if d:
            out.append((archive_id, d))
    return out


def _positions_by_ticker(portfolio: dict) -> dict[str, dict]:
    inner = (portfolio.get("portfolio") or {}).get("positions") or []
    return {p["ticker"]: p for p in inner if p.get("ticker")}


def _archive_date(portfolio: dict, archive_id: str) -> str:
    # Prefer the reconciled date when present, else consolidation_date, else parse
    # from the filename's UTC timestamp prefix.
    return (
        portfolio.get("reconciled_date")
        or portfolio.get("consolidation_date")
        or _date_from_archive_id(archive_id)
    )


def _date_from_archive_id(archive_id: str) -> Optional[str]:
    # portfolio_20260519T061450Z -> 2026-05-19
    import re
    m = re.search(r"(\d{4})(\d{2})(\d{2})T", archive_id)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def build(snapshot_dir: Optional[str]) -> dict:
    """
    Returns:
        {
            "as_of": ISO,
            "source_snapshot_dir": ...,
            "entries": [
                {ticker, entry_date, entry_price, exit_date|null, exit_price|null,
                 holding_period_days|null, sector, allocation_pct_at_entry,
                 entry_archive_id, exit_archive_id|null}
            ],
            "warnings": []
        }
    """
    now_iso = datetime.utcnow().isoformat() + "Z"
    result = {
        "as_of": now_iso,
        "source_snapshot_dir": snapshot_dir,
        "entries": [],
        "warnings": [],
    }
    if not snapshot_dir or not os.path.isdir(snapshot_dir):
        result["warnings"].append("no_snapshot_dir")
        return result

    archives = _archives_in_snapshot(snapshot_dir)
    if not archives:
        result["warnings"].append("no_portfolio_history_in_snapshot")
        return result

    # open_rows tracks tickers currently 'open' (latest archive has them):
    #   ticker -> dict (in-progress ledger row, to be closed when the ticker disappears)
    open_rows: dict[str, dict] = {}
    completed: list[dict] = []

    prev_tickers: set[str] = set()
    for archive_id, portfolio in archives:
        positions = _positions_by_ticker(portfolio)
        current_tickers = set(positions.keys())
        archive_date = _archive_date(portfolio, archive_id)

        # Open any new tickers
        for ticker in current_tickers - prev_tickers:
            pos = positions[ticker]
            open_rows[ticker] = {
                "ticker": ticker,
                "entry_date": pos.get("entry_date") or archive_date,
                "entry_price": pos.get("entry_price"),
                "exit_date": None,
                "exit_price": None,
                "holding_period_days": None,
                "sector": pos.get("sector"),
                "allocation_pct_at_entry": pos.get("allocation_pct"),
                "entry_archive_id": archive_id,
                "exit_archive_id": None,
            }

        # Close any tickers that disappeared
        for ticker in prev_tickers - current_tickers:
            row = open_rows.pop(ticker, None)
            if not row:
                continue
            row["exit_date"] = archive_date
            row["exit_archive_id"] = archive_id
            row["holding_period_days"] = _days_between(row["entry_date"], archive_date)
            completed.append(row)

        prev_tickers = current_tickers

    # Anything still in open_rows is still held — emit with exit_date=None
    completed.extend(open_rows.values())

    # Stable sort: entry_date then ticker
    completed.sort(key=lambda r: (r.get("entry_date") or "", r["ticker"]))
    result["entries"] = completed
    return result


def _days_between(start_iso: Optional[str], end_iso: Optional[str]) -> Optional[int]:
    if not start_iso or not end_iso:
        return None
    try:
        s = datetime.fromisoformat(start_iso).date()
        e = datetime.fromisoformat(end_iso).date()
        return (e - s).days
    except Exception:
        return None
