"""
Stage 4 — Portfolio Construction Pipeline Orchestrator.

Runs all Stage 4 sub-stages in sequence. Each sub-stage owns its own freshness
check, so re-running main.py is safe — only stale work gets redone.

Pipeline order:
    1. Candidate Summaries        (LLM per-company, parallelized internally)
    2. Pre-Optimization           (price cache + correlation + sector + macro analyses)
    3. Track A                    (pure quant MILP)
    4. Track B                    (pure LLM)
    5. Consolidation              (LLM selector + quant allocator)
    6. Reconciliation             (quant proposer + debate adjudicator)
    7. Portfolio Execution        (submits trades to Alpaca to match the final portfolio)

Fail-stop semantics: if any sub-stage fails (raises or exits non-zero), the
pipeline stops. Fix the failure and re-run main.py — freshness checks ensure
work isn't redone unnecessarily.
"""

import os
import sys
import time
import asyncio
import logging
import importlib.util
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()


# ============ LOGGING ============

log_filename = f"stage4_main_log_{datetime.now().strftime('%Y-%m-%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(log_filename, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ============ PATHS ============

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Each sub-stage lives in its own folder under /stage 4 DRAFT/
# Format: (label, folder_name, script_filename, is_async)
# Add new sub-stages by appending to this list — main.py needs no other changes.
SUB_STAGES = [
    ("Candidate Summaries", "candidate summaries", "runCandidateSummary.py",  True),
    ("Pre-Optimisation",    "pre optimisation",    "runPreOptimisation.py",   True),
    ("Track A",             "track A",             "runQuantOptimiser.py",    False),
    ("Track B",             "track B",             "runTrackB.py",            True),
    ("Consolidation",       "consolidation",       "runConsolidation.py",     True),
    ("Reconciliation",      "reconciliation",      "runReconciliation.py",    True),
    ("Portfolio Execution", "portfolio execution/executor", "runExecute.py",   True),
]


# ============ DYNAMIC IMPORT ============

def _load_module(module_path: str, module_name: str):
    """Dynamically load a Python file as a module."""
    if not os.path.exists(module_path):
        raise FileNotFoundError(f"Sub-stage script not found: {module_path}")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not create spec for {module_path}")
    module = importlib.util.module_from_spec(spec)
    # The script needs to find its own siblings on sys.path
    script_dir = os.path.dirname(module_path)
    sys.path.insert(0, script_dir)
    try:
        spec.loader.exec_module(module)
    finally:
        if sys.path and sys.path[0] == script_dir:
            sys.path.pop(0)
    return module


# ============ SUB-STAGE RUNNER ============

async def run_sub_stage(label: str, folder_name: str, script_filename: str, is_async: bool) -> bool:
    """
    Run one sub-stage by importing its module and invoking main().
    Returns True on success, False on failure.
    """
    print(f"\n{'='*70}")
    print(f"  Stage 4 — {label}")
    print(f"{'='*70}\n")

    script_path = os.path.join(SCRIPT_DIR, folder_name, script_filename)
    module_name = f"stage4_{folder_name.replace(' ', '_').lower()}_{script_filename.replace('.py', '')}"

    start_time = time.time()

    try:
        module = _load_module(script_path, module_name)
    except FileNotFoundError as e:
        logger.error(f"[{label}] {e}")
        print(f"\n[FAIL] {label}: script not found — {e}")
        return False
    except Exception as e:
        logger.error(f"[{label}] Failed to import {script_path}: {e}")
        print(f"\n[FAIL] {label}: import error — {e}")
        return False

    if not hasattr(module, "main"):
        logger.error(f"[{label}] Module has no main() function")
        print(f"\n[FAIL] {label}: no main() function in {script_filename}")
        return False

    try:
        if is_async:
            await module.main()
        else:
            # Run sync main in a thread so the event loop isn't blocked
            await asyncio.to_thread(module.main)
    except SystemExit as e:
        # Sub-stages call sys.exit(1) on failure. Treat non-zero as failure.
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        elapsed = time.time() - start_time
        if code != 0:
            logger.error(f"[{label}] exited with code {code} after {elapsed:.1f}s")
            print(f"\n[FAIL] {label} exited with code {code} after {elapsed:.1f}s")
            return False
        logger.info(f"[{label}] exited cleanly after {elapsed:.1f}s")
        return True
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(f"[{label}] raised exception after {elapsed:.1f}s: {e}")
        logger.exception(f"[{label}] traceback:")
        print(f"\n[FAIL] {label} raised exception after {elapsed:.1f}s: {e}")
        return False

    elapsed = time.time() - start_time
    print(f"\n[OK] {label} complete in {elapsed:.1f}s.")
    logger.info(f"[{label}] complete in {elapsed:.1f}s")
    return True


# ============ MAIN ============

async def main():
    pipeline_start = time.time()

    print(f"\n{'#'*70}")
    print(f"#  Stage 4 — Portfolio Construction Pipeline")
    print(f"#  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#'*70}")

    print(f"\nPipeline order ({len(SUB_STAGES)} sub-stages):")
    for i, (label, _, _, _) in enumerate(SUB_STAGES, start=1):
        print(f"  {i}. {label}")

    # Run each sub-stage sequentially, fail-stop on first failure
    for i, (label, folder_name, script_filename, is_async) in enumerate(SUB_STAGES, start=1):
        print(f"\n\n>>> [{i}/{len(SUB_STAGES)}] Running: {label}")

        ok = await run_sub_stage(label, folder_name, script_filename, is_async)
        if not ok:
            print(f"\n{'#'*70}")
            print(f"#  Stage 4 STOPPED at sub-stage {i} ({label}).")
            print(f"#  Fix the failure above, then re-run main.py.")
            print(f"#  Already-completed sub-stages will be skipped via freshness checks.")
            print(f"{'#'*70}")
            sys.exit(1)

    pipeline_elapsed = time.time() - pipeline_start

    print(f"\n\n{'#'*70}")
    print(f"#  Stage 4 COMPLETE.")
    print(f"#  Total runtime: {pipeline_elapsed:.1f}s ({pipeline_elapsed/60:.1f} min)")
    print(f"#  Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#'*70}")
    print(f"\nFinal portfolio: /stage 4 DRAFT/output/consolidation_portfolio.json")
    print(f"Execution logs:  /stage 4 DRAFT/portfolio execution/output/")


if __name__ == "__main__":
    asyncio.run(main())