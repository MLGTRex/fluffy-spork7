"""
Pipeline Invoker (library module).

Shared utility for Stage 5 sub-stages (trigger handler, daily run, weekly run)
to invoke the Stage 2/3/4 pipeline against a targeted list of companies.

Mechanism:
    1. Acquire the invoker lockfile (prevents simultaneous invocations from
       corrupting target_company_list.json).
    2. Back up current target_company_list.json (in Stage 1's output dir) and
       consolidation_portfolio.json to /Stage 5 DRAFT/backups/pre_invocation/{timestamp}/.
    3. Filter Stage 1's full list to entries matching the requested tickers,
       preserving the original "COMPANY NAME (TICKER)" format unchanged.
    4. Write the narrowed list to Stage 1's output location.
    5. Invoke Stage 2 → Stage 3 → Stage 4 sequentially via dynamic import.
       Fail-stop: any stage failure aborts the chain.
    6. On success, update Stage 5's monitor evaluation anchors file using
       prices from Stage 3's per-company JSONs. This lets the Stage 5 monitor
       compute "cumulative move since last evaluation" against the price at
       which the thesis was actually built.
    7. In a finally block: restore target_company_list.json to its
       pre-invocation contents. Always. Even on success.
    8. Release the lockfile.

Returns a structured result dict; callers decide what to log or persist.

Invoker does NOT inspect stage outputs for correctness — exit code 0 from a
stage is taken as success. Output validation is a future per-stage verification
concern.

Usage:
    from pipeline_invoker.invokePipeline import invoke_pipeline
    result = await invoke_pipeline(["ARDX", "BIP"])
    if result["status"] == "success":
        ...
"""

import os
import sys
import json
import time
import asyncio
import logging
import tempfile
import importlib.util
import re
import shutil
from datetime import datetime, timezone


# ============ TESTING OVERRIDE ============

# When OVERRIDE_TARGETS is True, the invoker ignores the caller's ticker list
# entirely and instead uses OVERRIDE_TARGET_ENTRIES as the narrowed target list.
#
# This exists for testing: it lets you run the pipeline with tickers that
# aren't in Stage 1's current target list (e.g., to see how the portfolio
# behaves with a hand-picked test ticker) without modifying Stage 1's output
# or any other canonical pipeline state.
#
# OVERRIDE_TARGET_ENTRIES must contain FULL ENTRIES in the format
# "COMPANY NAME (TICKER)" — the same format Stage 1 produces and Stage 2
# expects. Bare tickers will not work; Stage 2 needs the full string.
#
# When override mode is active, the standard "ticker must be in Stage 1's list"
# validation is bypassed. The entries get written directly to Stage 1's target
# list location, the pipeline runs, and the restore-on-exit step still puts
# Stage 1's original list back. Test ticker outputs land in Stage 2/3/4 dirs;
# the next pruner run moves them to backup.
#
# IMPORTANT: leave OVERRIDE_TARGETS = False in production. The invoker logs
# loudly when override mode is active so you don't accidentally leave it on.

OVERRIDE_TARGETS = False
OVERRIDE_TARGET_ENTRIES = [
    # "FORTESCUE METALS GROUP LTD (FWD.AX)",
    # "ANGLOGOLD ASHANTI PLC (ANG.AX)",
]


# ============ PATHS ============

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STAGE5_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
PROJECT_ROOT = os.path.normpath(os.path.join(STAGE5_ROOT, ".."))

TARGET_LIST_PATH = os.path.join(PROJECT_ROOT, "Stage 1 DRAFT", "output", "target_company_list.json")
CONSOLIDATION_PORTFOLIO_PATH = os.path.join(
    PROJECT_ROOT, "Stage 4 DRAFT", "output", "consolidation_portfolio.json"
)

ORCHESTRATOR_PATH = os.path.join(PROJECT_ROOT, "pipeline tools", "orchestrator.py")

STAGE_3_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "Stage 3 DRAFT", "output")

INVOKER_LOCKFILE_PATH = os.path.join(STAGE5_ROOT, "state", "pipeline_invoker.lock")
BACKUPS_ROOT = os.path.join(STAGE5_ROOT, "backups", "pre_invocation")
LOGS_DIR = os.path.join(STAGE5_ROOT, "logs")

