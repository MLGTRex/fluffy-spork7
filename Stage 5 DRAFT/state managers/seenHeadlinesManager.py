"""
Seen headlines manager — dedup of news items against history.

Tracks which news headline IDs have been processed by the watcher for each
ticker, so the same headline doesn't get re-classified on subsequent hourly
runs.

State file: /Stage 5 DRAFT/state/seen_headlines.json
Format:
    {
        "tickers": {
            "ARDX": [
                {"id": "https://example.com/ardx-fda", "seen_at": "2026-05-13T11:23:45+00:00"},
                ...
            ],
            ...
        },
        "last_pruned_at": "2026-05-13T00:00:00+00:00"
    }

Public functions:
    - filter_unseen(ticker, news_items): drops items previously seen for this ticker
    - mark_as_seen(ticker, news_items): records items as seen, persists immediately
    - prune_old_entries(force=False): removes entries older than TTL
    - load_seen_headlines(): returns the in-memory state dict (for debugging)
    - clear_ticker(ticker): manually clear seen-history for one ticker (debug/reset)
    - clear_all(): clear entire state (debug only)

Persistence: state file is atomic-written (temp file + rename).
Pruning: periodic, controlled by PRUNE_INTERVAL_HOURS. First call after
interval expires triggers prune; subsequent calls within the interval skip.

CLI:
    python3 seenHeadlinesManager.py --show ARDX
    python3 seenHeadlinesManager.py --show-all
    python3 seenHeadlinesManager.py --prune
    python3 seenHeadlinesManager.py --clear ARDX
    python3 seenHeadlinesManager.py --clear-all
"""

import os
import sys
import json
import tempfile
import logging
import argparse
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


# ============ CONFIG ============

# How long to keep "seen" markers before pruning. Items older than this are
# removed from the state file on next prune. 7 days matches the typical
# news API recency window — anything older is unlikely to come back in
# results anyway.
TTL_DAYS = 7

# How often to actually prune. The seen-headlines functions check the
# last_pruned_at marker; if more than this many hours have passed, prune
# runs once and updates the marker. Subsequent calls skip until next window.
PRUNE_INTERVAL_HOURS = 1


# ============ PATHS ============

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STAGE5_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
STATE_PATH = os.path.join(STAGE5_ROOT, "state", "seen_headlines.json")


# ============ MODULE STATE CACHE ============
# Multiple calls within a single watcher run share the same in-memory state.
# Loaded lazily on first access; reloaded if mtime changes (defensive against
# external edits between calls).

_STATE_CACHE = {"path": None, "data": None, "mtime": None}


# ============ HELPERS ============

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_to_utc(value):
    """Parse an ISO timestamp string to a tz-aware UTC datetime. None on failure."""
    if not value:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if not isinstance(value, str):
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _empty_state() -> dict:
    return {"tickers": {}, "last_pruned_at": None}


def _ensure_state_dir() -> None:
    state_dir = os.path.dirname(STATE_PATH)
    os.makedirs(state_dir, exist_ok=True)


# ============ LOAD / SAVE ============

def load_seen_headlines(force_reload: bool = False) -> dict:
    """
    Load the seen-headlines state file. Cached per-process; reloads if
    file mtime changes. Returns the empty-state dict if file doesn't exist
    or is unreadable.

    Args:
        force_reload: if True, bypass the cache and re-read from disk.
    """
    if not os.path.exists(STATE_PATH):
        if _STATE_CACHE["data"] is None:
            _STATE_CACHE["path"] = STATE_PATH
            _STATE_CACHE["data"] = _empty_state()
            _STATE_CACHE["mtime"] = None
        return _STATE_CACHE["data"]

    try:
        current_mtime = os.path.getmtime(STATE_PATH)
    except OSError:
        current_mtime = None

    if (not force_reload
            and _STATE_CACHE["path"] == STATE_PATH
            and _STATE_CACHE["data"] is not None
            and _STATE_CACHE["mtime"] == current_mtime):
        return _STATE_CACHE["data"]

    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"Could not read seen_headlines.json: {e}; treating as empty")
        data = _empty_state()

    # Defensive normalization
    if not isinstance(data, dict):
        data = _empty_state()
    if "tickers" not in data or not isinstance(data.get("tickers"), dict):
        data["tickers"] = {}
    if "last_pruned_at" not in data:
        data["last_pruned_at"] = None

    _STATE_CACHE["path"] = STATE_PATH
    _STATE_CACHE["data"] = data
    _STATE_CACHE["mtime"] = current_mtime
    return data


def _save_seen_headlines(data: dict) -> None:
    """Atomic-write the state file. Updates mtime cache after write."""
    _ensure_state_dir()
    # Write to temp file in same directory, then rename
    state_dir = os.path.dirname(STATE_PATH)
    fd, tmp_path = tempfile.mkstemp(prefix=".seen_headlines.", suffix=".tmp", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=False)
        os.replace(tmp_path, STATE_PATH)
    except Exception:
        # Best-effort cleanup of stray temp file
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise

    try:
        _STATE_CACHE["path"] = STATE_PATH
        _STATE_CACHE["data"] = data
        _STATE_CACHE["mtime"] = os.path.getmtime(STATE_PATH)
    except OSError:
        _STATE_CACHE["mtime"] = None


