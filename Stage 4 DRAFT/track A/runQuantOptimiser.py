"""
Workflow script for Track A — Pure Quant Portfolio Construction.

Reads Stage 3 per-company JSONs, extracts the fields Track A needs, runs the
optimization, writes the result to /stage 4 DRAFT/output/track_a_portfolio.json.

Freshness check: re-runs if track_a_portfolio.json is missing OR its date is
older than the latest Stage 3 consolidation_date.
"""

import os
import sys
import json
import logging
import re
from datetime import datetime

# Allow importing siblings
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from quantOptimiser import construct_track_a_portfolio


# ============ LOGGING ============

log_filename = f"track_a_log_{datetime.now().strftime('%Y-%m-%d')}.log"
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
STAGE4_ROOT = os.path.join(SCRIPT_DIR, "..")
OUTPUT_DIR = os.path.join(STAGE4_ROOT, "output")
TRACK_A_PATH = os.path.join(OUTPUT_DIR, "track_a_portfolio.json")
STAGE3_OUTPUT_DIR = os.path.normpath(os.path.join(STAGE4_ROOT, "..", "Stage 3 DRAFT", "output"))


# ============ INPUT LOADING ============

def extract_ticker_from_company_name(company_name: str) -> str:
    """Extract ticker from 'Mastercard (MA)' -> 'MA'."""
    if not company_name:
        return None
    match = re.search(r"\(([A-Z0-9\-\.]+)\)\s*$", company_name)
    return match.group(1) if match else None


def load_candidates_from_stage3(stage3_dir: str) -> list:
    """
    Read all per-company JSONs from Stage 3 output and extract the fields Track A needs.

    Returns list of dicts:
        [
            {"ticker", "expected_return_12m", "base_return_12m", "sector",
             "company_name", "industry", "conviction", "consolidation_date"},
            ...
        ]
    """
    candidates = []

    if not os.path.isdir(stage3_dir):
        logger.error(f"Stage 3 output directory not found: {stage3_dir}")
        return candidates

    for fname in sorted(os.listdir(stage3_dir)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(stage3_dir, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"Could not read {fname}: {e}")
            continue

        # Ticker (prefer top-level ticker field, fall back to company_name parsing)
        ticker = data.get("ticker")
        if not ticker:
            company_name = data.get("company_name", "")
            ticker = extract_ticker_from_company_name(company_name)
        if not ticker:
            logger.warning(f"Could not extract ticker from {fname}; skipping")
            continue

        # Sector from valuation_metrics (Stage 3a output)
        vm = data.get("valuation_metrics") or {}
        sector = vm.get("sector") if isinstance(vm, dict) else None
        industry = vm.get("industry") if isinstance(vm, dict) else None

        candidate = {
            "ticker": ticker,
            "expected_return_12m": data.get("expected_return_12m"),
            "base_return_12m": data.get("base_return_12m"),
            "upside_return_12m": data.get("upside_return_12m"),
            "downside_return_12m": data.get("downside_return_12m"),
            "sector": sector,
            "industry": industry,
            "company_name": data.get("company_name", ""),
            "conviction": data.get("conviction"),
            "consolidation_date": data.get("consolidation_date", ""),
        }
        candidates.append(candidate)

    return candidates


# ============ FRESHNESS ============

def is_track_a_stale(candidates: list) -> bool:
    """
    Track A is stale if:
        - track_a_portfolio.json doesn't exist, OR
        - its analysis_date is older than the latest consolidation_date across candidates
    """
    if not os.path.exists(TRACK_A_PATH):
        return True

    try:
        with open(TRACK_A_PATH, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except Exception:
        return True

    existing_date = existing.get("analysis_date", "")
    if not existing_date:
        return True

    latest_consolidation = ""
    for c in candidates:
        d = c.get("consolidation_date", "")
        if d and d > latest_consolidation:
            latest_consolidation = d

    if latest_consolidation and existing_date < latest_consolidation:
        return True

    return False


# ============ MAIN ============

def main():
    today_str = datetime.now().date().isoformat()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f"Stage 4 Track A (Pure Quant) starting...")
    print(f"Stage 3 input: {STAGE3_OUTPUT_DIR}")
    print(f"Output: {TRACK_A_PATH}")

    candidates = load_candidates_from_stage3(STAGE3_OUTPUT_DIR)
    if not candidates:
        print(f"Error: no candidates loaded from Stage 3.")
        sys.exit(1)
    print(f"\nLoaded {len(candidates)} candidates from Stage 3.")

    if not is_track_a_stale(candidates):
        print(f"[Track A] portfolio is current — skipping.")
        return

    print(f"[Track A] running optimization...")
    result = construct_track_a_portfolio(candidates)
    result["analysis_date"] = today_str

    try:
        with open(TRACK_A_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"Error writing {TRACK_A_PATH}: {e}")
        sys.exit(1)

    # Summary
    print(f"\n{'='*60}")
    print(f"  Track A complete.")
    print(f"{'='*60}")
    print(f"\nStatus: {result['status']}")
    print(f"Input candidates: {result['n_input_candidates']}")
    print(f"Eligible (passed pre-filter): {result['n_eligible']}")
    print(f"Filtered out: {result['n_filtered_out']}")
    if result["filtered_out"]:
        for entry in result["filtered_out"]:
            print(f"  - {entry['ticker']}: {entry['reason']}")

    if result["status"] == "optimal":
        print(f"\nObjective value (portfolio expected_return_12m): {result['objective_value']:.4f}")
        print(f"Positions ({len(result['positions'])}):")
        for p in result["positions"]:
            print(f"  {p['ticker']:6s}  {p['allocation_pct']:6.2f}%  "
                  f"sector={p['sector']:20s}  exp_ret_12m={p['expected_return_12m']:7.4f}  "
                  f"base_ret_12m={p['base_return_12m']:7.4f}")
        # Sector check summary
        sector_totals = {}
        for p in result["positions"]:
            sector_totals[p["sector"]] = sector_totals.get(p["sector"], 0.0) + p["allocation_pct"]
        print(f"\nSector allocations:")
        for sec, pct in sorted(sector_totals.items(), key=lambda x: -x[1]):
            print(f"  {sec:25s}  {pct:6.2f}%")
        print(f"\nWritten to {TRACK_A_PATH}")
    else:
        print(f"\nError: {result.get('error_message')}")
        sys.exit(1)


if __name__ == "__main__":
    main()