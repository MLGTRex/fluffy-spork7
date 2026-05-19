"""
Stage 5 — Continuous Monitoring Pipeline Orchestrator.

Cron-driven (eventually) or manual-loop mode. Reads schedule_config.json to
determine sub-stage cadences and schedule_state.json to track last_run
timestamps. On each invocation (or each loop iteration), checks what's due
and runs due sub-stages in priority order.

Currently in manual-loop mode (RUN_MODE='loop'). main.py is started manually
and runs until interrupted with Ctrl-C. Set RUN_MODE='single' to convert to
cron mode — main.py exits after one pass through due jobs.

Sub-stages are registered in SUB_STAGES below. Sub-stages whose scripts don't
exist yet are gracefully skipped with a "[Not yet implemented]" message so
this scheduler can run before any real sub-stage is built.

Fail-stop semantics: if a sub-stage fails (raises or sys.exit non-zero),
this iteration logs the failure but the scheduler loop continues. The failed
sub-stage's last_run is NOT updated, so it will retry on the next iteration.
"""

import os
import sys
import json
import time
import signal
import asyncio
import logging
import importlib.util
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()


# ============ RUN MODE ============

# 'loop'   — main.py runs forever, sleeps CHECK_INTERVAL_SECONDS between checks.
#            Use this for manual runs during the build/test phase.
# 'single' — main.py does one pass through due jobs and exits.
#            Use this for cron-driven mode in production.
RUN_MODE = "loop"

# Seconds between loop iterations in 'loop' mode. Default 300 (5 minutes) to
# match the eventual cron cadence. Lower this (e.g. 60) for faster feedback
# during testing.
CHECK_INTERVAL_SECONDS = 300


# ============ PATHS ============

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

SCHEDULE_CONFIG_PATH = os.path.join(SCRIPT_DIR, "schedule_config.json")
SCHEDULE_STATE_PATH = os.path.join(SCRIPT_DIR, "state", "schedule_state.json")
PORTFOLIO_STATE_PATH = os.path.join(SCRIPT_DIR, "state", "portfolio_state.json")
CONCURRENCY_STATE_PATH = os.path.join(SCRIPT_DIR, "state", "concurrency_state.json")
TIER_2_QUEUE_PATH = os.path.join(SCRIPT_DIR, "state", "tier_2_queue.json")
LOCKFILE_PATH = os.path.join(SCRIPT_DIR, "state", "lockfile")
PREDICTION_LOG_PATH = os.path.join(SCRIPT_DIR, "performance", "prediction_log.jsonl")

# Stage 4's consolidation portfolio — read-only reference for state initialization
STAGE4_OUTPUT_DIR = os.path.normpath(
    os.path.join(SCRIPT_DIR, "..", "Stage 4 DRAFT", "output")
)
CONSOLIDATION_PORTFOLIO_PATH = os.path.join(STAGE4_OUTPUT_DIR, "consolidation_portfolio.json")


# ============ SUB-STAGE REGISTRY ============

# Sub-stages are registered here as they're built. Only entries listed here
# can be invoked by the scheduler. Each entry maps the schedule_config key
# to its workflow script.
#
# Format: (schedule_key, folder_name, script_filename, is_async)
#
# To register a new sub-stage:
#   1. Build its sub-folder with workflow + functional scripts
#   2. Append an entry here
#   3. Confirm it's also defined in schedule_config.json
#
# Sub-stages in schedule_config.json but missing from this list are gracefully
# skipped during dispatch with a "[Not yet implemented]" log message.

