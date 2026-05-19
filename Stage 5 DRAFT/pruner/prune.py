"""
Pruner (functional).

Pure logic for deciding what to prune from downstream stages, given:
    - The current top 50 (from Stage 1's target_company_list.json contents)
    - The list of per-company output files currently in Stage 2 and Stage 3
    - The list of output files currently in Stage 4

Produces a structured 'prune plan' describing what should be moved where.
No file I/O. No side effects. The workflow script consumes the plan and
performs the actual file moves.

The plan separates moves by source stage so the workflow can structure
backups under stage-specific subdirectories.
"""

import logging
import re

logger = logging.getLogger(__name__)


# ============ CONFIG ============

# Stage 4 output entries that must NOT be pruned. "portfolio history" is the
# reconciliation sub-stage's incumbent archive — Stage 4 is stateful and depends
# on it surviving every run. Listed explicitly so it stays protected even if the
# directory listing is later changed to recurse into subdirectories.
STAGE_4_PRESERVE = {"portfolio history"}


# ============ TICKER EXTRACTION ============

_TICKER_RE = re.compile(r"\(([A-Z0-9\-\.]+)\)\s*$")


def extract_ticker_from_entry(entry: str):
    """
    Extract ticker symbol from a 'COMPANY NAME (TICKER)' entry.
    Returns None if no ticker found.
    """
    if not isinstance(entry, str):
        return None
    match = _TICKER_RE.search(entry)
    return match.group(1) if match else None


def extract_ticker_from_filename(filename: str, recognized_tickers: set):
    """
    Extract a ticker from a per-company output filename by matching against a
    set of recognized tickers. Returns the matched ticker or None.

    Stage 2 and Stage 3 per-company files use ticker-bearing filenames but the
    exact convention may vary (e.g., 'ARDX_research.json', 'ARDX_scenarios.json',
    'Ardelyx (ARDX).json'). Rather than hard-code a regex per stage, we accept
    any filename that contains a known ticker token bounded by start/end or
    non-alphanumeric characters (so 'ARDX' matches in 'ARDX_research',
    'Ardelyx (ARDX)', 'ARDX-summary' but not 'CARDX' or 'ARDXY').

    Args:
        filename: the bare filename (no directory prefix)
        recognized_tickers: set of ticker strings to match against

    Returns:
        The matched ticker (str) or None if no recognized ticker is found.
    """
    if not filename:
        return None
    # Strip extension
    base = filename.rsplit(".", 1)[0]
    for ticker in recognized_tickers:
        pattern = r"(^|[^A-Za-z0-9])" + re.escape(ticker) + r"($|[^A-Za-z0-9])"
        if re.search(pattern, base):
            return ticker
    return None


# Regex for "any ticker-looking token" — used to detect stale files whose
# ticker is no longer in the current universe. Same boundary semantics as
# extract_ticker_from_filename: non-alphanumeric (or string edge) on both sides.
_GENERIC_TICKER_RE = re.compile(
    r"(?:^|[^A-Za-z0-9])([A-Z][A-Z0-9\-\.]{0,9})(?=$|[^A-Za-z0-9])"
)


def find_plausible_ticker_in_filename(filename: str):
    """
    Find the first uppercase token in the filename that looks like a ticker
    (1-10 chars starting with uppercase letter, allowing digits, hyphens, dots).
    Returns the token or None.

    Used to identify stale files whose ticker is no longer in the current
    universe — those files SHOULD be moved, even though extract_ticker_from_filename
    won't have matched them against the (current) recognized_tickers set.
    """
    if not filename:
        return None
    base = filename.rsplit(".", 1)[0]
    match = _GENERIC_TICKER_RE.search(base)
    return match.group(1) if match else None


# ============ TARGET LIST PARSING ============

def parse_current_universe(target_list_contents: list):
    """
    Parse Stage 1's target_company_list contents into a set of currently-active
    tickers.

    Args:
        target_list_contents: list of 'COMPANY NAME (TICKER)' strings

    Returns:
        {
            'tickers': set of ticker strings,
            'entries': list of original entries (preserved for logging),
            'unparseable': list of entries that had no extractable ticker,
        }
    """
    if not isinstance(target_list_contents, list):
        raise TypeError(
            f"Expected list, got {type(target_list_contents).__name__}. "
            f"target_company_list.json should be a JSON array."
        )

    tickers = set()
    unparseable = []
    entries = []
    for entry in target_list_contents:
        if not isinstance(entry, str):
            unparseable.append(entry)
            continue
        entries.append(entry)
        ticker = extract_ticker_from_entry(entry)
        if ticker:
            tickers.add(ticker)
        else:
            unparseable.append(entry)

    return {
        "tickers": tickers,
        "entries": entries,
        "unparseable": unparseable,
    }


# ============ PRUNE PLAN COMPUTATION ============

def compute_stage_2_plan(stage_2_files: list, current_tickers: set):
    """
    Decide which Stage 2 per-company files should be moved.

    Args:
        stage_2_files: list of filenames (not full paths) currently in Stage 2's output dir
        current_tickers: set of currently-active tickers

    Returns:
        {
            'to_move': [{'filename': str, 'matched_ticker': str}],
            'to_keep': [{'filename': str, 'matched_ticker': str}],
            'unrecognized': [str],  # filenames that didn't look like ticker-bearing files at all
        }
    """
    to_move = []
    to_keep = []
    unrecognized = []

    for filename in stage_2_files:
        # First: try to match against current tickers
        current_ticker = extract_ticker_from_filename(filename, current_tickers)
        if current_ticker is not None:
            to_keep.append({"filename": filename, "matched_ticker": current_ticker})
            continue

        # Otherwise: look for any plausible ticker token. If found, the file
        # belongs to a ticker that's no longer in the universe — move it.
        stale_ticker = find_plausible_ticker_in_filename(filename)
        if stale_ticker is not None:
            to_move.append({"filename": filename, "matched_ticker": stale_ticker})
            continue

        # No ticker-like token found — file isn't recognized as ours, leave alone
        unrecognized.append(filename)

    return {
        "to_move": to_move,
        "to_keep": to_keep,
        "unrecognized": unrecognized,
    }