# Stage 5 monitor's evaluation anchors file. Owned by the monitor; written
# here ONLY after a successful rerun, to record what price the thesis was
# (re)built at for that ticker. The monitor reads this to compute cumulative
# drift since last evaluation.
MONITOR_EVALUATION_ANCHORS_PATH = os.path.join(
    STAGE5_ROOT, "monitor", "state", "ticker_evaluation_anchors.json"
)


# ============ LOGGING ============

os.makedirs(LOGS_DIR, exist_ok=True)
_log_filename = os.path.join(
    LOGS_DIR, f"pipeline_invoker_{datetime.now().strftime('%Y-%m-%d')}.log"
)
_logger_configured = False


def _ensure_logger() -> logging.Logger:
    """Lazy logger setup so importing the module doesn't pollute logs."""
    global _logger_configured
    logger = logging.getLogger("pipeline_invoker")
    if not _logger_configured:
        # Avoid duplicate handlers if invoker is imported multiple times
        if not logger.handlers:
            handler = logging.FileHandler(_log_filename, encoding="utf-8")
            handler.setFormatter(logging.Formatter(
                "%(asctime)s | %(levelname)-7s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S"
            ))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
        _logger_configured = True
    return logger


# ============ TICKER EXTRACTION ============

_TICKER_RE = re.compile(r"\(([A-Z0-9\-\.]+)\)\s*$")


def _extract_ticker(entry: str) -> str:
    """Extract the ticker symbol from a 'COMPANY NAME (TICKER)' entry."""
    if not entry:
        return None
    match = _TICKER_RE.search(entry)
    return match.group(1) if match else None


# ============ LOCKFILE ============

def _acquire_invoker_lock() -> bool:
    """
    Try to acquire the pipeline invoker lockfile.
    Returns True if acquired, False if another invocation is in flight.

    Defense-in-depth against simultaneous invocations corrupting the
    shared target_company_list.json. Trigger handler's concurrency cap
    is the primary defense; this is the backup.
    """
    logger = _ensure_logger()
    if os.path.exists(INVOKER_LOCKFILE_PATH):
        try:
            with open(INVOKER_LOCKFILE_PATH, "r") as f:
                content = f.read().strip()
                old_pid = int(content.split("\n")[0])
            try:
                os.kill(old_pid, 0)
                # Process still alive — another invocation in flight
                logger.warning(f"Invoker lockfile held by PID {old_pid}; refusing to start.")
                return False
            except (OSError, ProcessLookupError):
                # Stale lock — take it over
                logger.info(f"Stale invoker lockfile for PID {old_pid}; taking over.")
        except (ValueError, FileNotFoundError, IndexError):
            logger.info("Malformed invoker lockfile; taking over.")

    os.makedirs(os.path.dirname(INVOKER_LOCKFILE_PATH), exist_ok=True)
    with open(INVOKER_LOCKFILE_PATH, "w") as f:
        f.write(f"{os.getpid()}\n{datetime.now(timezone.utc).isoformat()}\n")
    return True


def _release_invoker_lock() -> None:
    """Remove the invoker lockfile."""
    logger = _ensure_logger()
    try:
        if os.path.exists(INVOKER_LOCKFILE_PATH):
            os.remove(INVOKER_LOCKFILE_PATH)
    except Exception as e:
        logger.warning(f"Could not release invoker lockfile: {e}")


# ============ OVERRIDE MODE ============

def _validate_override_config() -> tuple:
    """
    Sanity-check the OVERRIDE_TARGETS / OVERRIDE_TARGET_ENTRIES configuration.
    Only called when OVERRIDE_TARGETS is True.

    Returns (valid: bool, error: str or None, entries: list or None).
    """
    if not isinstance(OVERRIDE_TARGET_ENTRIES, list):
        return False, (
            f"OVERRIDE_TARGETS is True but OVERRIDE_TARGET_ENTRIES is not a list "
            f"(got {type(OVERRIDE_TARGET_ENTRIES).__name__})."
        ), None

    if not OVERRIDE_TARGET_ENTRIES:
        return False, (
            "OVERRIDE_TARGETS is True but OVERRIDE_TARGET_ENTRIES is empty. "
            "Either set OVERRIDE_TARGETS = False, or add at least one full entry "
            "in 'COMPANY NAME (TICKER)' format."
        ), None

    # Check each entry is a string in the expected format
    bad_entries = []
    for entry in OVERRIDE_TARGET_ENTRIES:
        if not isinstance(entry, str):
            bad_entries.append(f"non-string entry: {entry!r}")
            continue
        ticker = _extract_ticker(entry)
        if not ticker:
            bad_entries.append(
                f"entry has no extractable ticker (expected 'NAME (TICKER)' format): {entry!r}"
            )

    if bad_entries:
        return False, (
            f"OVERRIDE_TARGET_ENTRIES contains malformed entries:\n  - "
            + "\n  - ".join(bad_entries)
        ), None

    # Dedup while preserving order (in case of accidental duplicates in the config)
    seen = set()
    deduped = []
    for entry in OVERRIDE_TARGET_ENTRIES:
        if entry not in seen:
            seen.add(entry)
            deduped.append(entry)

    return True, None, deduped


