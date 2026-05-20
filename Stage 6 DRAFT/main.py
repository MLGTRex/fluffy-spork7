"""
Stage 6 — Data Consolidation, Analysis & Backup.

Reads every upstream stage's outputs + live Alpaca data, writes:
  - backups/snapshots/{ts}/   — full point-in-time backups (no-op if no change)
  - backups/logs/{date}/      — copied rolling logs, deduped by hash
  - cache/                    — Alpaca fallback caches
  - state/prediction_log.jsonl — append-only, replayable from snapshots
  - output/*.json             — UI backend files

Independence:
  - Reads upstream files only via paths in config.py
  - Never imports from another stage's source tree
  - Never writes outside Stage 6 DRAFT/

On-demand: run `python "Stage 6 DRAFT/main.py"`.
Fail-soft: per-step errors land in runs/stage6_run_*.json and data_quality.json;
main does not abort on a single failure.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Callable

# Local imports
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import config  # noqa: E402
from snapshot import runSnapshot  # noqa: E402
from live import fetchAlpaca, fetchBenchmark  # noqa: E402
from performance import (  # noqa: E402
    computePortfolioPerf,
    buildPredictionLog,
    computePredictionAccuracy,
)
from dossier import (  # noqa: E402
    buildPortfolioOverview,
    buildPositionsLedger,
    buildPerTicker,
)
from indexing import buildIndices  # noqa: E402


# ============ Logging ============

config.ensure_stage6_dirs()
log_dir = os.path.join(config.SCRIPT_DIR, "logs")
os.makedirs(log_dir, exist_ok=True)
log_path = os.path.join(
    log_dir, f"stage6_main_log_{datetime.now().strftime('%Y-%m-%d')}.log"
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger("stage6.main")


# ============ Atomic write ============

def _atomic_write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, path)


# ============ Step runner ============

def _step(name: str, fn: Callable[[], Any], audit: list[dict], warnings: list[str]):
    """Run a step, capture timing + errors into the audit list. Returns result or None."""
    start = time.time()
    record = {"step": name, "status": "ok", "duration_s": 0.0, "error": None}
    try:
        result = fn()
        record["duration_s"] = round(time.time() - start, 3)
        audit.append(record)
        logger.info("[%s] ok in %.2fs", name, record["duration_s"])
        return result
    except Exception as e:
        record["status"] = "error"
        record["duration_s"] = round(time.time() - start, 3)
        record["error"] = f"{type(e).__name__}: {e}"
        record["traceback"] = traceback.format_exc()
        audit.append(record)
        warnings.append(f"{name}: {record['error']}")
        logger.error("[%s] failed after %.2fs: %s", name, record["duration_s"], e)
        return None


# ============ Main ============

def main() -> int:
    pipeline_start = time.time()
    started_at = datetime.now(timezone.utc).isoformat()
    audit: list[dict] = []
    warnings: list[str] = []

    logger.info("Stage 6 starting — log: %s", log_path)

    # 1. Snapshot pass (also archives logs internally)
    snapshot_info = _step(
        "snapshot", runSnapshot.run_snapshot, audit, warnings
    ) or {}
    snapshot_id = snapshot_info.get("snapshot_id") or snapshot_info.get("prior_snapshot_id")
    snapshot_dir = snapshot_info.get("snapshot_dir") or runSnapshot.newest_snapshot_dir()

    # 2. Live Alpaca pull
    alpaca_bundle = _step("alpaca_fetch", fetchAlpaca.fetch_all, audit, warnings) or {
        "account": None, "positions": None, "portfolio_history": None,
        "account_stale": True, "positions_stale": True, "portfolio_history_stale": True,
        "errors": ["alpaca_step_failed"],
    }

    # 3. Benchmark pull (depends on portfolio history for date range)
    benchmark_bundle = _step(
        "benchmark_fetch",
        lambda: fetchBenchmark.fetch_benchmark(alpaca_bundle.get("portfolio_history")),
        audit,
        warnings,
    ) or {"bars": None, "stale": True, "symbol": config.BENCHMARK_SYMBOL}

    # 4. Performance compute
    perf_bundle = _step(
        "performance_compute",
        lambda: computePortfolioPerf.compute(
            alpaca_bundle.get("portfolio_history"), benchmark_bundle
        ),
        audit,
        warnings,
    ) or {"performance": {}, "portfolio_value_history": {"series": []},
          "benchmark_history": {"series": []}}

    # 5. Prediction log rebuild
    prediction_log_info = _step(
        "prediction_log_build", buildPredictionLog.build, audit, warnings
    ) or {"entries_total": 0, "entries_appended": 0, "skipped_missing_stage3": []}

    prediction_log_entries = _step(
        "prediction_log_load", buildPredictionLog.load_all_entries, audit, warnings
    ) or []

    # 6. Prediction accuracy
    accuracy_bundle = _step(
        "prediction_accuracy",
        lambda: computePredictionAccuracy.compute(prediction_log_entries),
        audit,
        warnings,
    ) or {
        "evaluated": [], "pending": [], "aggregate": {
            "by_horizon": {}, "by_conviction": {}, "n_evaluated": 0, "n_pending": 0,
        }, "stale": True,
    }

    # 7. Positions ledger
    ledger = _step(
        "positions_ledger",
        lambda: buildPositionsLedger.build(snapshot_dir),
        audit,
        warnings,
    ) or {"entries": [], "warnings": ["ledger_step_failed"]}

    # 8. Portfolio overview (depends on snapshot + alpaca + perf)
    portfolio_overview = _step(
        "portfolio_overview",
        lambda: buildPortfolioOverview.build(
            snapshot_dir, snapshot_id, alpaca_bundle, perf_bundle
        ),
        audit,
        warnings,
    ) or {"positions": [], "performance": perf_bundle.get("performance", {})}

    # 9. Per-ticker dossiers (depends on overview + ledger)
    per_ticker_info = _step(
        "per_ticker_dossiers",
        lambda: buildPerTicker.build(
            snapshot_dir, snapshot_id, alpaca_bundle, ledger, portfolio_overview
        ),
        audit,
        warnings,
    ) or {"tickers_written": [], "tickers_missing_stage3": [], "tickers_missing_stage1": []}

    # 10. Indices
    indices = _step(
        "indices",
        lambda: buildIndices.build_all(snapshot_dir),
        audit,
        warnings,
    ) or {"snapshots_index": {"snapshots": []}, "runs_index": {"events": []}}

    # 11. Write all UI outputs (atomic)
    def _write_outputs():
        _atomic_write_json(
            os.path.join(config.OUTPUT_DIR, "portfolio_overview.json"),
            portfolio_overview,
        )
        _atomic_write_json(
            os.path.join(config.OUTPUT_DIR, "portfolio_value_history.json"),
            perf_bundle.get("portfolio_value_history", {}),
        )
        _atomic_write_json(
            os.path.join(config.OUTPUT_DIR, "benchmark_history.json"),
            perf_bundle.get("benchmark_history", {}),
        )
        _atomic_write_json(
            os.path.join(config.OUTPUT_DIR, "positions_ledger.json"), ledger
        )
        _atomic_write_json(
            os.path.join(config.OUTPUT_DIR, "prediction_accuracy.json"),
            accuracy_bundle,
        )
        _atomic_write_json(
            os.path.join(config.OUTPUT_DIR, "snapshots_index.json"),
            indices.get("snapshots_index", {}),
        )
        _atomic_write_json(
            os.path.join(config.OUTPUT_DIR, "runs_index.json"),
            indices.get("runs_index", {}),
        )

    _step("write_outputs", _write_outputs, audit, warnings)

    # 12. data_quality.json
    consolidation = portfolio_overview.get("pipeline_run") or {}
    consolidation_date = consolidation.get("latest_consolidation_date")
    days_old = None
    if consolidation_date:
        try:
            d = datetime.fromisoformat(consolidation_date).date()
            days_old = (datetime.now(timezone.utc).date() - d).days
        except Exception:
            pass

    data_quality = {
        "as_of": started_at,
        "snapshot_id": snapshot_info.get("snapshot_id") or snapshot_info.get("prior_snapshot_id"),
        "snapshot_skipped": bool(snapshot_info.get("snapshot_skipped")),
        "live": {
            "account_stale": bool(alpaca_bundle.get("account_stale")),
            "positions_stale": bool(alpaca_bundle.get("positions_stale")),
            "portfolio_history_stale": bool(alpaca_bundle.get("portfolio_history_stale")),
            "benchmark_stale": bool(benchmark_bundle.get("stale")),
            "accuracy_stale": bool(accuracy_bundle.get("stale")),
        },
        "upstream": {
            "stage4_consolidation_age_days": days_old,
            "missing_dossier_tickers": (per_ticker_info or {}).get("tickers_missing_stage3", []),
            "tickers_missing_stage1": (per_ticker_info or {}).get("tickers_missing_stage1", []),
        },
        "prediction_log": {
            "entries_total": prediction_log_info.get("entries_total", 0),
            "entries_appended_this_run": prediction_log_info.get("entries_appended", 0),
            "skipped_missing_stage3": len(prediction_log_info.get("skipped_missing_stage3", [])),
            "entries_pending": (accuracy_bundle.get("aggregate") or {}).get("n_pending", 0),
            "entries_evaluated": (accuracy_bundle.get("aggregate") or {}).get("n_evaluated", 0),
        },
        "warnings": warnings,
    }
    _step(
        "write_data_quality",
        lambda: _atomic_write_json(
            os.path.join(config.OUTPUT_DIR, "data_quality.json"), data_quality
        ),
        audit,
        warnings,
    )

    # 13. Run audit
    pipeline_elapsed = round(time.time() - pipeline_start, 3)
    ended_at = datetime.now(timezone.utc).isoformat()
    audit_payload = {
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_s": pipeline_elapsed,
        "snapshot_id": snapshot_info.get("snapshot_id"),
        "snapshot_skipped": bool(snapshot_info.get("snapshot_skipped")),
        "prior_snapshot_id": snapshot_info.get("prior_snapshot_id"),
        "tracked_file_count": snapshot_info.get("tracked_file_count"),
        "changes": snapshot_info.get("changes"),
        "log_archive": snapshot_info.get("log_archive"),
        "alpaca_errors": alpaca_bundle.get("errors", []),
        "benchmark_errors": benchmark_bundle.get("errors", []),
        "warnings": warnings,
        "steps": audit,
        "per_ticker_dossiers_written": len(per_ticker_info.get("tickers_written", [])),
        "prediction_log": {
            "entries_total": prediction_log_info.get("entries_total"),
            "entries_appended": prediction_log_info.get("entries_appended"),
        },
    }
    audit_ts = ended_at.replace(":", "-").replace("+", "_")
    audit_path = os.path.join(config.RUNS_DIR, f"stage6_run_{audit_ts}.json")
    _atomic_write_json(audit_path, audit_payload)
    logger.info("Wrote run audit: %s", audit_path)

    # 14. Console summary
    print()
    print("=" * 70)
    print(f"  Stage 6 complete — {pipeline_elapsed:.1f}s — warnings: {len(warnings)}")
    if snapshot_info.get("snapshot_skipped"):
        print(f"  Snapshot:        SKIPPED (prior: {snapshot_info.get('prior_snapshot_id')})")
    else:
        print(f"  Snapshot:        {snapshot_info.get('snapshot_id')}")
        changes = snapshot_info.get("changes") or {}
        print(
            f"     changes:      +{len(changes.get('added') or [])} "
            f"~{len(changes.get('modified') or [])} "
            f"-{len(changes.get('removed') or [])}"
        )
    print(f"  Tracked files:   {snapshot_info.get('tracked_file_count')}")
    print(f"  Per-ticker docs: {len(per_ticker_info.get('tickers_written', []))} written")
    print(f"  Prediction log:  {prediction_log_info.get('entries_total')} total "
          f"(+{prediction_log_info.get('entries_appended')} this run)")
    print(f"  Accuracy:        {(accuracy_bundle.get('aggregate') or {}).get('n_evaluated', 0)} evaluated, "
          f"{(accuracy_bundle.get('aggregate') or {}).get('n_pending', 0)} pending")
    print(f"  UI outputs:      {config.OUTPUT_DIR}")
    print(f"  Run audit:       {audit_path}")
    if warnings:
        print(f"  Warnings ({len(warnings)}):")
        for w in warnings:
            print(f"     - {w}")
    print("=" * 70)

    # Exit code: 0 always (fail-soft). Errors are visible in data_quality.json
    # and the audit; consumers decide whether to alert.
    return 0


if __name__ == "__main__":
    sys.exit(main())