# ============ PRUNING ============

def _is_prune_due(state: dict, now: datetime) -> bool:
    """Return True if it's been at least PRUNE_INTERVAL_HOURS since last prune."""
    last = _parse_iso_to_utc(state.get("last_pruned_at"))
    if last is None:
        return True
    return (now - last) >= timedelta(hours=PRUNE_INTERVAL_HOURS)


def prune_old_entries(force: bool = False) -> dict:
    """
    Remove entries older than TTL_DAYS across all tickers. Tickers with zero
    remaining entries get their entries cleared but the ticker key stays
    (cheap, avoids churn).

    Args:
        force: bypass the PRUNE_INTERVAL_HOURS check.

    Returns:
        {
            "pruned": bool,             # True if pruning was actually performed
            "skipped_reason": str or None,
            "entries_before": int,
            "entries_after": int,
            "entries_removed": int,
            "tickers_affected": [str],  # tickers that had entries removed
            "ttl_cutoff": ISO timestamp,
        }
    """
    state = load_seen_headlines()
    now = _now_utc()

    if not force and not _is_prune_due(state, now):
        return {
            "pruned": False,
            "skipped_reason": (
                f"Last prune was within {PRUNE_INTERVAL_HOURS}h; skipping. "
                f"Use force=True to override."
            ),
            "entries_before": _count_entries(state),
            "entries_after": _count_entries(state),
            "entries_removed": 0,
            "tickers_affected": [],
            "ttl_cutoff": (now - timedelta(days=TTL_DAYS)).isoformat(),
        }

    cutoff = now - timedelta(days=TTL_DAYS)
    entries_before = _count_entries(state)
    affected = []

    for ticker, entries in list(state["tickers"].items()):
        if not isinstance(entries, list):
            state["tickers"][ticker] = []
            continue
        kept = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            seen_at = _parse_iso_to_utc(entry.get("seen_at"))
            # If seen_at is malformed, keep the entry (defensive — better to over-keep
            # than to silently drop)
            if seen_at is None or seen_at >= cutoff:
                kept.append(entry)
        if len(kept) != len(entries):
            affected.append(ticker)
        state["tickers"][ticker] = kept

    state["last_pruned_at"] = now.isoformat()
    _save_seen_headlines(state)

    entries_after = _count_entries(state)
    return {
        "pruned": True,
        "skipped_reason": None,
        "entries_before": entries_before,
        "entries_after": entries_after,
        "entries_removed": entries_before - entries_after,
        "tickers_affected": affected,
        "ttl_cutoff": cutoff.isoformat(),
    }


def _count_entries(state: dict) -> int:
    total = 0
    for entries in (state.get("tickers") or {}).values():
        if isinstance(entries, list):
            total += len(entries)
    return total


# ============ PUBLIC API ============

def filter_unseen(ticker: str, news_items: list) -> dict:
    """
    Given a list of news items (from newsFetcher), drop those previously
    seen for this ticker. Pruning is invoked opportunistically (controlled
    by PRUNE_INTERVAL_HOURS).

    Args:
        ticker: ticker symbol
        news_items: list of dicts as returned by newsFetcher.fetch_news_for_ticker

    Returns:
        {
            "ticker": str,
            "input_count": int,
            "unseen_items": [dict],     # items that have not been seen
            "filtered_out_count": int,
            "filtered_out_ids": [str],
            "prune_summary": {...} or None,  # if pruning ran on this call
        }
    """
    # Opportunistic pruning before we read
    prune_summary = prune_old_entries(force=False)
    prune_summary = prune_summary if prune_summary.get("pruned") else None

    state = load_seen_headlines()
    ticker_entries = state["tickers"].get(ticker, [])
    seen_ids = {e.get("id") for e in ticker_entries if isinstance(e, dict) and e.get("id")}

    unseen = []
    filtered_ids = []
    for item in news_items or []:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if not item_id:
            # No ID → defensive: keep it (can't dedupe without ID)
            unseen.append(item)
            continue
        if item_id in seen_ids:
            filtered_ids.append(item_id)
        else:
            unseen.append(item)

    return {
        "ticker": ticker,
        "input_count": len(news_items or []),
        "unseen_items": unseen,
        "filtered_out_count": len(filtered_ids),
        "filtered_out_ids": filtered_ids,
        "prune_summary": prune_summary,
    }


