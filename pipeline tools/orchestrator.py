"""
Pipeline Orchestrator.

Runs Stages 1-4 in order. Each stage owns its own freshness checks, so this
orchestrator never reimplements skip logic and never modifies a stage script —
it just runs a stage's main.py, then verifies the stage's outputs are actually
complete, and reruns the stage until they are (or until the stage is provably
stuck).

Completion model per stage:
    1. Snapshot the stage's output dir (content hashes).
    2. Run the stage as a subprocess. Its own freshness checks resume only the
       incomplete work.
    3. Snapshot again. A run that changed nothing = no progress.
    4. Check completeness (the orchestrator's own read-only inspection):
         - fixed stages (1, 4): required output files present.
         - per-company stages (2, 3): every expected company's research JSON
           has the stage's terminal field populated.
    5. Decide:
         - complete                         -> move to next stage
         - incomplete + progress was made   -> rerun
         - incomplete + no progress         -> stuck; apply the threshold gate
       Threshold gate: continue (recording the gaps) if completion ratio is at
       or above COMPLETION_THRESHOLD, otherwise halt the whole pipeline.

Stage 1 can be skipped (--skip-stage-1) for targeted reruns that operate on an
already-narrowed target_company_list.json — this is the entry point Stage 5's
pipeline invoker is intended to delegate to.
"""

import os
import sys
import json
import time
import hashlib
import asyncio
import logging
import argparse
import re
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()


# ============ PATHS ============

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
TARGET_LIST_PATH = os.path.join(
    PROJECT_ROOT, "Stage 1 DRAFT", "output", "target_company_list.json"
)
PRUNER_DIR = os.path.join(PROJECT_ROOT, "Stage 5 DRAFT", "pruner")


# ============ CONFIG ============

# Absolute cap on attempts per stage, regardless of progress.
MAX_ATTEMPTS = 4

# Consecutive no-progress runs tolerated before a stage is declared stuck.
# >1 absorbs transient API flakiness (a retry that happens to catch a working API).
NO_PROGRESS_TOLERANCE = 2

# A stuck stage continues (recording the gaps) only if at least this fraction of
# its expected outputs completed; otherwise the pipeline halts.
COMPLETION_THRESHOLD = 0.90

# Subprocess timeout for the Stage 5 pruner (seconds). It only moves files — a
# generous backstop against a hang, not a normal-run budget.
PRUNER_TIMEOUT = 300

# Per-stage subprocess timeout (seconds). This is a kill-a-hung-stage backstop,
# not a normal-run budget — set generously.
STAGE_SPECS = [
    {
        "key": "stage_1",
        "label": "Stage 1 - Universe & Scoring",
        "dir": "Stage 1 DRAFT",
        "output_dir": "Stage 1 DRAFT/output",
        "kind": "fixed",
        "required_files": ["target_company_list.json"],
        "timeout": 4 * 3600,
    },
    {
        "key": "stage_2",
        "label": "Stage 2 - Research & Debate",
        "dir": "Stage 2 DRAFT",
        "output_dir": "Stage 2 DRAFT/output",
        "kind": "per_company",
        "terminal_field": "synthesis",
        "numeric_fields": [],
        "timeout": 6 * 3600,
    },
    {
        "key": "stage_3",
        "label": "Stage 3 - Scenarios & Valuation",
        "dir": "Stage 3 DRAFT",
        "output_dir": "Stage 3 DRAFT/output",
        "kind": "per_company",
        "terminal_field": "consolidation",
        "numeric_fields": ["expected_return_12m"],
        "timeout": 6 * 3600,
    },
    {
        "key": "stage_4",
        "label": "Stage 4 - Portfolio Construction",
        "dir": "Stage 4 DRAFT",
        "output_dir": "Stage 4 DRAFT/output",
        "kind": "fixed",
        "required_files": [
            "candidate_summaries.json",
            "pre_optimization.json",
            "track_a_portfolio.json",
            "track_b_portfolio.json",
            "consolidation_portfolio.json",
        ],
        "timeout": 3 * 3600,
    },
]


