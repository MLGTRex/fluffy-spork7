"""
Workflow script for the Pruner sub-stage.

Reads:
    - Stage 1 DRAFT/output/target_company_list.json (the current universe)
    - Stage 2 DRAFT/output/* (per-company research files)
    - Stage 3 DRAFT/output/* (per-company scenario/valuation/consolidation files)
    - Stage 4 DRAFT/output/* (portfolio-level files)
    - Stage 4 DRAFT/cache/* (price cache, etc.)

Calls prune.compute_prune_plan() to determine what should be moved or deleted.

Performs the actual file operations:
    - Moves per-company files for dropped tickers from Stage 2 and Stage 3
      output dirs to Stage 5 DRAFT/backups/pruned/stage2/{timestamp}/ and
      stage3/{timestamp}/
    - Moves the regenerated Stage 4 portfolio-level output files to
      backups/pruned/stage4/{timestamp}/. The "portfolio history" directory is
      preserved — it is the reconciliation sub-stage's incumbent archive.
    - Deletes ALL Stage 4 cache files (rebuildable for free)

Always moves, never deletes (except Stage 4 cache).

CLI: Can be invoked directly as `python3 runPrune.py [--dry-run]`. Also
importable: `from pruner.runPrune import main` (async).

DRY_RUN mode at the top toggles whether to actually move/delete files or
just log the intended plan. Override with --dry-run / --execute CLI flags.
"""

import os
import sys
import json
import shutil
import asyncio
import argparse
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# Allow importing the functional sibling
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from prune import compute_prune_plan


# ============ CONFIG ============

# When True, log what would be moved/deleted but don't actually do it. Useful
# for testing. Override with --dry-run / --execute CLI flags when invoking
# directly.
DRY_RUN = False


# ============ PATHS ============

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STAGE5_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
PROJECT_ROOT = os.path.normpath(os.path.join(STAGE5_ROOT, ".."))

TARGET_LIST_PATH = os.path.join(PROJECT_ROOT, "Stage 1 DRAFT", "output", "target_company_list.json")

STAGE_2_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "Stage 2 DRAFT", "output")
STAGE_3_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "Stage 3 DRAFT", "output")
STAGE_4_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "Stage 4 DRAFT", "output")
STAGE_4_CACHE_DIR = os.path.join(PROJECT_ROOT, "Stage 4 DRAFT", "cache")

# Currently-held portfolio — used to protect held tickers from pruning.
CONSOLIDATION_PORTFOLIO_PATH = os.path.join(STAGE_4_OUTPUT_DIR, "consolidation_portfolio.json")
STAGE_4_HISTORY_DIR = os.path.join(STAGE_4_OUTPUT_DIR, "portfolio history")

BACKUPS_PRUNED_ROOT = os.path.join(STAGE5_ROOT, "backups", "pruned")
LOGS_DIR = os.path.join(STAGE5_ROOT, "logs")


# ============ LOGGING ============

os.makedirs(LOGS_DIR, exist_ok=True)
_log_filename = os.path.join(LOGS_DIR, f"pruner_{datetime.now().strftime('%Y-%m-%d')}.log")