def mark_as_seen(ticker: str, news_items: list) -> dict:
    """
    Record items as seen for this ticker. Persists immediately.

    If an item is already in the state, its seen_at is left unchanged (we
    don't refresh timestamps — would defeat TTL pruning).

    Args:
        ticker: ticker symbol
        news_items: list of dicts (from newsFetcher)

    Returns:
        {
            "ticker": str,
            "newly_marked_count": int,
            "already_seen_count": int,
            "newly_marked_ids": [str],
        }
    """
    state = load_seen_headlines()
    now_iso = _now_utc().isoformat()

    if ticker not in state["tickers"]:
        state["tickers"][ticker] = []

    existing_ids = {
        e.get("id") for e in state["tickers"][ticker]
        if isinstance(e, dict) and e.get("id")
    }

    newly_marked_ids = []
    already_seen = 0
    for item in news_items or []:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if not item_id:
            continue
        if item_id in existing_ids:
            already_seen += 1
            continue
        state["tickers"][ticker].append({
            "id": item_id,
            "seen_at": now_iso,
        })
        existing_ids.add(item_id)
        newly_marked_ids.append(item_id)

    if newly_marked_ids:
        _save_seen_headlines(state)

    return {
        "ticker": ticker,
        "newly_marked_count": len(newly_marked_ids),
        "already_seen_count": already_seen,
        "newly_marked_ids": newly_marked_ids,
    }


def clear_ticker(ticker: str) -> dict:
    """
    Manually clear seen-history for one ticker. Useful for testing or when
    you want to force the watcher to re-classify all of a ticker's news.

    Returns:
        {"ticker": str, "entries_removed": int}
    """
    state = load_seen_headlines()
    if ticker not in state["tickers"]:
        return {"ticker": ticker, "entries_removed": 0}
    removed = len(state["tickers"][ticker])
    del state["tickers"][ticker]
    _save_seen_headlines(state)
    return {"ticker": ticker, "entries_removed": removed}


def clear_all() -> dict:
    """
    Nuke entire state. Use with caution — next watcher run will re-classify
    every news item it sees as if for the first time.
    """
    state = load_seen_headlines()
    entries_removed = _count_entries(state)
    tickers_affected = list(state["tickers"].keys())
    new_state = _empty_state()
    _save_seen_headlines(new_state)
    return {
        "entries_removed": entries_removed,
        "tickers_cleared": tickers_affected,
    }


# ============ CLI ============

def _setup_cli_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)-7s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[logging.StreamHandler()],
    )


def _parse_cli_args():
    parser = argparse.ArgumentParser(description="Manage seen-headlines state.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--show", type=str, metavar="TICKER",
                       help="Print seen-headline entries for one ticker.")
    group.add_argument("--show-all", action="store_true",
                       help="Print all seen-headline entries (counts per ticker).")
    group.add_argument("--prune", action="store_true",
                       help="Force-prune entries older than TTL.")
    group.add_argument("--clear", type=str, metavar="TICKER",
                       help="Clear seen-history for one ticker.")
    group.add_argument("--clear-all", action="store_true",
                       help="Nuke entire seen-history state (with confirm prompt).")
    return parser.parse_args()


def _cli_show_ticker(ticker: str) -> None:
    state = load_seen_headlines(force_reload=True)
    entries = state["tickers"].get(ticker, [])
    print(f"\nSeen headlines for {ticker}: {len(entries)} entries")
    if not entries:
        print("  (none)")
        return
    # Sort by seen_at desc
    sorted_entries = sorted(
        entries,
        key=lambda e: _parse_iso_to_utc(e.get("seen_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    for e in sorted_entries:
        print(f"  [{e.get('seen_at')}] {e.get('id')}")


def _cli_show_all() -> None:
    state = load_seen_headlines(force_reload=True)
    total = _count_entries(state)
    print(f"\nTotal entries across all tickers: {total}")
    print(f"Last pruned at: {state.get('last_pruned_at')}")
    print(f"Tickers tracked: {len(state['tickers'])}\n")
    for ticker in sorted(state["tickers"].keys()):
        entries = state["tickers"][ticker]
        print(f"  {ticker}: {len(entries)} entries")


def _cli_prune() -> None:
    result = prune_old_entries(force=True)
    print(f"\nPrune result:")
    print(f"  Pruned:           {result['pruned']}")
    print(f"  Entries before:   {result['entries_before']}")
    print(f"  Entries after:    {result['entries_after']}")
    print(f"  Entries removed:  {result['entries_removed']}")
    print(f"  TTL cutoff (UTC): {result['ttl_cutoff']}")
    if result['tickers_affected']:
        print(f"  Tickers affected: {', '.join(result['tickers_affected'])}")


def _cli_clear_ticker(ticker: str) -> None:
    result = clear_ticker(ticker)
    print(f"\nCleared {ticker}: removed {result['entries_removed']} entries.")


def _cli_clear_all() -> None:
    print("\nWARNING: This will clear ALL seen-headlines state.")
    print("Next watcher run will re-classify every news item as if for the first time.")
    confirm = input("Type 'CLEAR' to confirm: ").strip()
    if confirm != "CLEAR":
        print("Aborted.")
        return
    result = clear_all()
    print(f"Cleared {result['entries_removed']} entries across {len(result['tickers_cleared'])} tickers.")


if __name__ == "__main__":
    args = _parse_cli_args()
    _setup_cli_logging()
    if args.show:
        _cli_show_ticker(args.show)
    elif args.show_all:
        _cli_show_all()
    elif args.prune:
        _cli_prune()
    elif args.clear:
        _cli_clear_ticker(args.clear)
    elif args.clear_all:
        _cli_clear_all()