# ============ LOGGING ============

_log_path = os.path.join(
    SCRIPT_DIR, f"orchestrator_log_{datetime.now().strftime('%Y-%m-%d')}.log"
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(_log_path, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("orchestrator")


# ============ TARGET LIST ============

_TICKER_RE = re.compile(r"\(([A-Z0-9\-\.]+)\)\s*$")


def _extract_ticker(entry: str):
    """Extract the ticker from a 'COMPANY NAME (TICKER)' entry."""
    if not entry:
        return None
    match = _TICKER_RE.search(entry)
    return match.group(1) if match else None


def load_target_list():
    """Return the list of 'NAME (TICKER)' entries, or None if missing/invalid."""
    if not os.path.exists(TARGET_LIST_PATH):
        return None
    try:
        with open(TARGET_LIST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"Could not parse target_company_list.json: {e}")
        return None
    if not isinstance(data, list) or not data:
        return None
    return [e for e in data if isinstance(e, str) and e.strip()]


# ============ OUTPUT-DIR SNAPSHOT ============

def snapshot_dir(abs_dir: str) -> dict:
    """
    Map every file under abs_dir to a sha256 of its contents. Content hashes
    (not mtimes) so an identical re-dump of a file does not read as progress.
    """
    snap = {}
    if not os.path.isdir(abs_dir):
        return snap
    for root, _, files in os.walk(abs_dir):
        for filename in files:
            file_path = os.path.join(root, filename)
            rel = os.path.relpath(file_path, abs_dir)
            try:
                with open(file_path, "rb") as f:
                    snap[rel] = hashlib.sha256(f.read()).hexdigest()
            except OSError:
                snap[rel] = "unreadable"
    return snap


# ============ COMPLETENESS CHECKS ============

def _safe_research_name(entry: str) -> str:
    """Replicate Stage 2's safe_filename transform for 'NAME (TICKER)' entries."""
    safe = (
        entry.replace(" ", "_")
        .replace("(", "")
        .replace(")", "")
        .replace(".", "")
        .replace("/", "-")
    )
    return f"{safe}_research.json"


def locate_company_file(output_dir: str, entry: str):
    """
    Find a company's research JSON. Tries the exact derived filename first,
    then falls back to matching the '_{TICKER}_research.json' suffix.
    """
    if not os.path.isdir(output_dir):
        return None
    exact = os.path.join(output_dir, _safe_research_name(entry))
    if os.path.exists(exact):
        return exact
    ticker = _extract_ticker(entry)
    if ticker:
        suffix = f"_{ticker}_research.json"
        try:
            for filename in os.listdir(output_dir):
                if filename.endswith(suffix):
                    return os.path.join(output_dir, filename)
        except OSError:
            return None
    return None


def _company_is_complete(data: dict, spec: dict) -> bool:
    """A company is complete if its terminal field (and any required numeric
    fields) are populated."""
    terminal = data.get(spec["terminal_field"])
    if not isinstance(terminal, str) or not terminal.strip():
        return False
    for numeric_field in spec.get("numeric_fields", []):
        if data.get(numeric_field) is None:
            return False
    return True


def check_fixed(spec: dict) -> dict:
    """Completeness for a fixed-output stage: are all required files present?"""
    output_dir = os.path.join(PROJECT_ROOT, spec["output_dir"])
    required = spec["required_files"]
    present, missing = [], []
    for filename in required:
        file_path = os.path.join(output_dir, filename)
        ok = os.path.exists(file_path)
        if ok and filename == "target_company_list.json":
            # Stage 1's key handoff must also be a non-empty list.
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    contents = json.load(f)
                ok = isinstance(contents, list) and len(contents) > 0
            except Exception:
                ok = False
        (present if ok else missing).append(filename)
    return {
        "ratio": 1.0 if not missing else 0.0,  # fixed stages are all-or-nothing
        "expected_count": len(required),
        "completed_count": len(present),
        "incomplete": missing,
        "complete_entries": [],
    }


def check_per_company(spec: dict, expected_entries: list) -> dict:
    """Completeness for a per-company stage: how many expected companies have
    the stage's terminal field populated?"""
    output_dir = os.path.join(PROJECT_ROOT, spec["output_dir"])
    complete, incomplete = [], []
    for entry in expected_entries:
        file_path = locate_company_file(output_dir, entry)
        if file_path is None:
            incomplete.append(entry)
            continue
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            incomplete.append(entry)
            continue
        if isinstance(data, dict) and _company_is_complete(data, spec):
            complete.append(entry)
        else:
            incomplete.append(entry)
    expected_count = len(expected_entries)
    ratio = (len(complete) / expected_count) if expected_count else 1.0
    return {
        "ratio": ratio,
        "expected_count": expected_count,
        "completed_count": len(complete),
        "incomplete": incomplete,
        "complete_entries": complete,
    }


def evaluate_completeness(spec: dict, expected_entries) -> dict:
    if spec["kind"] == "fixed":
        return check_fixed(spec)
    return check_per_company(spec, expected_entries or [])


# ============ STAGE SUBPROCESS ============

async def run_stage_subprocess(spec: dict):
    """
    Run a stage's main.py as a subprocess, streaming its output. Returns
    (exit_code, timed_out).
    """
    stage_dir = os.path.join(PROJECT_ROOT, spec["dir"])
    main_path = os.path.join(stage_dir, "main.py")
    if not os.path.exists(main_path):
        logger.error(f"[{spec['key']}] main.py not found at {main_path}")
        return None, False

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "main.py",
        cwd=stage_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=os.environ.copy(),
    )

    async def pump_output():
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip("\n")
            logger.info(f"[{spec['key']}] {text}")

    pump_task = asyncio.create_task(pump_output())
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=spec["timeout"])
    except asyncio.TimeoutError:
        timed_out = True
        logger.error(
            f"[{spec['key']}] exceeded timeout of {spec['timeout']}s — killing stage."
        )
        proc.kill()
        await proc.wait()

    await pump_task  # drain any buffered output
    return proc.returncode, timed_out