def _write_override_target_list(override_entries: list) -> None:
    """
    Write the override entries directly to Stage 1's target_company_list.json
    location, bypassing the normal narrowing-from-Stage-1 step.

    Override mode injects entries that may not exist in Stage 1's current list,
    so we don't filter — we just write the entries verbatim.
    """
    logger = _ensure_logger()
    with open(TARGET_LIST_PATH, "w", encoding="utf-8") as f:
        json.dump(override_entries, f, indent=2, ensure_ascii=False)
    logger.warning(
        f"OVERRIDE MODE: wrote {len(override_entries)} override entries to "
        f"target_company_list.json (bypassed normal narrowing): {override_entries}"
    )


# ============ INPUT VALIDATION ============

def _validate_inputs(targeted_tickers: list) -> tuple:
    """
    Pre-flight checks on the requested tickers and on Stage 1's target list.

    Returns (valid: bool, error: str or None, parsed_target_list: list or None,
             deduped_tickers: list or None,
             resolved_entries: dict {ticker: entry} or None).

    Hard errors:
      - Empty targeted list (caller bug)
      - target_company_list.json missing
      - A requested ticker that is neither in Stage 1's list nor present in
        Stage 3's research output (nothing to rerun)

    Silent dedup:
      - Duplicate tickers in the requested list

    Tickers not in Stage 1's list but present in Stage 3's research output are
    accepted: their target-list entry is reconstructed from Stage 3.
    """
    logger = _ensure_logger()

    if not targeted_tickers:
        return False, "Empty target list passed to invoker — likely a caller bug", None, None, None

    # Dedup while preserving order
    seen = set()
    deduped = []
    duplicate_count = 0
    for t in targeted_tickers:
        if t in seen:
            duplicate_count += 1
            continue
        seen.add(t)
        deduped.append(t)
    if duplicate_count > 0:
        logger.info(f"Deduplicated {duplicate_count} duplicate ticker(s) from request.")

    if not os.path.exists(TARGET_LIST_PATH):
        return False, (
            f"Stage 1's target_company_list.json not found at {TARGET_LIST_PATH}. "
            f"Run Stage 1 first."
        ), None, None, None

    try:
        with open(TARGET_LIST_PATH, "r", encoding="utf-8") as f:
            current_list = json.load(f)
    except Exception as e:
        return False, f"Could not parse target_company_list.json: {e}", None, None, None

    if not isinstance(current_list, list):
        return False, (
            f"target_company_list.json is not a list (got {type(current_list).__name__})"
        ), None, None, None

    # Build a map of ticker → original entry from Stage 1's list
    existing_tickers = {}
    for entry in current_list:
        if not isinstance(entry, str):
            continue
        ticker = _extract_ticker(entry)
        if ticker:
            existing_tickers[ticker] = entry

    # Resolve a full "COMPANY NAME (TICKER)" entry for every requested ticker.
    # Tickers already in Stage 1's list use that entry as-is; tickers outside it
    # (e.g. a hand-picked test ticker) are reconstructed from their Stage 3
    # research file, so a targeted rerun works for any monitored company.
    resolved_entries = {}
    unresolvable = []
    for t in deduped:
        entry = existing_tickers.get(t) or _entry_from_stage_3(t)
        if entry:
            resolved_entries[t] = entry
        else:
            unresolvable.append(t)
    if unresolvable:
        return False, (
            f"Requested ticker(s) could not be resolved: {unresolvable}. They are "
            f"neither in Stage 1's target list nor present in Stage 3's research "
            f"output, so there is nothing to rerun."
        ), None, None, None

    return True, None, current_list, deduped, resolved_entries


