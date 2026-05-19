"""
Stage 5 weekly run — full pipeline rebuild.

Registered as a Stage 5 sub-stage and scheduled via schedule_config.json
("weekly_run", day_of_week_time, Saturday 00:00 UTC). The scheduler dispatches
it by importing this file and awaiting main().

All it does: invoke pipeline tools/orchestrator.py's run_pipeline() to run
Stages 1-4 end to end. Stage 4's own main.py runs its Portfolio Execution
sub-stage as part of that, so the weekly run also places the resulting trades.
Every stage owns its own freshness checks, so the orchestrator only redoes
stale work.

Usage:
    python3 runWeeklyRun.py        # triggers a full pipeline run (long; trades)
"""

import os
import sys
import asyncio
import logging
import importlib.util
from datetime import datetime, timezone


# ============ PATHS ============

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STAGE5_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
PROJECT_ROOT = os.path.normpath(os.path.join(STAGE5_ROOT, ".."))
ORCHESTRATOR_PATH = os.path.join(PROJECT_ROOT, "pipeline tools", "orchestrator.py")


# ============ LOGGER ============

logger = logging.getLogger("stage5.weekly_run")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


# ============ ORCHESTRATOR LOADING ============

def _load_orchestrator():
    """
    Dynamically load pipeline tools/orchestrator.py and return the module.
    Loaded by path because the containing folder name has a space.
    """
    if not os.path.exists(ORCHESTRATOR_PATH):
        raise FileNotFoundError(f"Orchestrator not found: {ORCHESTRATOR_PATH}")
    spec = importlib.util.spec_from_file_location("weekly_run_orchestrator", ORCHESTRATOR_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not create spec for {ORCHESTRATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ============ MAIN ============

async def main():
    started = datetime.now(timezone.utc)
    logger.info("Weekly run starting — full pipeline rebuild via orchestrator (Stages 1-4).")

    orchestrator = _load_orchestrator()
    summary = await orchestrator.run_pipeline(run_stage_1=True)

    status = summary.get("overall_status")
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    logger.info(f"Weekly run finished in {elapsed:.0f}s — overall_status={status}")

    # Mirror the orchestrator's own exit semantics: only a hard halt is a
    # failure. 'completed' and 'completed_with_gaps' are treated as success.
    if status == "halted":
        logger.error(
            f"Pipeline halted at {summary.get('halted_at_stage')} — "
            f"weekly run reports failure."
        )
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