async def run_pruner() -> dict:
    """
    Run the Stage 5 pruner (runPrune.py --execute) as a subprocess, streaming
    its output. Returns a result dict with status "complete" or "halted".
    """
    runprune_path = os.path.join(PRUNER_DIR, "runPrune.py")
    if not os.path.exists(runprune_path):
        logger.error(f"[pruner] runPrune.py not found at {runprune_path}")
        return {"key": "pruner", "status": "halted", "returncode": None, "timed_out": False}

    logger.info("Running Stage 5 pruner (post Stage 1)...")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "runPrune.py",
        "--execute",
        cwd=PRUNER_DIR,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=os.environ.copy(),
    )

    async def pump_output():
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip("\n")
            logger.info(f"[pruner] {text}")

    pump_task = asyncio.create_task(pump_output())
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=PRUNER_TIMEOUT)
    except asyncio.TimeoutError:
        timed_out = True
        logger.error(f"[pruner] exceeded timeout of {PRUNER_TIMEOUT}s — killing pruner.")
        proc.kill()
        await proc.wait()

    await pump_task  # drain any buffered output
    returncode = proc.returncode
    status = "complete" if (returncode == 0 and not timed_out) else "halted"
    return {
        "key": "pruner",
        "status": status,
        "returncode": returncode,
        "timed_out": timed_out,
    }


# ============ PER-STAGE LOOP ============