SUB_STAGES = [
    # ("thesis_decomposition",   "thesis_decomposition", "runDecomposeThesis.py", True),

    # OLD hourly news-keyword watcher — SUPERSEDED by `monitor` below.
    # Preserved (commented out) for reference and quick rollback. State files
    # for this sub-stage (seen_headlines.json, threshold_counters.json,
    # daily_volatility_cache.json, intraday_volatility_cache.json) are
    # untouched on disk; uncommenting this line restores it. The matching
    # entry in schedule_config.json is also disabled (renamed with
    # _disabled_ prefix); restore both together if needed.
    # ("hourly_watcher",         "hourly_watcher",       "runWatchHeld.py",       True),

    # NEW price-led monitor. Runs at four specific times ET via the existing
    # `times_of_day` trigger (09:00, 10:00, 15:30, 16:30 ET).
    # runMonitor.py has a SYNC main() that internally calls asyncio.run(),
    # so it's registered with is_async=False — the scheduler runs it in a
    # worker thread via asyncio.to_thread(), and asyncio.run() inside that
    # thread creates its own event loop, which works cleanly.
    ("monitor",                "monitor",              "runMonitor.py",         False),

    # ("twice_daily_watcher",    "twice_daily_watcher",  "runWatchUnselected.py", True),
    # ("daily_run",              "daily_run",            "runDailyRun.py",        True),

    # Weekly full-pipeline rebuild. runWeeklyRun.py has an async main() that
    # invokes pipeline tools/orchestrator.py (Stages 1-4, incl. Stage 4's
    # Portfolio Execution sub-stage). Scheduled Saturday 00:00 UTC.
    ("weekly_run",             "weekly_run",           "runWeeklyRun.py",       True),
]


# ============ LOGGING ============

log_filename = os.path.join(
    SCRIPT_DIR, "logs", f"stage5_main_log_{datetime.now().strftime('%Y-%m-%d')}.log"
)
os.makedirs(os.path.dirname(log_filename), exist_ok=True)

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


# ============ SHUTDOWN FLAG ============

# Set to True by signal handler. Loop checks this after each iteration; if
# True, exits cleanly. In-flight sub-stages are allowed to complete; no new
# ones are started after Ctrl-C.
_shutdown_requested = False


