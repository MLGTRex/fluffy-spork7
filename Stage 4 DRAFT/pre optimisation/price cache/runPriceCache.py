"""
Workflow script for updating the price cache.

Reads the 50 candidate tickers from /stage 3 DRAFT/output/ and the 7 macro factor
proxies from macroAnalysis.py's FACTOR_PROXIES, then ensures the local price cache
is current for all of them.

Runs sequentially to be polite to yfinance.
"""

import os
import sys
import json
import logging
from datetime import datetime
import re

# Allow importing siblings
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from priceCache import ensure_price_cache

# Configure logging
log_filename = f"price_cache_log_{datetime.now().strftime('%Y-%m-%d')}.log"
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


# ============ FACTOR PROXIES ============
# Defined here to keep the price cache independent of the macro analysis module.
# Should match macroAnalysis.py's FACTOR_PROXIES dict.

FACTOR_PROXIES = {
    "interest_rates": "^TNX",
    "oil": "CL=F",
    "usd": "DX-Y.NYB",
    "housing": "XHB",
    "china": "FXI",
    "credit": "HYG",
    "geopolitical": "^VIX",
}


# ============ HELPERS ============

def extract_ticker_from_company_name(company_name: str) -> str:
    """Extract ticker from a string like 'Mastercard (MA)' -> 'MA'."""
    if not company_name:
        return None
    match = re.search(r"\(([A-Z0-9\-\.]+)\)\s*$", company_name)
    return match.group(1) if match else None


def load_candidate_tickers(stage3_output_dir: str) -> list:
    """
    Read tickers from /stage 3 DRAFT/output/ by examining each company JSON's
    company_name field (which is in the form 'Mastercard (MA)').
    """
    tickers = []
    if not os.path.isdir(stage3_output_dir):
        logger.error(f"Stage 3 output dir not found: {stage3_output_dir}")
        return tickers

    for fname in sorted(os.listdir(stage3_output_dir)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(stage3_output_dir, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"Could not read {fname}: {e}")
            continue

        # Try ticker field first (cleanest), fall back to extracting from company_name
        ticker = data.get("ticker")
        if not ticker:
            company_name = data.get("company_name", "")
            ticker = extract_ticker_from_company_name(company_name)

        if ticker:
            tickers.append(ticker)
        else:
            logger.warning(f"Could not extract ticker from {fname}")

    # De-dupe while preserving order
    seen = set()
    unique = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return unique


# ============ MAIN ============

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # /Stage 4 DRAFT root is two levels up from /pre optimisation/price cache/
    stage4_root = os.path.normpath(os.path.join(script_dir, "..", ".."))
    cache_dir = os.path.join(stage4_root, "cache", "prices")
    os.makedirs(cache_dir, exist_ok=True)

    # Stage 3 output lives in a sibling Stage 3 folder
    # Typical path: /investment_pipeline/Stage 3 DRAFT/output/
    stage3_output = os.path.join(stage4_root, "..", "Stage 3 DRAFT", "output")
    stage3_output = os.path.normpath(stage3_output)

    print(f"Stage 4 price cache update starting...")
    print(f"Cache directory: {cache_dir}")
    print(f"Stage 3 output: {stage3_output}")

    # Build list of all tickers to cache
    candidate_tickers = load_candidate_tickers(stage3_output)
    factor_tickers = list(FACTOR_PROXIES.values())
    all_tickers = candidate_tickers + factor_tickers

    print(f"\nCandidate tickers (from Stage 3): {len(candidate_tickers)}")
    print(f"Factor proxy tickers: {len(factor_tickers)}")
    print(f"Total to cache: {len(all_tickers)}")

    if not candidate_tickers:
        print("\nError: no candidate tickers found in Stage 3 output. Aborting.")
        sys.exit(1)

    # Process sequentially to avoid stacking yfinance concurrency
    summary = {"fresh": 0, "fetched_full": 0, "fetched_incremental": 0, "failed": 0}
    failed_tickers = []

    print(f"\nUpdating cache (sequential)...")
    for i, ticker in enumerate(all_tickers, start=1):
        print(f"  [{i}/{len(all_tickers)}] {ticker} ...", end=" ", flush=True)
        result = ensure_price_cache(ticker, cache_dir)
        status = result["status"]
        summary[status] = summary.get(status, 0) + 1
        if status == "failed":
            failed_tickers.append(ticker)
            print(f"FAILED ({result.get('error', 'unknown')})")
        elif status == "fresh":
            print(f"fresh ({result['last_date']})")
        elif status == "fetched_incremental":
            print(f"+{result['rows_added']} rows ({result['last_date']})")
        elif status == "fetched_full":
            print(f"full backfill, {result['rows_added']} rows")

    print(f"\nCache update complete.")
    print(f"  Fresh (no update needed): {summary.get('fresh', 0)}")
    print(f"  Fetched incremental:       {summary.get('fetched_incremental', 0)}")
    print(f"  Fetched full backfill:     {summary.get('fetched_full', 0)}")
    print(f"  Failed:                    {summary.get('failed', 0)}")

    if failed_tickers:
        print(f"\nFailed tickers: {', '.join(failed_tickers)}")
        if summary.get("failed", 0) > 0:
            sys.exit(1)


if __name__ == "__main__":
    main()