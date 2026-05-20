"""
Two indices for the UI history pages:

  snapshots_index.json  newest-first list of every Stage 6 snapshot, with the
                       upstream anchors and change-summary already in each
                       snapshot's manifest.

  runs_index.json       timeline of upstream pipeline events (consolidations,
                       reconciliations, executions, monitor runs) discovered
                       from the newest snapshot — lets the UI render
                       'what happened on this day'.
"""

from __future__ import annotations

import glob
import json
import logging
import os
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


def _list_snapshots() -> list[str]:
    if not os.path.isdir(config.SNAPSHOTS_DIR):
        return []
    return sorted(
        d for d in os.listdir(config.SNAPSHOTS_DIR)
        if os.path.isdir(os.path.join(config.SNAPSHOTS_DIR, d))
    )


def build_snapshots_index() -> dict:
    snapshots = _list_snapshots()
    entries: list[dict] = []
    for sid in snapshots:
        manifest = _load_json(
            os.path.join(config.SNAPSHOTS_DIR, sid, "manifest.json")
        ) or {}
        entries.append({
            "snapshot_id": sid,
            "created_at": manifest.get("created_at"),
            "prior_snapshot_id": manifest.get("prior_snapshot_id"),
            "tracked_file_count": manifest.get("tracked_file_count"),
            "changes_summary": _summarise_changes(
                manifest.get("changes_since_prior") or {}
            ),
            "upstream_anchors": manifest.get("upstream_anchors") or {},
        })
    entries.sort(key=lambda e: e["snapshot_id"], reverse=True)
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "snapshot_count": len(entries),
        "snapshots": entries,
    }


def _summarise_changes(changes: dict) -> dict:
    return {
        "added": len(changes.get("added") or []),
        "modified": len(changes.get("modified") or []),
        "removed": len(changes.get("removed") or []),
    }


# ============ runs_index ============

def build_runs_index(newest_snapshot_dir: Optional[str]) -> dict:
    """
    Returns:
        {
            "as_of": ISO,
            "events": [
                {date, type, ...metadata}
            ]
        }
    """
    now = datetime.now(timezone.utc).isoformat()
    out: dict = {"as_of": now, "events": []}
    if not newest_snapshot_dir or not os.path.isdir(newest_snapshot_dir):
        return out

    events: list[dict] = []

    # Stage 1 universe rebuild
    universe = _load_json(os.path.join(newest_snapshot_dir, "stage1", "universe.json"))
    if universe and universe.get("fetched_date"):
        events.append({
            "date": universe["fetched_date"],
            "type": "stage1_universe",
            "company_count": universe.get("company_count"),
            "source": universe.get("source"),
        })

    # Stage 4 consolidations + reconciliations (current + history)
    consol_now = _load_json(
        os.path.join(newest_snapshot_dir, "stage4", "consolidation_portfolio.json")
    )
    if consol_now:
        if consol_now.get("consolidation_date"):
            events.append({
                "date": consol_now["consolidation_date"],
                "type": "stage4_consolidation",
                "status": consol_now.get("status"),
                "positions_count": len(
                    (consol_now.get("portfolio") or {}).get("positions") or []
                ),
                "source": "current_target",
            })
        if consol_now.get("reconciled_date"):
            events.append({
                "date": consol_now["reconciled_date"],
                "type": "stage4_reconciliation",
                "status": (consol_now.get("reconciliation") or {}).get("status"),
                "source": "current_target",
            })

    history_dir = os.path.join(newest_snapshot_dir, "stage4", "portfolio_history")
    if os.path.isdir(history_dir):
        for p in sorted(glob.glob(os.path.join(history_dir, "*.json"))):
            archive = _load_json(p) or {}
            archive_id = os.path.basename(p).replace(".json", "")
            consol_date = archive.get("consolidation_date")
            recon_date = archive.get("reconciled_date")
            if consol_date:
                events.append({
                    "date": consol_date,
                    "type": "stage4_consolidation",
                    "status": archive.get("status"),
                    "positions_count": len(
                        (archive.get("portfolio") or {}).get("positions") or []
                    ),
                    "source": "history",
                    "archive_id": archive_id,
                })
            if recon_date:
                events.append({
                    "date": recon_date,
                    "type": "stage4_reconciliation",
                    "status": (archive.get("reconciliation") or {}).get("status"),
                    "source": "history",
                    "archive_id": archive_id,
                })

    # Stage 4 executions
    execs = sorted(glob.glob(
        os.path.join(newest_snapshot_dir, "stage4_execution", "execution_*.json")
    ))
    for p in execs:
        ex = _load_json(p) or {}
        events.append({
            "date": ex.get("execution_date") or ex.get("execution_timestamp"),
            "type": "stage4_execution",
            "status": ex.get("status"),
            "dry_run": ex.get("dry_run"),
            "orders_submitted": len(ex.get("orders_submitted") or []),
            "execution_timestamp": ex.get("execution_timestamp"),
        })

    # Stage 5 monitor runs
    monitor_runs = sorted(glob.glob(
        os.path.join(newest_snapshot_dir, "stage5", "monitor_run_*.json")
    ))
    for p in monitor_runs:
        mr = _load_json(p) or {}
        events.append({
            "date": (mr.get("started_at") or "")[:10] or None,
            "type": "stage5_monitor_run",
            "run_id": mr.get("run_id"),
            "status": mr.get("status"),
            "tickers_to_rerun": (mr.get("summary") or {}).get("tickers_to_rerun"),
        })

    events.sort(key=lambda e: (e.get("date") or "", e.get("type")))
    out["events"] = events
    return out


# ============ Entry point ============

def build_all(newest_snapshot_dir: Optional[str]) -> dict:
    """Computes both indices and returns them in a single dict."""
    return {
        "snapshots_index": build_snapshots_index(),
        "runs_index": build_runs_index(newest_snapshot_dir),
    }