def _handle_shutdown(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    print(f"\n[Scheduler] Shutdown requested (signal {signum}). Will exit after current iteration.")
    logger.info(f"Shutdown signal {signum} received; will exit after current iteration.")


# ============ LOCKFILE ============

def acquire_lockfile() -> bool:
    """
    Try to acquire the lockfile. Returns True if acquired, False if another
    instance is running. Writes current PID to the lockfile for diagnostics.
    """
    if os.path.exists(LOCKFILE_PATH):
        # Check if the PID in the lockfile is still alive
        try:
            with open(LOCKFILE_PATH, "r") as f:
                content = f.read().strip()
                old_pid = int(content.split("\n")[0])
            # Try sending signal 0 (no-op, just checks existence)
            try:
                os.kill(old_pid, 0)
                # Process is still alive — another main.py is running
                print(f"[Scheduler] Lockfile held by PID {old_pid}, which is still running. Exiting.")
                logger.warning(f"Lockfile held by PID {old_pid}; not starting.")
                return False
            except (OSError, ProcessLookupError):
                # PID not alive — stale lockfile from a previous crash. Take it over.
                print(f"[Scheduler] Stale lockfile found (PID {old_pid} not running). Taking over.")
                logger.info(f"Stale lockfile for PID {old_pid}; taking over.")
        except (ValueError, FileNotFoundError, IndexError):
            print(f"[Scheduler] Malformed lockfile found. Taking over.")
            logger.info(f"Malformed lockfile; taking over.")

    os.makedirs(os.path.dirname(LOCKFILE_PATH), exist_ok=True)
    with open(LOCKFILE_PATH, "w") as f:
        f.write(f"{os.getpid()}\n{datetime.now(timezone.utc).isoformat()}\n")
    return True


def release_lockfile() -> None:
    """Remove the lockfile on clean shutdown."""
    try:
        if os.path.exists(LOCKFILE_PATH):
            os.remove(LOCKFILE_PATH)
            logger.info("Lockfile released.")
    except Exception as e:
        logger.warning(f"Could not release lockfile: {e}")


# ============ DYNAMIC IMPORT ============

def _load_module(module_path: str, module_name: str):
    """Dynamically load a Python file as a module."""
    if not os.path.exists(module_path):
        raise FileNotFoundError(f"Sub-stage script not found: {module_path}")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not create spec for {module_path}")
    module = importlib.util.module_from_spec(spec)
    script_dir = os.path.dirname(module_path)
    sys.path.insert(0, script_dir)
    try:
        spec.loader.exec_module(module)
    finally:
        if sys.path and sys.path[0] == script_dir:
            sys.path.pop(0)
    return module


# ============ STATE INITIALIZATION (FIRST-RUN) ============

def _ensure_schedule_state(schedule_config: dict) -> dict:
    """
    Load schedule_state.json. If missing, initialize with last_run=None for
    every key in schedule_config (which makes every job immediately due).
    Returns the parsed state.
    """
    if os.path.exists(SCHEDULE_STATE_PATH):
        with open(SCHEDULE_STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        # Ensure every config key has a state entry (in case new jobs were added)
        for key in schedule_config:
            if key.startswith("_"):
                continue
            if key not in state:
                state[key] = {"last_run": None, "last_status": "never_run"}
        return state

    # First run — create with all entries unset
    state = {}
    for key in schedule_config:
        if key.startswith("_"):
            continue
        state[key] = {"last_run": None, "last_status": "never_run"}
    _write_schedule_state(state)
    logger.info(f"Initialized schedule_state.json with {len(state)} job entries (all due).")
    return state


def _write_schedule_state(state: dict) -> None:
    os.makedirs(os.path.dirname(SCHEDULE_STATE_PATH), exist_ok=True)
    with open(SCHEDULE_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=4)


def _load_consolidation_portfolio() -> dict:
    if not os.path.exists(CONSOLIDATION_PORTFOLIO_PATH):
        raise FileNotFoundError(
            f"consolidation_portfolio.json not found at {CONSOLIDATION_PORTFOLIO_PATH}. "
            f"Stage 5 requires a Stage 4 portfolio to monitor."
        )
    with open(CONSOLIDATION_PORTFOLIO_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _ensure_portfolio_state() -> dict:
    """
    Initialize portfolio_state.json from consolidation_portfolio.json if missing.
    The 15 positions become 'held'. Other top-50 candidates would be added by
    a future helper that reads Stage 1's target_company_list.json — for now,
    only the held 15 are tracked here.
    """
    if os.path.exists(PORTFOLIO_STATE_PATH):
        with open(PORTFOLIO_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    portfolio = _load_consolidation_portfolio()
    positions = (portfolio.get("portfolio") or {}).get("positions", [])
    state = {
        "initialized_at": datetime.now(timezone.utc).isoformat(),
        "source_consolidation_date": portfolio.get("consolidation_date"),
        "positions": {}
    }
    for p in positions:
        ticker = p.get("ticker")
        if not ticker:
            continue
        state["positions"][ticker] = {
            "status": "held",
            "status_changed_at": datetime.now(timezone.utc).isoformat(),
            "allocation_pct": p.get("allocation_pct"),
            "sector": p.get("sector"),
        }

    os.makedirs(os.path.dirname(PORTFOLIO_STATE_PATH), exist_ok=True)
    with open(PORTFOLIO_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=4)
    logger.info(f"Initialized portfolio_state.json with {len(state['positions'])} held positions.")
    return state


def _ensure_concurrency_state() -> dict:
    if os.path.exists(CONCURRENCY_STATE_PATH):
        with open(CONCURRENCY_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    state = {
        "initialized_at": datetime.now(timezone.utc).isoformat(),
        "active_reevaluations": [],
        "daily_initiated_count": 0,
        "daily_counter_date": datetime.now(timezone.utc).date().isoformat(),
    }
    os.makedirs(os.path.dirname(CONCURRENCY_STATE_PATH), exist_ok=True)
    with open(CONCURRENCY_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=4)
    logger.info("Initialized concurrency_state.json.")
    return state


def _ensure_tier_2_queue() -> list:
    if os.path.exists(TIER_2_QUEUE_PATH):
        with open(TIER_2_QUEUE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    queue = []
    os.makedirs(os.path.dirname(TIER_2_QUEUE_PATH), exist_ok=True)
    with open(TIER_2_QUEUE_PATH, "w", encoding="utf-8") as f:
        json.dump(queue, f, indent=4)
    logger.info("Initialized tier_2_queue.json (empty).")
    return queue


def _ensure_prediction_log() -> None:
    """
    Initialize prediction_log.jsonl with one entry per currently held position
    if the file doesn't exist. This captures the baseline predictions for
    performance tracking.
    """
    if os.path.exists(PREDICTION_LOG_PATH):
        return

    portfolio = _load_consolidation_portfolio()
    positions = (portfolio.get("portfolio") or {}).get("positions", [])
    entry_date = portfolio.get("consolidation_date", datetime.now(timezone.utc).date().isoformat())

    os.makedirs(os.path.dirname(PREDICTION_LOG_PATH), exist_ok=True)
    with open(PREDICTION_LOG_PATH, "w", encoding="utf-8") as f:
        for p in positions:
            entry = {
                "ticker": p.get("ticker"),
                "entry_date": entry_date,
                "entry_source": "initial_load",
                "allocation_pct": p.get("allocation_pct"),
                "sector": p.get("sector"),
                "expected_return_12m": p.get("expected_return_12m"),
                "rationale": p.get("rationale"),
                "logged_at": datetime.now(timezone.utc).isoformat(),
            }
            f.write(json.dumps(entry) + "\n")
    logger.info(f"Initialized prediction_log.jsonl with {len(positions)} baseline entries.")


def initialize_state_if_needed(schedule_config: dict) -> None:
    """Single entry point for first-run state initialization."""
    _ensure_schedule_state(schedule_config)
    _ensure_portfolio_state()
    _ensure_concurrency_state()
    _ensure_tier_2_queue()
    _ensure_prediction_log()


# ============ SCHEDULE CONFIG ============

def load_schedule_config() -> dict:
    if not os.path.exists(SCHEDULE_CONFIG_PATH):
        raise FileNotFoundError(f"schedule_config.json not found at {SCHEDULE_CONFIG_PATH}")
    with open(SCHEDULE_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ============ "IS DUE?" LOGIC ============

def _parse_iso(s: str) -> datetime:
    """Parse an ISO timestamp, accepting both Z and +00:00 forms. Returns tz-aware UTC."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _most_recent_scheduled(job_config: dict, now_utc: datetime) -> datetime:
    """
    Compute the most recent scheduled time for a job, relative to now_utc.
    Returns a tz-aware UTC datetime.

    For 'on_summary_change', returns epoch — these jobs are "always due if
    the source has changed"; sub-stage handles that internally.
    """
    trigger = job_config.get("trigger")

    if trigger == "on_summary_change":
        # Always considered due — the sub-stage's own freshness check decides
        # whether work is actually needed. We return epoch so any non-None
        # last_run satisfies "last_run >= scheduled" only if the sub-stage
        # explicitly updated it.
        return datetime(1970, 1, 1, tzinfo=timezone.utc)

    if trigger == "anchored_interval":
        interval_minutes = int(job_config.get("interval_minutes", 60))
        anchor = job_config.get("anchor", "top_of_hour")
        if anchor == "top_of_hour":
            return now_utc.replace(minute=0, second=0, microsecond=0)
        # Fallback: rolling interval — most recent multiple of N minutes
        # before now, anchored to midnight UTC.
        midnight = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        minutes_since_midnight = int((now_utc - midnight).total_seconds() // 60)
        slots = minutes_since_midnight // interval_minutes
        return midnight + timedelta(minutes=slots * interval_minutes)

    if trigger == "time_of_day":
        time_str = job_config.get("time", "00:00")
        tz_name = job_config.get("timezone", "UTC")
        tz = ZoneInfo(tz_name)
        now_local = now_utc.astimezone(tz)
        hh, mm = [int(x) for x in time_str.split(":")]
        scheduled_today_local = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if scheduled_today_local > now_local:
            # Today's slot hasn't happened yet — most recent is yesterday's
            scheduled_local = scheduled_today_local - timedelta(days=1)
        else:
            scheduled_local = scheduled_today_local
        return scheduled_local.astimezone(timezone.utc)

    if trigger == "times_of_day":
        times = job_config.get("times", [])
        tz_name = job_config.get("timezone", "UTC")
        tz = ZoneInfo(tz_name)
        now_local = now_utc.astimezone(tz)
        # Find most recent time among today's slots and yesterday's last slot
        candidates = []
        for t in times:
            hh, mm = [int(x) for x in t.split(":")]
            today_local = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if today_local <= now_local:
                candidates.append(today_local)
            else:
                # Yesterday's version of this slot
                candidates.append(today_local - timedelta(days=1))
        most_recent_local = max(candidates)
        return most_recent_local.astimezone(timezone.utc)

    if trigger == "day_of_week_time":
        day_name = job_config.get("day", "saturday").lower()
        time_str = job_config.get("time", "00:00")
        tz_name = job_config.get("timezone", "UTC")
        tz = ZoneInfo(tz_name)
        now_local = now_utc.astimezone(tz)
        days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        target_dow = days.index(day_name)
        hh, mm = [int(x) for x in time_str.split(":")]
        # Most recent occurrence of (day_of_week, time) at or before now
        current_dow = now_local.weekday()
        days_back = (current_dow - target_dow) % 7
        candidate = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0) - timedelta(days=days_back)
        if candidate > now_local:
            candidate -= timedelta(days=7)
        return candidate.astimezone(timezone.utc)

    # Unknown trigger type — never due (logged for visibility)
    logger.warning(f"Unknown trigger type '{trigger}' in schedule_config; treating as never due.")
    return datetime(9999, 12, 31, tzinfo=timezone.utc)


def is_due(job_key: str, job_config: dict, schedule_state: dict, now_utc: datetime) -> tuple:
    """
    Returns (due: bool, reason: str).
    A job is due if its last_run is None (never run) or older than the most
    recent scheduled time.
    """
    state_entry = schedule_state.get(job_key, {})
    last_run_str = state_entry.get("last_run")
    scheduled = _most_recent_scheduled(job_config, now_utc)

    if last_run_str is None:
        return True, f"never run (scheduled: {scheduled.isoformat()})"

    try:
        last_run = _parse_iso(last_run_str)
    except Exception as e:
        return True, f"could not parse last_run '{last_run_str}': {e}"

    if last_run < scheduled:
        return True, f"last_run {last_run.isoformat()} < scheduled {scheduled.isoformat()}"
    return False, f"last_run {last_run.isoformat()} >= scheduled {scheduled.isoformat()}"


# ============ SUB-STAGE DISPATCH ============

async def run_sub_stage(job_key: str, folder_name: str, script_filename: str, is_async: bool) -> bool:
    """
    Load and invoke a sub-stage's workflow script. Returns True on success.
    """
    print(f"\n{'='*70}")
    print(f"  Stage 5 — {job_key}")
    print(f"{'='*70}\n")

    script_path = os.path.join(SCRIPT_DIR, folder_name, script_filename)
    module_name = f"stage5_{folder_name.replace(' ', '_').lower()}_{script_filename.replace('.py', '')}"

    start_time = time.time()

    try:
        module = _load_module(script_path, module_name)
    except FileNotFoundError as e:
        logger.error(f"[{job_key}] {e}")
        print(f"\n[FAIL] {job_key}: script not found — {e}")
        return False
    except Exception as e:
        logger.error(f"[{job_key}] Failed to import {script_path}: {e}")
        print(f"\n[FAIL] {job_key}: import error — {e}")
        return False

    if not hasattr(module, "main"):
        logger.error(f"[{job_key}] Module has no main() function")
        print(f"\n[FAIL] {job_key}: no main() function in {script_filename}")
        return False

    try:
        if is_async:
            await module.main()
        else:
            await asyncio.to_thread(module.main)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        elapsed = time.time() - start_time
        if code != 0:
            logger.error(f"[{job_key}] exited with code {code} after {elapsed:.1f}s")
            print(f"\n[FAIL] {job_key} exited with code {code} after {elapsed:.1f}s")
            return False
        logger.info(f"[{job_key}] exited cleanly after {elapsed:.1f}s")
        return True
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(f"[{job_key}] raised exception after {elapsed:.1f}s: {e}")
        logger.exception(f"[{job_key}] traceback:")
        print(f"\n[FAIL] {job_key} raised exception after {elapsed:.1f}s: {e}")
        return False

    elapsed = time.time() - start_time
    print(f"\n[OK] {job_key} complete in {elapsed:.1f}s.")
    logger.info(f"[{job_key}] complete in {elapsed:.1f}s")
    return True


# ============ ONE SCHEDULER PASS ============

async def run_due_jobs() -> None:
    """
    One pass through the scheduler:
      1. Load schedule config and state
      2. Compute which jobs are due
      3. Run them in priority order (one per pass, in priority order)
         — actually all due jobs in this pass, ordered by priority
      4. Update last_run on success
    """
    schedule_config = load_schedule_config()
    schedule_state = _ensure_schedule_state(schedule_config)

    now_utc = datetime.now(timezone.utc)

    # Filter out comment keys and compute due-ness for each job
    jobs = []
    for key, cfg in schedule_config.items():
        if key.startswith("_"):
            continue
        due, reason = is_due(key, cfg, schedule_state, now_utc)
        jobs.append({
            "key": key,
            "config": cfg,
            "due": due,
            "reason": reason,
            "priority": cfg.get("priority", 999),
        })

    due_jobs = sorted([j for j in jobs if j["due"]], key=lambda j: j["priority"])
    not_due_jobs = [j for j in jobs if not j["due"]]

    print(f"\n[Scheduler] Pass at {now_utc.isoformat()}")
    print(f"[Scheduler] Due jobs ({len(due_jobs)}): {[j['key'] for j in due_jobs]}")
    print(f"[Scheduler] Not due ({len(not_due_jobs)}): {[j['key'] for j in not_due_jobs]}")

    if not due_jobs:
        print("[Scheduler] Nothing to do this pass.")
        return

    # Build registry lookup
    sub_stage_lookup = {entry[0]: entry for entry in SUB_STAGES}

    for job in due_jobs:
        if _shutdown_requested:
            print("[Scheduler] Shutdown requested mid-pass; stopping job dispatch.")
            return

        key = job["key"]

        if key not in sub_stage_lookup:
            print(f"\n[Scheduler] {key}: [Not yet implemented] — skipping. "
                  f"(Listed in schedule_config but no SUB_STAGES entry yet.)")
            logger.info(f"[{key}] not yet implemented; skipped.")
            continue

        _, folder_name, script_filename, is_async = sub_stage_lookup[key]
        run_start = datetime.now(timezone.utc)
        ok = await run_sub_stage(key, folder_name, script_filename, is_async)
        run_end = datetime.now(timezone.utc)

        if ok:
            schedule_state[key] = {
                "last_run": run_end.isoformat(),
                "last_status": "success",
                "started_at": run_start.isoformat(),
            }
            _write_schedule_state(schedule_state)
        else:
            # Don't update last_run on failure — will retry next pass.
            # Do record the attempt for visibility.
            schedule_state[key] = {
                **schedule_state.get(key, {}),
                "last_status": "failed",
                "last_attempt": run_end.isoformat(),
            }
            _write_schedule_state(schedule_state)
            print(f"[Scheduler] {key} failed; last_run not updated (will retry next pass).")


# ============ MAIN ============

async def main():
    print(f"\n{'#'*70}")
    print(f"#  Stage 5 — Continuous Monitoring Scheduler")
    print(f"#  Mode: {RUN_MODE}")
    if RUN_MODE == "loop":
        print(f"#  Check interval: {CHECK_INTERVAL_SECONDS}s")
    print(f"#  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"#  PID: {os.getpid()}")
    print(f"{'#'*70}")

    if not acquire_lockfile():
        sys.exit(1)

    # Install signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    try:
        # First-run initialization
        schedule_config = load_schedule_config()
        initialize_state_if_needed(schedule_config)

        if RUN_MODE == "single":
            await run_due_jobs()
        elif RUN_MODE == "loop":
            iteration = 0
            while not _shutdown_requested:
                iteration += 1
                print(f"\n[Scheduler] === Iteration {iteration} ===")
                try:
                    await run_due_jobs()
                except Exception as e:
                    logger.exception(f"Unhandled error in run_due_jobs(): {e}")
                    print(f"[Scheduler] Unhandled error: {e}. Continuing.")

                if _shutdown_requested:
                    break

                # Sleep in small chunks so Ctrl-C is responsive
                sleep_remaining = CHECK_INTERVAL_SECONDS
                while sleep_remaining > 0 and not _shutdown_requested:
                    chunk = min(1.0, sleep_remaining)
                    await asyncio.sleep(chunk)
                    sleep_remaining -= chunk
        else:
            raise ValueError(f"Unknown RUN_MODE: {RUN_MODE}")

    finally:
        release_lockfile()
        print(f"\n{'#'*70}")
        print(f"#  Stage 5 Scheduler exiting at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'#'*70}")


if __name__ == "__main__":
    asyncio.run(main())