# ============ BACKUP ============

def _backup_pre_invocation(timestamp_str: str, current_target_list: list) -> str:
    """
    Snapshot target_company_list.json and consolidation_portfolio.json to
    /Stage 5 DRAFT/backups/pre_invocation/{timestamp}/.

    Returns the backup directory path.
    """
    logger = _ensure_logger()
    backup_dir = os.path.join(BACKUPS_ROOT, timestamp_str)
    os.makedirs(backup_dir, exist_ok=True)

    # target_company_list.json — write from in-memory contents so we know exactly
    # what we'll restore even if the file is mutated externally during the run.
    target_backup = os.path.join(backup_dir, "target_company_list.json")
    with open(target_backup, "w", encoding="utf-8") as f:
        json.dump(current_target_list, f, indent=2, ensure_ascii=False)

    # consolidation_portfolio.json — copy file if it exists
    if os.path.exists(CONSOLIDATION_PORTFOLIO_PATH):
        portfolio_backup = os.path.join(backup_dir, "consolidation_portfolio.json")
        shutil.copy2(CONSOLIDATION_PORTFOLIO_PATH, portfolio_backup)
        logger.info(f"Backed up consolidation_portfolio.json to {portfolio_backup}")
    else:
        logger.info("consolidation_portfolio.json does not exist yet — nothing to back up.")

    logger.info(f"Backed up target_company_list.json ({len(current_target_list)} entries) to {target_backup}")
    return backup_dir


def _restore_target_list(current_target_list: list) -> bool:
    """Restore target_company_list.json to its pre-invocation contents."""
    logger = _ensure_logger()
    try:
        with open(TARGET_LIST_PATH, "w", encoding="utf-8") as f:
            json.dump(current_target_list, f, indent=2, ensure_ascii=False)
        logger.info(f"Restored target_company_list.json to {len(current_target_list)} entries.")
        return True
    except Exception as e:
        logger.error(f"FAILED to restore target_company_list.json: {e}")
        return False


# ============ NARROWING ============

def _write_narrowed_target_list(deduped_tickers: list, resolved_entries: dict) -> list:
    """
    Write a narrowed target_company_list.json with one 'COMPANY NAME (TICKER)'
    entry per requested ticker. Entries come from resolved_entries, which
    _validate_inputs built from Stage 1's list (for tickers already in it) and
    from Stage 3 research files (for tickers outside it).

    Returns the narrowed list that was written.
    """
    logger = _ensure_logger()

    narrowed = [resolved_entries[t] for t in deduped_tickers if t in resolved_entries]

    with open(TARGET_LIST_PATH, "w", encoding="utf-8") as f:
        json.dump(narrowed, f, indent=2, ensure_ascii=False)
    logger.info(
        f"Wrote narrowed target_company_list.json with {len(narrowed)} entries: {narrowed}"
    )
    return narrowed


# ============ EVALUATION ANCHOR UPDATE ============

def _find_stage_3_file_for_ticker(ticker: str) -> str:
    """
    Stage 3 files are named like 'COMPANY_NAME_TICKER_research.json' with
    underscores separating each part. We don't know the company name from the
    ticker alone, so we list the directory and look for files ending with
    '_{TICKER}_research.json'.

    Returns the path if found, None otherwise.
    """
    if not os.path.isdir(STAGE_3_OUTPUT_DIR):
        return None

    suffix = f"_{ticker}_research.json"
    try:
        for filename in os.listdir(STAGE_3_OUTPUT_DIR):
            if filename.endswith(suffix):
                return os.path.join(STAGE_3_OUTPUT_DIR, filename)
    except OSError:
        return None
    return None


def _entry_from_stage_3(ticker: str) -> str:
    """
    Reconstruct a 'COMPANY NAME (TICKER)' target-list entry for a ticker that is
    not in target_company_list.json, by reading company_name from its Stage 3
    research JSON. The monitor only reruns tickers from the universe it watches,
    which is derived from Stage 3's output dir — so any such ticker has a file
    here. Returns None if no Stage 3 file or no usable company name.
    """
    path = _find_stage_3_file_for_ticker(ticker)
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    name = data.get("company_name")
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    if _extract_ticker(name) == ticker:
        return name
    return f"{name} ({ticker})"