async def run_one_stage(spec: dict, expected_entries) -> dict:
    """
    Run a stage, rerunning until its outputs are complete or it is stuck.
    Returns a per-stage result dict.
    """
    logger.info("=" * 70)
    logger.info(f"  {spec['label']}")
    logger.info("=" * 70)

    result = {
        "stage": spec["key"],
        "label": spec["label"],
        "attempts": 0,
        "expected_count": 0,
        "completed_count": 0,
        "completion_ratio": 0.0,
        "status": "unknown",
        "incomplete_companies": [],
        "complete_entries": [],
        "duration_seconds": 0.0,
        "timed_out": False,
        "last_exit_code": None,
    }
    started = time.time()
    no_progress_streak = 0

    def apply_threshold_gate(reason: str):
        ratio = result["completion_ratio"]
        if ratio >= COMPLETION_THRESHOLD:
            result["status"] = "continued_below_full"
            logger.warning(
                f"[{spec['key']}] {reason}. Completion {ratio:.1%} "
                f">= threshold {COMPLETION_THRESHOLD:.0%} — continuing. "
                f"Incomplete: {result['incomplete_companies']}"
            )
        else:
            result["status"] = "halted"
            logger.error(
                f"[{spec['key']}] {reason}. Completion {ratio:.1%} "
                f"< threshold {COMPLETION_THRESHOLD:.0%} — HALTING pipeline. "
                f"Incomplete: {result['incomplete_companies']}"
            )

    while True:
        result["attempts"] += 1
        attempt = result["attempts"]
        logger.info(f"[{spec['key']}] attempt {attempt}/{MAX_ATTEMPTS}")

        output_dir = os.path.join(PROJECT_ROOT, spec["output_dir"])
        before = snapshot_dir(output_dir)
        exit_code, timed_out = await run_stage_subprocess(spec)
        after = snapshot_dir(output_dir)

        progressed = before != after
        result["last_exit_code"] = exit_code
        result["timed_out"] = result["timed_out"] or timed_out

        completeness = evaluate_completeness(spec, expected_entries)
        result["expected_count"] = completeness["expected_count"]
        result["completed_count"] = completeness["completed_count"]
        result["completion_ratio"] = completeness["ratio"]
        result["incomplete_companies"] = completeness["incomplete"]
        result["complete_entries"] = completeness["complete_entries"]

        logger.info(
            f"[{spec['key']}] attempt {attempt}: exit={exit_code} "
            f"progressed={progressed} "
            f"complete={completeness['completed_count']}/{completeness['expected_count']} "
            f"({completeness['ratio']:.1%})"
        )

        if completeness["ratio"] >= 1.0:
            result["status"] = "complete"
            logger.info(f"[{spec['key']}] COMPLETE.")
            break

        if attempt >= MAX_ATTEMPTS:
            apply_threshold_gate(f"reached MAX_ATTEMPTS ({MAX_ATTEMPTS})")
            break

        if progressed:
            no_progress_streak = 0
        else:
            no_progress_streak += 1
            logger.info(
                f"[{spec['key']}] no progress this run "
                f"({no_progress_streak}/{NO_PROGRESS_TOLERANCE})"
            )
            if no_progress_streak >= NO_PROGRESS_TOLERANCE:
                apply_threshold_gate("no progress across retries (stuck)")
                break

    result["duration_seconds"] = round(time.time() - started, 1)
    return result


# ============ PUBLIC ENTRY POINT ============