def compute_stage_3_plan(stage_3_files: list, current_tickers: set):
    """Same logic as compute_stage_2_plan."""
    return compute_stage_2_plan(stage_3_files, current_tickers)


def compute_stage_4_plan(stage_4_files: list):
    """
    Stage 4's outputs are portfolio-level, not per-company. The pruner moves
    the regenerated portfolio-level files every run (sub-stages 1-5 rebuild them
    from scratch), but preserves anything in STAGE_4_PRESERVE — notably the
    reconciliation sub-stage's "portfolio history" incumbent archive, which is
    durable state Stage 4 depends on.

    Args:
        stage_4_files: list of filenames currently in Stage 4's output dir

    Returns:
        {
            'to_move': [{'filename': str}],
            'to_keep': [{'filename': str}],
        }
    """
    return {
        "to_move": [{"filename": f} for f in stage_4_files if f not in STAGE_4_PRESERVE],
        "to_keep": [{"filename": f} for f in stage_4_files if f in STAGE_4_PRESERVE],
    }


def compute_stage_4_cache_plan(stage_4_cache_files: list):
    """
    Stage 4's cache is the only thing the pruner DELETES rather than moves.
    The cache (price data from yfinance) is rebuildable for free.

    Args:
        stage_4_cache_files: list of filenames in Stage 4's cache dir

    Returns:
        {
            'to_delete': [{'filename': str}],
        }
    """
    return {
        "to_delete": [{"filename": f} for f in stage_4_cache_files],
    }


# ============ TOP-LEVEL PLAN ============

def compute_prune_plan(
    target_list_contents: list,
    stage_2_files: list,
    stage_3_files: list,
    stage_4_files: list,
    stage_4_cache_files: list,
    held_tickers: set = None,
) -> dict:
    """
    Top-level plan computation. Pure function — given the inputs, produces the
    full plan of what to move, what to keep, and what to delete.

    Args:
        target_list_contents: parsed contents of Stage 1's target_company_list.json
        stage_2_files: filenames in Stage 2's output dir (per-company files)
        stage_3_files: filenames in Stage 3's output dir (per-company files)
        stage_4_files: filenames in Stage 4's output dir (portfolio-level files)
        stage_4_cache_files: filenames in Stage 4's cache dir
        held_tickers: tickers of currently-held portfolio positions. These are
            protected from pruning even when they fall out of Stage 1's fresh
            universe — Stage 4 reconciliation needs their existing Stage 2/3
            research, and the weekly run must neither discard nor re-research
            it. Defaults to empty (behaviour unchanged for callers that omit it).

    Returns:
        {
            'universe': {tickers, entries, unparseable, held_tickers},
            'stage_2': {to_move, to_keep, unrecognized},
            'stage_3': {to_move, to_keep, unrecognized},
            'stage_4': {to_move, to_keep},
            'stage_4_cache': {to_delete},
            'summary': {
                'universe_size': int,
                'held_protected_count': int,
                'stage_2_to_move': int,
                'stage_2_to_keep': int,
                'stage_3_to_move': int,
                'stage_3_to_keep': int,
                'stage_4_to_move': int,
                'stage_4_cache_to_delete': int,
                'unparseable_target_entries': int,
                'unrecognized_stage_2_files': int,
                'unrecognized_stage_3_files': int,
            },
        }
    """
    universe = parse_current_universe(target_list_contents)
    current_tickers = universe["tickers"]

    held_tickers = set(held_tickers or set())
    # Currently-held positions are protected from pruning even when they fall
    # out of Stage 1's fresh universe — Stage 4 reconciliation needs their
    # existing Stage 2/3 research, and the weekly run must neither discard nor
    # re-research it.
    keep_tickers = current_tickers | held_tickers
    universe["held_tickers"] = sorted(held_tickers)

    s2 = compute_stage_2_plan(stage_2_files, keep_tickers)
    s3 = compute_stage_3_plan(stage_3_files, keep_tickers)
    s4 = compute_stage_4_plan(stage_4_files)
    s4_cache = compute_stage_4_cache_plan(stage_4_cache_files)

    summary = {
        "universe_size": len(current_tickers),
        "held_protected_count": len(held_tickers - current_tickers),
        "stage_2_to_move": len(s2["to_move"]),
        "stage_2_to_keep": len(s2["to_keep"]),
        "stage_3_to_move": len(s3["to_move"]),
        "stage_3_to_keep": len(s3["to_keep"]),
        "stage_4_to_move": len(s4["to_move"]),
        "stage_4_cache_to_delete": len(s4_cache["to_delete"]),
        "unparseable_target_entries": len(universe["unparseable"]),
        "unrecognized_stage_2_files": len(s2["unrecognized"]),
        "unrecognized_stage_3_files": len(s3["unrecognized"]),
    }

    return {
        "universe": universe,
        "stage_2": s2,
        "stage_3": s3,
        "stage_4": s4,
        "stage_4_cache": s4_cache,
        "summary": summary,
    }