def _read_price_from_stage_3(ticker: str) -> tuple:
    """
    Read current_price and current_price_date from this ticker's Stage 3
    per-company JSON file.

    Returns (price: float or None, price_date: str or None, error: str or None).
    """
    stage_3_path = _find_stage_3_file_for_ticker(ticker)
    if stage_3_path is None:
        return None, None, f"Stage 3 research file not found for ticker {ticker}"

    try:
        with open(stage_3_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return None, None, f"Stage 3 file for {ticker} is not valid JSON: {e}"
    except OSError as e:
        return None, None, f"Could not read Stage 3 file for {ticker}: {e}"

    if not isinstance(data, dict):
        return None, None, f"Stage 3 file for {ticker} is not a JSON object"

    price = data.get("current_price")
    price_date = data.get("current_price_date")

    if price is None:
        return None, None, f"Stage 3 file for {ticker} has no 'current_price' field"
    if not isinstance(price, (int, float)):
        return None, None, (
            f"Stage 3 file for {ticker} has non-numeric current_price: {price!r}"
        )

    return float(price), price_date, None


def _atomic_write_json(path: str, data) -> None:
    """Write JSON to path atomically (temp file + rename)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    parent_dir = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".evaluation_anchors.", suffix=".tmp", dir=parent_dir
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


def _update_evaluation_anchors(tickers: list) -> dict:
    """
    Update the Stage 5 monitor's evaluation anchors file for the tickers that
    were just successfully reran by this invocation.

    For each ticker:
      - Read current_price and current_price_date from its Stage 3 file
      - Update (or create) the anchor entry with that price + today's UTC
        timestamp as evaluated_at

    The anchors file preserves entries for tickers NOT in this update — only
    the rerun tickers' anchors are touched. New tickers are added; existing
    tickers' anchors are overwritten with the fresh price.

    Atomically writes the merged result.

    Args:
        tickers: list of ticker symbols that just had a successful rerun

    Returns dict:
        {
            "anchors_updated": [tickers successfully updated],
            "anchors_failed": [{ticker, error} for each failure],
            "anchors_path": str,
            "anchors_total_after": int,
        }
    """
    logger = _ensure_logger()
    result = {
        "anchors_updated": [],
        "anchors_failed": [],
        "anchors_path": MONITOR_EVALUATION_ANCHORS_PATH,
        "anchors_total_after": 0,
    }

    # Load existing anchors file (or start fresh)
    existing = {}
    if os.path.exists(MONITOR_EVALUATION_ANCHORS_PATH):
        try:
            with open(MONITOR_EVALUATION_ANCHORS_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                existing = loaded
            else:
                logger.warning(
                    f"Existing anchors file is not a dict; starting from empty. "
                    f"Old contents preserved nowhere."
                )
                existing = {}
        except Exception as e:
            logger.warning(f"Could not parse existing anchors file: {e}; starting from empty")
            existing = {}

    now_iso = datetime.now(timezone.utc).isoformat()

    for ticker in tickers:
        price, price_date, error = _read_price_from_stage_3(ticker)
        if error is not None:
            result["anchors_failed"].append({"ticker": ticker, "error": error})
            logger.warning(f"Could not update anchor for {ticker}: {error}")
            continue

        existing[ticker] = {
            "evaluated_at": now_iso,
            "evaluated_at_price": price,
            "stage_3_price_date": price_date,
        }
        result["anchors_updated"].append(ticker)
        logger.info(
            f"Anchor updated for {ticker}: price={price} "
            f"(stage_3_price_date={price_date})"
        )

    result["anchors_total_after"] = len(existing)

    try:
        _atomic_write_json(MONITOR_EVALUATION_ANCHORS_PATH, existing)
        logger.info(
            f"Wrote evaluation anchors file: {len(result['anchors_updated'])} updated, "
            f"{len(result['anchors_failed'])} failed, "
            f"{result['anchors_total_after']} total anchors after."
        )
    except Exception as e:
        # If the write itself fails, mark every "updated" ticker as failed.
        logger.error(f"FAILED to write evaluation anchors file: {e}")
        for ticker in result["anchors_updated"]:
            result["anchors_failed"].append({
                "ticker": ticker,
                "error": f"Write of anchors file failed: {e}"
            })
        result["anchors_updated"] = []
        result["anchors_total_after"] = 0

    return result


# ============ ORCHESTRATOR INVOCATION ============

def _load_orchestrator():
    """
    Dynamically load pipeline tools/orchestrator.py and return the module.
    Loaded by path because the containing folder name has a space.
    """
    if not os.path.exists(ORCHESTRATOR_PATH):
        raise FileNotFoundError(f"Orchestrator not found: {ORCHESTRATOR_PATH}")
    spec = importlib.util.spec_from_file_location("pipeline_orchestrator", ORCHESTRATOR_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not create spec for {ORCHESTRATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ============ PUBLIC ENTRY POINT ============

async def invoke_pipeline(targeted_tickers: list) -> dict:
    """
    Narrow target_company_list.json to the given tickers, run Stages 2/3/4
    sequentially, restore target_company_list.json on exit.

    Args:
        targeted_tickers: list of ticker symbols (e.g. ["ARDX", "BIP"]).

    Returns:
        Result dict with the following shape:
        {
          "invocation_timestamp": ISO timestamp,
          "targeted_tickers_requested": [...],
          "targeted_tickers_deduped": [...],
          "targeted_entries_written": [...full "NAME (TICKER)" strings...],
          "stages_attempted": ["stage_2", "stage_3", "stage_4"],
          "stages_succeeded": [list of stage labels that returned exit 0],
          "stages_failed": [list of stage labels that failed],
          "stage_results": [per-stage detail dicts],
          "status": "success" | "failed" | "rejected_lock_held" | "rejected_invalid_input",
          "pre_invocation_consolidation_date": str or None,
          "post_invocation_consolidation_date": str or None,
          "backup_path": str or None,
          "target_list_restored": bool,
          "anchor_update": dict or None,           # populated only on success
          "duration_seconds": float,
          "data_quality_flags": [list of human-readable strings],
        }
    """
    logger = _ensure_logger()
    invocation_start = time.time()
    timestamp_dt = datetime.now(timezone.utc)
    timestamp_str = timestamp_dt.strftime("%Y-%m-%dT%H-%M-%S")

    result = {
        "invocation_timestamp": timestamp_dt.isoformat(),
        "targeted_tickers_requested": list(targeted_tickers) if targeted_tickers else [],
        "targeted_tickers_deduped": [],
        "targeted_entries_written": [],
        "override_mode_active": bool(OVERRIDE_TARGETS),
        "override_entries_injected": [],
        "stages_attempted": [],
        "stages_succeeded": [],
        "stages_failed": [],
        "stage_results": [],
        "orchestrator_summary": None,
        "status": "unknown",
        "pre_invocation_consolidation_date": None,
        "post_invocation_consolidation_date": None,
        "backup_path": None,
        "target_list_restored": False,
        "anchor_update": None,
        "duration_seconds": None,
        "data_quality_flags": [],
    }

    # ============ Lockfile ============
    if not _acquire_invoker_lock():
        result["status"] = "rejected_lock_held"
        result["data_quality_flags"].append(
            "Pipeline invoker lock held by another invocation; refused to start."
        )
        result["duration_seconds"] = round(time.time() - invocation_start, 2)
        return result

    # current_list is captured in either the override or normal validation path.
    # We need it visible inside the finally block to restore target_company_list.json.
    current_list = None
    # deduped is the ticker list used for both narrowing (normal mode) and the
    # anchor update (both modes). In override mode it's extracted from the
    # override entries.
    deduped = []

    try:
        # ============ Override mode check ============
        # If OVERRIDE_TARGETS is on, we bypass the normal validation entirely
        # and use OVERRIDE_TARGET_ENTRIES verbatim. Log loudly so this never
        # goes unnoticed.
        override_entries = None

        if OVERRIDE_TARGETS:
            print("\n[Pipeline Invoker] *** OVERRIDE MODE ACTIVE ***")
            print(f"[Pipeline Invoker] Caller's tickers ({targeted_tickers}) IGNORED.")
            print(f"[Pipeline Invoker] Using OVERRIDE_TARGET_ENTRIES instead.")
            logger.warning(
                f"OVERRIDE MODE ACTIVE — caller's tickers {targeted_tickers} ignored. "
                f"Using OVERRIDE_TARGET_ENTRIES."
            )

            valid, error, override_entries = _validate_override_config()
            if not valid:
                result["status"] = "rejected_invalid_input"
                result["data_quality_flags"].append(f"OVERRIDE MODE: {error}")
                logger.error(f"Override config invalid: {error}")
                result["duration_seconds"] = round(time.time() - invocation_start, 2)
                return result

            result["override_entries_injected"] = override_entries

            # Tickers for the anchor update come from the override entries.
            deduped = [_extract_ticker(e) for e in override_entries if _extract_ticker(e)]

            # Even in override mode we need Stage 1's current list for backup
            # and restoration. Read it directly (skipping the validate step).
            if not os.path.exists(TARGET_LIST_PATH):
                result["status"] = "rejected_invalid_input"
                msg = (
                    f"OVERRIDE MODE: Stage 1's target_company_list.json not found "
                    f"at {TARGET_LIST_PATH}. Run Stage 1 first (or create an empty "
                    f"placeholder list there)."
                )
                result["data_quality_flags"].append(msg)
                logger.error(msg)
                result["duration_seconds"] = round(time.time() - invocation_start, 2)
                return result
            try:
                with open(TARGET_LIST_PATH, "r", encoding="utf-8") as f:
                    current_list = json.load(f)
                if not isinstance(current_list, list):
                    raise TypeError(
                        f"target_company_list.json is not a list (got {type(current_list).__name__})"
                    )
            except Exception as e:
                result["status"] = "rejected_invalid_input"
                result["data_quality_flags"].append(
                    f"OVERRIDE MODE: failed to read Stage 1's target list for backup: {e}"
                )
                logger.error(f"Override read of Stage 1 list failed: {e}")
                result["duration_seconds"] = round(time.time() - invocation_start, 2)
                return result

        else:
            # ============ Normal mode: validate caller's inputs ============
            valid, error, current_list, deduped, resolved_entries = _validate_inputs(targeted_tickers)
            if not valid:
                result["status"] = "rejected_invalid_input"
                result["data_quality_flags"].append(error)
                logger.error(error)
                result["duration_seconds"] = round(time.time() - invocation_start, 2)
                return result

            result["targeted_tickers_deduped"] = deduped

        # ============ Capture pre-invocation consolidation date ============
        if os.path.exists(CONSOLIDATION_PORTFOLIO_PATH):
            try:
                with open(CONSOLIDATION_PORTFOLIO_PATH, "r", encoding="utf-8") as f:
                    pre_portfolio = json.load(f)
                result["pre_invocation_consolidation_date"] = pre_portfolio.get("consolidation_date")
            except Exception as e:
                result["data_quality_flags"].append(
                    f"Could not read pre-invocation consolidation_portfolio.json: {e}"
                )

        # ============ Backup ============
        try:
            backup_dir = _backup_pre_invocation(timestamp_str, current_list)
            result["backup_path"] = backup_dir
        except Exception as e:
            result["status"] = "failed"
            result["data_quality_flags"].append(f"Backup failed; aborting before any mutation: {e}")
            logger.exception("Backup failed:")
            result["duration_seconds"] = round(time.time() - invocation_start, 2)
            return result

        # ============ Write target list (override or narrowed) ============
        try:
            if OVERRIDE_TARGETS:
                _write_override_target_list(override_entries)
                result["targeted_entries_written"] = override_entries
            else:
                narrowed = _write_narrowed_target_list(deduped, resolved_entries)
                result["targeted_entries_written"] = narrowed
        except Exception as e:
            result["status"] = "failed"
            result["data_quality_flags"].append(f"Failed to write target list: {e}")
            logger.exception("Write target list failed:")
            result["duration_seconds"] = round(time.time() - invocation_start, 2)
            return result

        # ============ Invoke Stages 2 → 3 → 4 via the orchestrator ============
        # Delegate to pipeline tools/orchestrator.py with Stage 1 skipped. The
        # orchestrator runs each stage as an isolated subprocess with
        # completeness gating and retry — the path it is designed to be called
        # on for targeted reruns.
        logger.info("=== Invoking Stages 2-4 via the pipeline orchestrator (skip Stage 1) ===")
        print("\n[Pipeline Invoker] === Invoking Stages 2-4 via orchestrator ===\n")

        chain_failed = False
        try:
            orchestrator = _load_orchestrator()
            orch_summary = await orchestrator.run_pipeline(run_stage_1=False)
        except Exception as e:
            result["status"] = "failed"
            result["data_quality_flags"].append(f"Orchestrator invocation raised: {e}")
            logger.exception("Orchestrator invocation raised:")
            result["duration_seconds"] = round(time.time() - invocation_start, 2)
            return result

        result["orchestrator_summary"] = orch_summary

        for stage_record in orch_summary.get("stages", []):
            stage_label = stage_record.get("stage")
            result["stages_attempted"].append(stage_label)
            result["stage_results"].append(stage_record)
            if stage_record.get("status") in ("complete", "continued_below_full"):
                result["stages_succeeded"].append(stage_label)
            else:
                result["stages_failed"].append(stage_label)

        overall_status = orch_summary.get("overall_status")
        if overall_status == "halted":
            chain_failed = True
            print(f"[Pipeline Invoker] Orchestrator HALTED at "
                  f"{orch_summary.get('halted_at_stage')}.")
            logger.error(f"Orchestrator halted at {orch_summary.get('halted_at_stage')}.")
        elif overall_status == "completed_with_gaps":
            result["data_quality_flags"].append(
                "Orchestrator completed with gaps — a stage finished below 100% "
                "completeness but at/above the threshold."
            )

        # ============ Capture post-invocation consolidation date ============
        if os.path.exists(CONSOLIDATION_PORTFOLIO_PATH):
            try:
                with open(CONSOLIDATION_PORTFOLIO_PATH, "r", encoding="utf-8") as f:
                    post_portfolio = json.load(f)
                result["post_invocation_consolidation_date"] = post_portfolio.get("consolidation_date")
            except Exception as e:
                result["data_quality_flags"].append(
                    f"Could not read post-invocation consolidation_portfolio.json: {e}"
                )

        result["status"] = "failed" if chain_failed else "success"

        # ============ Update Stage 5 monitor's evaluation anchors ============
        # Only on full success. The Stage 5 monitor reads this file to compute
        # cumulative move since last evaluation for each ticker. Without it,
        # the monitor's cumulative-move trigger would fire on the same drift
        # forever after a rerun.
        #
        # Failure here is non-fatal — the rerun itself succeeded. We log and
        # surface the failure in data_quality_flags so the monitor knows the
        # anchor for that ticker may be stale.
        if result["status"] == "success":
            try:
                anchor_result = _update_evaluation_anchors(deduped)
                result["anchor_update"] = anchor_result
                if anchor_result["anchors_failed"]:
                    for failure in anchor_result["anchors_failed"]:
                        result["data_quality_flags"].append(
                            f"Anchor update failed for {failure['ticker']}: {failure['error']}"
                        )
            except Exception as e:
                # Truly unexpected error in anchor update itself. Don't fail the
                # invocation — the rerun was successful — but flag prominently.
                logger.exception("Unexpected error during anchor update:")
                result["data_quality_flags"].append(
                    f"Evaluation anchor update raised unexpected exception: {e}"
                )
                result["anchor_update"] = {
                    "anchors_updated": [],
                    "anchors_failed": [
                        {"ticker": t, "error": f"anchor update raised: {e}"}
                        for t in deduped
                    ],
                    "anchors_path": MONITOR_EVALUATION_ANCHORS_PATH,
                    "anchors_total_after": None,
                }

    finally:
        # ============ Always restore target list ============
        # Even if the chain failed or an unhandled exception occurred, we must
        # not leave target_company_list.json in the narrowed state.
        try:
            if current_list is not None:
                restored = _restore_target_list(current_list)
                result["target_list_restored"] = restored
                if not restored:
                    result["data_quality_flags"].append(
                        "FAILED to restore target_company_list.json — manual intervention needed. "
                        f"Pre-invocation contents preserved at: {result['backup_path']}"
                    )
        except Exception as e:
            result["data_quality_flags"].append(
                f"Exception during target list restore: {e}"
            )
            logger.exception("Exception during restore:")

        # ============ Always release lockfile ============
        _release_invoker_lock()

        result["duration_seconds"] = round(time.time() - invocation_start, 2)
        logger.info(
            f"Invocation complete: status={result['status']}, "
            f"duration={result['duration_seconds']}s, "
            f"succeeded={result['stages_succeeded']}, "
            f"failed={result['stages_failed']}"
        )

    return result