async def run_pipeline(run_stage_1: bool = True) -> dict:
    """
    Run Stages 1-4 (or 2-4 when run_stage_1 is False) in order, gating
    progression on each stage's completeness. Returns a run-summary dict.
    """
    started_dt = datetime.now(timezone.utc)
    start = time.time()

    summary = {
        "started_at": started_dt.isoformat(),
        "finished_at": None,
        "duration_seconds": None,
        "run_stage_1": run_stage_1,
        "overall_status": "unknown",
        "halted_at_stage": None,
        "pruner": None,
        "stages": [],
    }

    logger.info("#" * 70)
    logger.info(f"#  Pipeline Orchestrator  (run_stage_1={run_stage_1})")
    logger.info(f"#  Started: {started_dt.isoformat()}")
    logger.info("#" * 70)

    stages_to_run = STAGE_SPECS if run_stage_1 else STAGE_SPECS[1:]

    # The per-company stages need the expected company set. When Stage 1 runs,
    # we read the target list after it completes; when skipped, it must already
    # exist.
    target_entries = None
    if not run_stage_1:
        target_entries = load_target_list()
        if target_entries is None:
            logger.error(
                f"--skip-stage-1 set but target_company_list.json is missing or "
                f"empty at {TARGET_LIST_PATH}. Run Stage 1 first."
            )
            summary["overall_status"] = "halted"
            summary["halted_at_stage"] = "stage_1"
            _finalize(summary, start)
            return summary

    # Tracks the expected set for the next per-company stage.
    next_per_company_expected = target_entries
    stage_2_completed = None

    for spec in stages_to_run:
        expected = next_per_company_expected if spec["kind"] == "per_company" else None
        stage_result = await run_one_stage(spec, expected)
        summary["stages"].append(stage_result)

        if stage_result["status"] == "halted":
            summary["overall_status"] = "halted"
            summary["halted_at_stage"] = spec["key"]
            logger.error(f"Pipeline halted at {spec['key']}.")
            break

        # After Stage 1, load the target list it produced.
        if spec["key"] == "stage_1":
            target_entries = load_target_list()
            if target_entries is None:
                summary["overall_status"] = "halted"
                summary["halted_at_stage"] = "stage_1"
                logger.error(
                    "Stage 1 reported complete but target_company_list.json is "
                    "missing or empty — halting."
                )
                break
            next_per_company_expected = target_entries

            # Stage 1 re-screened the universe — prune stale downstream outputs
            # for dropped tickers before the rest of the pipeline runs.
            pruner_result = await run_pruner()
            summary["pruner"] = pruner_result
            if pruner_result["status"] == "halted":
                summary["overall_status"] = "halted"
                summary["halted_at_stage"] = "pruner"
                logger.error("Pipeline halted: Stage 5 pruner failed.")
                break

        # Stage 3's expected set is the companies Stage 2 actually completed —
        # Stage 3 cannot complete a company Stage 2 never produced.
        if spec["key"] == "stage_2":
            stage_2_completed = stage_result["complete_entries"]
            next_per_company_expected = stage_2_completed

    if summary["overall_status"] != "halted":
        if any(s["status"] == "continued_below_full" for s in summary["stages"]):
            summary["overall_status"] = "completed_with_gaps"
        else:
            summary["overall_status"] = "complete"

    _finalize(summary, start)
    return summary


def _finalize(summary: dict, start: float) -> None:
    """Stamp timing, write the run-summary JSON, and print a summary table."""
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    summary["duration_seconds"] = round(time.time() - start, 1)

    # Strip the bulky complete_entries list from the persisted per-stage records.
    for stage in summary["stages"]:
        stage.pop("complete_entries", None)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    run_summary_path = os.path.join(SCRIPT_DIR, f"orchestrator_run_{ts}.json")
    try:
        with open(run_summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        logger.info(f"Run summary written to {run_summary_path}")
    except Exception as e:
        logger.error(f"Could not write run summary: {e}")

    logger.info("#" * 70)
    logger.info(f"#  Pipeline Orchestrator — {summary['overall_status'].upper()}")
    logger.info(f"#  Duration: {summary['duration_seconds']}s")
    for stage in summary["stages"]:
        logger.info(
            f"#   {stage['stage']:<9} {stage['status']:<22} "
            f"{stage['completed_count']}/{stage['expected_count']} "
            f"({stage['completion_ratio']:.0%}) "
            f"attempts={stage['attempts']} {stage['duration_seconds']}s"
        )
        if stage["incomplete_companies"]:
            logger.info(f"#     incomplete: {stage['incomplete_companies']}")
    if summary["halted_at_stage"]:
        logger.info(f"#  Halted at: {summary['halted_at_stage']}")
    logger.info("#" * 70)


# ============ CLI ============

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Stages 1-4 in order with completeness gating."
    )
    parser.add_argument(
        "--skip-stage-1",
        action="store_true",
        help="Skip Stage 1 and run Stages 2-4 against the existing "
        "target_company_list.json (used for targeted reruns).",
    )
    args = parser.parse_args()

    summary = asyncio.run(run_pipeline(run_stage_1=not args.skip_stage_1))
    sys.exit(1 if summary["overall_status"] == "halted" else 0)


if __name__ == "__main__":
    main()