logger = logging.getLogger("pruner_workflow")
if not logger.handlers:
    handler = logging.FileHandler(_log_filename, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


# ============ HELPERS ============

def _list_files_in_dir(dir_path: str) -> list:
    """List filenames (not full paths) in a directory. Returns [] if dir missing."""
    if not os.path.isdir(dir_path):
        return []
    return [
        f for f in sorted(os.listdir(dir_path))
        if os.path.isfile(os.path.join(dir_path, f))
    ]


def _stage_1_target_list_mtime() -> str:
    """Return ISO timestamp of Stage 1's target_company_list.json modification."""
    if not os.path.exists(TARGET_LIST_PATH):
        return None
    mtime = os.path.getmtime(TARGET_LIST_PATH)
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


def _stage_1_target_list_age_days() -> float:
    """Return age in days of Stage 1's target list (rough freshness sanity check)."""
    if not os.path.exists(TARGET_LIST_PATH):
        return None
    mtime = os.path.getmtime(TARGET_LIST_PATH)
    age_seconds = datetime.now(timezone.utc).timestamp() - mtime
    return round(age_seconds / 86400.0, 2)


def _load_target_list() -> list:
    if not os.path.exists(TARGET_LIST_PATH):
        raise FileNotFoundError(
            f"target_company_list.json not found at {TARGET_LIST_PATH}. "
            f"Pruner requires Stage 1 output. Run Stage 1 first."
        )
    with open(TARGET_LIST_PATH, "r", encoding="utf-8") as f:
        contents = json.load(f)
    return contents


def _extract_held_tickers(portfolio_path: str) -> set:
    """Read a portfolio JSON and return the set of held position tickers."""
    with open(portfolio_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    positions = (data.get("portfolio") or {}).get("positions") or []
    return {
        p["ticker"] for p in positions
        if isinstance(p, dict) and p.get("ticker")
    }


def _load_held_tickers(data_quality_flags: list) -> set:
    """
    Return the set of currently-held portfolio tickers, so the pruner can
    protect their Stage 2/3 research from being pruned even if they fell out of
    Stage 1's fresh universe.

    Primary source: Stage 4's consolidation_portfolio.json. Fallback (if that
    file is missing/unreadable): the newest file in Stage 4's "portfolio
    history" archive, which is never pruned. Best-effort: on total failure,
    returns an empty set so the pruner degrades to its pre-existing behaviour.
    """
    if os.path.exists(CONSOLIDATION_PORTFOLIO_PATH):
        try:
            return _extract_held_tickers(CONSOLIDATION_PORTFOLIO_PATH)
        except Exception as e:
            msg = f"Could not read held positions from consolidation_portfolio.json: {e}"
            data_quality_flags.append(msg)
            logger.warning(msg)

    # Fallback: newest archived portfolio (portfolio history is never pruned).
    try:
        if os.path.isdir(STAGE_4_HISTORY_DIR):
            history = sorted(
                f for f in os.listdir(STAGE_4_HISTORY_DIR) if f.endswith(".json")
            )
            if history:
                return _extract_held_tickers(
                    os.path.join(STAGE_4_HISTORY_DIR, history[-1])
                )
    except Exception as e:
        msg = f"Could not read held positions from portfolio history: {e}"
        data_quality_flags.append(msg)
        logger.warning(msg)

    return set()


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _move_file(src: str, dst: str, data_quality_flags: list) -> bool:
    """Move src to dst. Returns True on success."""
    try:
        _ensure_dir(os.path.dirname(dst))
        shutil.move(src, dst)
        return True
    except Exception as e:
        data_quality_flags.append(f"Failed to move {src} → {dst}: {e}")
        logger.error(f"Move failed: {src} → {dst}: {e}")
        return False


def _delete_file(path: str, data_quality_flags: list) -> bool:
    """Delete a file. Returns True on success."""
    try:
        os.remove(path)
        return True
    except Exception as e:
        data_quality_flags.append(f"Failed to delete {path}: {e}")
        logger.error(f"Delete failed: {path}: {e}")
        return False


# ============ MAIN ============

async def main(dry_run_override: bool = None) -> dict:
    """
    Run the pruner. Returns the full result dict (also written to log).

    Args:
        dry_run_override: if not None, overrides the DRY_RUN constant. The CLI
            uses this to honor --dry-run / --execute flags.

    Returns:
        Result dict with the prune plan, what was actually moved/deleted,
        backup paths, data quality flags, and status.
    """
    effective_dry_run = DRY_RUN if dry_run_override is None else dry_run_override

    now = datetime.now(timezone.utc)
    timestamp_str = now.strftime("%Y-%m-%dT%H-%M-%S")

    print(f"\n[Pruner] Starting (DRY_RUN={effective_dry_run})")
    logger.info(f"Pruner starting, DRY_RUN={effective_dry_run}")

    result = {
        "pruner_run_timestamp": now.isoformat(),
        "dry_run": effective_dry_run,
        "stage_1_target_list_mtime": _stage_1_target_list_mtime(),
        "stage_1_target_list_age_days": _stage_1_target_list_age_days(),
        "plan": None,
        "moves_executed": {
            "stage_2": [],
            "stage_3": [],
            "stage_4": [],
        },
        "deletions_executed": {
            "stage_4_cache": [],
        },
        "backup_paths": {
            "stage_2": None,
            "stage_3": None,
            "stage_4": None,
        },
        "status": "unknown",
        "data_quality_flags": [],
    }

    # ============ Step 1: Load target list ============
    try:
        target_list_contents = _load_target_list()
    except Exception as e:
        result["status"] = "failed"
        result["data_quality_flags"].append(f"Could not load target list: {e}")
        logger.error(f"Could not load target list: {e}")
        print(f"[Pruner] FAILED: {e}")
        return result

    age = result["stage_1_target_list_age_days"]
    if age is not None and age > 14:
        msg = (
            f"Stage 1's target_company_list.json is {age:.1f} days old. "
            f"The pruner is proceeding, but the universe may be stale. "
            f"Consider re-running Stage 1 before the pruner."
        )
        result["data_quality_flags"].append(msg)
        print(f"[Pruner] WARNING: {msg}")
        logger.warning(msg)

    # ============ Step 2: Scan filesystem ============
    stage_2_files = _list_files_in_dir(STAGE_2_OUTPUT_DIR)
    stage_3_files = _list_files_in_dir(STAGE_3_OUTPUT_DIR)
    stage_4_files = _list_files_in_dir(STAGE_4_OUTPUT_DIR)
    stage_4_cache_files = _list_files_in_dir(STAGE_4_CACHE_DIR)

    print(f"[Pruner] Found {len(stage_2_files)} files in Stage 2 output")
    print(f"[Pruner] Found {len(stage_3_files)} files in Stage 3 output")
    print(f"[Pruner] Found {len(stage_4_files)} files in Stage 4 output")
    print(f"[Pruner] Found {len(stage_4_cache_files)} files in Stage 4 cache")

    # ============ Step 2b: Load held positions (protected from pruning) ============
    held_tickers = _load_held_tickers(result["data_quality_flags"])
    print(f"[Pruner] {len(held_tickers)} currently-held ticker(s) protected from pruning")
    logger.info(f"Held tickers protected from pruning: {sorted(held_tickers)}")

    # ============ Step 3: Compute plan ============
    try:
        plan = compute_prune_plan(
            target_list_contents=target_list_contents,
            stage_2_files=stage_2_files,
            stage_3_files=stage_3_files,
            stage_4_files=stage_4_files,
            stage_4_cache_files=stage_4_cache_files,
            held_tickers=held_tickers,
        )
    except Exception as e:
        result["status"] = "failed"
        result["data_quality_flags"].append(f"Plan computation failed: {e}")
        logger.exception("Plan computation failed:")
        print(f"[Pruner] FAILED computing plan: {e}")
        return result

    result["plan"] = {
        "universe_size": plan["summary"]["universe_size"],
        "held_protected_count": plan["summary"]["held_protected_count"],
        "held_tickers": plan["universe"].get("held_tickers", []),
        "stage_2": {
            "to_move_count": len(plan["stage_2"]["to_move"]),
            "to_keep_count": len(plan["stage_2"]["to_keep"]),
            "unrecognized_count": len(plan["stage_2"]["unrecognized"]),
            "to_move_files": [m["filename"] for m in plan["stage_2"]["to_move"]],
            "unrecognized_files": plan["stage_2"]["unrecognized"],
        },
        "stage_3": {
            "to_move_count": len(plan["stage_3"]["to_move"]),
            "to_keep_count": len(plan["stage_3"]["to_keep"]),
            "unrecognized_count": len(plan["stage_3"]["unrecognized"]),
            "to_move_files": [m["filename"] for m in plan["stage_3"]["to_move"]],
            "unrecognized_files": plan["stage_3"]["unrecognized"],
        },
        "stage_4": {
            "to_move_count": len(plan["stage_4"]["to_move"]),
            "to_move_files": [m["filename"] for m in plan["stage_4"]["to_move"]],
        },
        "stage_4_cache": {
            "to_delete_count": len(plan["stage_4_cache"]["to_delete"]),
            "to_delete_files": [m["filename"] for m in plan["stage_4_cache"]["to_delete"]],
        },
    }

    print(f"\n[Pruner] Plan:")
    print(f"  Universe: {plan['summary']['universe_size']} tickers")
    print(f"  Held protected: {plan['summary']['held_protected_count']} ticker(s) kept despite not being in the fresh universe")
    print(f"  Stage 2: move {plan['summary']['stage_2_to_move']}, keep {plan['summary']['stage_2_to_keep']}, unrecognized {plan['summary']['unrecognized_stage_2_files']}")
    print(f"  Stage 3: move {plan['summary']['stage_3_to_move']}, keep {plan['summary']['stage_3_to_keep']}, unrecognized {plan['summary']['unrecognized_stage_3_files']}")
    print(f"  Stage 4: move {plan['summary']['stage_4_to_move']}")
    print(f"  Stage 4 cache: delete {plan['summary']['stage_4_cache_to_delete']}")

    for warning_field, label in [
        ("unrecognized_files", "Unrecognized Stage 2 files (left in place):"),
    ]:
        if plan["stage_2"]["unrecognized"]:
            msg = f"Stage 2 has {len(plan['stage_2']['unrecognized'])} unrecognized files (not matched to any ticker): {plan['stage_2']['unrecognized']}"
            result["data_quality_flags"].append(msg)
            logger.warning(msg)

    if plan["stage_3"]["unrecognized"]:
        msg = f"Stage 3 has {len(plan['stage_3']['unrecognized'])} unrecognized files (not matched to any ticker): {plan['stage_3']['unrecognized']}"
        result["data_quality_flags"].append(msg)
        logger.warning(msg)

    if plan["universe"]["unparseable"]:
        msg = f"target_company_list.json had {len(plan['universe']['unparseable'])} unparseable entries (no ticker extracted): {plan['universe']['unparseable']}"
        result["data_quality_flags"].append(msg)
        logger.warning(msg)

    # ============ Step 4: Execute (or dry-run) ============
    if effective_dry_run:
        print(f"\n[Pruner] DRY RUN — no files will be moved or deleted.")
        result["status"] = "success_dry_run"
        logger.info(f"Dry run complete. Plan summary: {plan['summary']}")
        return result

    # Stage 2 moves
    if plan["stage_2"]["to_move"]:
        backup_dir = os.path.join(BACKUPS_PRUNED_ROOT, "stage2", timestamp_str)
        result["backup_paths"]["stage_2"] = backup_dir
        for m in plan["stage_2"]["to_move"]:
            src = os.path.join(STAGE_2_OUTPUT_DIR, m["filename"])
            dst = os.path.join(backup_dir, m["filename"])
            ok = _move_file(src, dst, result["data_quality_flags"])
            result["moves_executed"]["stage_2"].append({
                "filename": m["filename"],
                "matched_ticker": m["matched_ticker"],
                "src": src,
                "dst": dst,
                "moved": ok,
            })
        moved_count = sum(1 for m in result["moves_executed"]["stage_2"] if m["moved"])
        print(f"[Pruner] Moved {moved_count}/{len(plan['stage_2']['to_move'])} Stage 2 files to {backup_dir}")
        logger.info(f"Moved {moved_count} Stage 2 files to {backup_dir}")

    # Stage 3 moves
    if plan["stage_3"]["to_move"]:
        backup_dir = os.path.join(BACKUPS_PRUNED_ROOT, "stage3", timestamp_str)
        result["backup_paths"]["stage_3"] = backup_dir
        for m in plan["stage_3"]["to_move"]:
            src = os.path.join(STAGE_3_OUTPUT_DIR, m["filename"])
            dst = os.path.join(backup_dir, m["filename"])
            ok = _move_file(src, dst, result["data_quality_flags"])
            result["moves_executed"]["stage_3"].append({
                "filename": m["filename"],
                "matched_ticker": m["matched_ticker"],
                "src": src,
                "dst": dst,
                "moved": ok,
            })
        moved_count = sum(1 for m in result["moves_executed"]["stage_3"] if m["moved"])
        print(f"[Pruner] Moved {moved_count}/{len(plan['stage_3']['to_move'])} Stage 3 files to {backup_dir}")
        logger.info(f"Moved {moved_count} Stage 3 files to {backup_dir}")

    # Stage 4 moves (always, regardless of whether universe changed)
    if plan["stage_4"]["to_move"]:
        backup_dir = os.path.join(BACKUPS_PRUNED_ROOT, "stage4", timestamp_str)
        result["backup_paths"]["stage_4"] = backup_dir
        for m in plan["stage_4"]["to_move"]:
            src = os.path.join(STAGE_4_OUTPUT_DIR, m["filename"])
            dst = os.path.join(backup_dir, m["filename"])
            ok = _move_file(src, dst, result["data_quality_flags"])
            result["moves_executed"]["stage_4"].append({
                "filename": m["filename"],
                "src": src,
                "dst": dst,
                "moved": ok,
            })
        moved_count = sum(1 for m in result["moves_executed"]["stage_4"] if m["moved"])
        print(f"[Pruner] Moved {moved_count}/{len(plan['stage_4']['to_move'])} Stage 4 files to {backup_dir}")
        logger.info(f"Moved {moved_count} Stage 4 files to {backup_dir}")

    # Stage 4 cache deletes (the only thing that gets deleted, not moved)
    if plan["stage_4_cache"]["to_delete"]:
        for m in plan["stage_4_cache"]["to_delete"]:
            path = os.path.join(STAGE_4_CACHE_DIR, m["filename"])
            ok = _delete_file(path, result["data_quality_flags"])
            result["deletions_executed"]["stage_4_cache"].append({
                "filename": m["filename"],
                "path": path,
                "deleted": ok,
            })
        deleted_count = sum(1 for d in result["deletions_executed"]["stage_4_cache"] if d["deleted"])
        print(f"[Pruner] Deleted {deleted_count}/{len(plan['stage_4_cache']['to_delete'])} Stage 4 cache files")
        logger.info(f"Deleted {deleted_count} Stage 4 cache files")

    # ============ Step 5: Status ============
    move_failures = sum(
        1 for stage_moves in result["moves_executed"].values()
        for m in stage_moves
        if not m.get("moved", False)
    )
    delete_failures = sum(
        1 for d in result["deletions_executed"]["stage_4_cache"]
        if not d.get("deleted", False)
    )

    if move_failures == 0 and delete_failures == 0:
        result["status"] = "success"
        print(f"[Pruner] Complete: success.")
        logger.info("Pruner complete: success.")
    else:
        result["status"] = "partial"
        msg = f"Completed with {move_failures} move failure(s) and {delete_failures} delete failure(s)."
        result["data_quality_flags"].append(msg)
        print(f"[Pruner] Complete with failures: {msg}")
        logger.warning(msg)

    return result


# ============ CLI ============

def _parse_cli_args():
    parser = argparse.ArgumentParser(description="Stage 5 pruner — moves stale outputs to backup.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true",
                       help="Compute the plan but don't move/delete anything.")
    group.add_argument("--execute", action="store_true",
                       help="Actually perform moves/deletes (overrides DRY_RUN=True at top of script).")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_cli_args()
    if args.dry_run:
        override = True
    elif args.execute:
        override = False
    else:
        override = None  # use the DRY_RUN constant at the top of the script

    result = asyncio.run(main(dry_run_override=override))

    # Print a brief summary to stdout
    print("\n" + "=" * 70)
    print(f"Pruner result: status={result['status']}")
    if result["plan"]:
        print(f"  Universe: {result['plan']['universe_size']} tickers")
    print(f"  Stage 2 moves: {len(result['moves_executed']['stage_2'])}")
    print(f"  Stage 3 moves: {len(result['moves_executed']['stage_3'])}")
    print(f"  Stage 4 moves: {len(result['moves_executed']['stage_4'])}")
    print(f"  Stage 4 cache deletes: {len(result['deletions_executed']['stage_4_cache'])}")
    if result["data_quality_flags"]:
        print(f"  Flags ({len(result['data_quality_flags'])}):")
        for f in result["data_quality_flags"]:
            print(f"    - {f}")
    print("=" * 70)

    sys.exit(0 if result["status"] in ("success", "success_dry_run") else 1)