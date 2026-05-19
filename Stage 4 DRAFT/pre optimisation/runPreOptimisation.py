"""
Stage 4 pre-optimization orchestrator.

Workflow:
    Phase 1 (sequential, external data): runPriceCache.py
    Phase 2 (parallel, local compute):
        - correlation analysis (reads cached returns)
        - sector analysis (reads Stage 3 JSONs)
        - macro analysis (reads cached returns)
    Phase 3 (sequential): aggregate into pre_optimization.json

Each sub-analysis writes its own file with a fixed name + analysis_date inside.
The aggregate file is rebuilt from the three sub-files whenever any of them
is newer than the aggregate.

Freshness rules:
    correlation_analysis.json -> stale if missing or analysis_date < today
    sector_analysis.json      -> stale if missing or analysis_date < latest_stage3_consolidation_date
    macro_analysis.json       -> stale if missing or analysis_date < today
    pre_optimization.json     -> stale if missing or analysis_date < max(sub-file dates)
"""

import os
import sys
import json
import asyncio
import logging
import subprocess
from datetime import datetime

# Allow importing from sibling subdirectories (analysis/ and price cache/)
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "analysis"))
sys.path.insert(0, os.path.join(_HERE, "price cache"))

from correlationAnalysis import compute_correlation_matrix
from sectorAnalysis import compute_sector_analysis
from macroAnalysis import compute_macro_analysis, FACTOR_PROXIES
from runPriceCache import load_candidate_tickers


# ============ CONFIG ============

# Configure logging
log_filename = f"pre_optimization_log_{datetime.now().strftime('%Y-%m-%d')}.log"
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
CACHE_DIR = os.path.join(STAGE4_ROOT, "cache", "prices")
STAGE3_OUTPUT_DIR = os.path.normpath(os.path.join(STAGE4_ROOT, "..", "Stage 3 DRAFT", "output"))

CORRELATION_PATH = os.path.join(OUTPUT_DIR, "correlation_analysis.json")
SECTOR_PATH = os.path.join(OUTPUT_DIR, "sector_analysis.json")
MACRO_PATH = os.path.join(OUTPUT_DIR, "macro_analysis.json")
AGGREGATE_PATH = os.path.join(OUTPUT_DIR, "pre_optimization.json")

PRICE_CACHE_SCRIPT = os.path.join(SCRIPT_DIR, "price cache", "runPriceCache.py")


# ============ FRESHNESS HELPERS ============

def _read_analysis_date(file_path: str) -> str:
    """Read the analysis_date field from a sub-analysis JSON. Returns '' if missing."""
    if not os.path.exists(file_path):
        return ""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f).get("analysis_date", "")
    except Exception:
        return ""


def _latest_stage3_consolidation_date(stage3_dir: str) -> str:
    """Find the most recent consolidation_date across all Stage 3 per-company JSONs."""
    latest = ""
    if not os.path.isdir(stage3_dir):
        return latest
    for fname in os.listdir(stage3_dir):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(stage3_dir, fname), "r", encoding="utf-8") as f:
                data = json.load(f)
            d = data.get("consolidation_date", "")
            if d and d > latest:
                latest = d
        except Exception:
            continue
    return latest


def is_correlation_stale(today_str: str) -> bool:
    d = _read_analysis_date(CORRELATION_PATH)
    return (not d) or (d < today_str)


def is_sector_stale(stage3_dir: str) -> bool:
    d = _read_analysis_date(SECTOR_PATH)
    if not d:
        return True
    latest_input = _latest_stage3_consolidation_date(stage3_dir)
    if not latest_input:
        return False  # nothing newer to be stale relative to
    return d < latest_input


def is_macro_stale(today_str: str) -> bool:
    d = _read_analysis_date(MACRO_PATH)
    return (not d) or (d < today_str)


def is_aggregate_stale() -> bool:
    agg_date = _read_analysis_date(AGGREGATE_PATH)
    if not agg_date:
        return True
    sub_dates = [
        _read_analysis_date(CORRELATION_PATH),
        _read_analysis_date(SECTOR_PATH),
        _read_analysis_date(MACRO_PATH),
    ]
    max_sub = max([d for d in sub_dates if d], default="")
    if not max_sub:
        return True
    return agg_date < max_sub


# ============ SUB-ANALYSIS RUNNERS ============

def run_price_cache_step() -> bool:
    """Run runPriceCache.py as a subprocess. Returns True on success."""
    if not os.path.exists(PRICE_CACHE_SCRIPT):
        logger.error(f"Price cache script not found at {PRICE_CACHE_SCRIPT}")
        return False
    print(f"\n{'='*60}\n  Phase 1: Updating price cache\n{'='*60}")
    try:
        result = subprocess.run(
            [sys.executable, PRICE_CACHE_SCRIPT],
            cwd=os.path.dirname(PRICE_CACHE_SCRIPT),
            check=False,
        )
    except Exception as e:
        logger.error(f"Price cache subprocess launch failed: {e}")
        return False
    if result.returncode != 0:
        logger.error(f"Price cache exited with code {result.returncode}")
        return False
    return True


def run_correlation_step(tickers: list, today_str: str) -> bool:
    """Compute correlation analysis and write file. Returns True on success."""
    print(f"\n[Correlation analysis] computing...")
    try:
        result = compute_correlation_matrix(tickers, CACHE_DIR)
    except Exception as e:
        logger.error(f"Correlation analysis failed: {e}")
        return False

    result["analysis_date"] = today_str

    try:
        with open(CORRELATION_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Could not write {CORRELATION_PATH}: {e}")
        return False

    n_tickers = len(result.get("tickers", []))
    n_flags = len(result.get("data_quality_flags", []))
    print(f"[Correlation analysis] done — {n_tickers} tickers, {n_flags} data quality flag(s).")
    return True


def run_sector_step(today_str: str) -> bool:
    """Compute sector analysis and write file. Returns True on success."""
    print(f"\n[Sector analysis] computing...")
    try:
        result = compute_sector_analysis(STAGE3_OUTPUT_DIR)
    except Exception as e:
        logger.error(f"Sector analysis failed: {e}")
        return False

    result["analysis_date"] = today_str

    try:
        with open(SECTOR_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Could not write {SECTOR_PATH}: {e}")
        return False

    n_companies = len(result.get("tickers_to_sector", {}))
    n_sectors = len(result.get("sector_breakdown", {}))
    n_flags = len(result.get("data_quality_flags", []))
    print(f"[Sector analysis] done — {n_companies} companies across {n_sectors} sectors, "
          f"{n_flags} data quality flag(s).")
    return True


def run_macro_step(tickers: list, today_str: str) -> bool:
    """Compute macro factor analysis and write file. Returns True on success."""
    print(f"\n[Macro analysis] computing...")
    try:
        result = compute_macro_analysis(tickers, CACHE_DIR)
    except Exception as e:
        logger.error(f"Macro analysis failed: {e}")
        return False

    result["analysis_date"] = today_str

    try:
        with open(MACRO_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Could not write {MACRO_PATH}: {e}")
        return False

    n_companies = len(result.get("per_company_betas", {}))
    n_pairs = len(result.get("pairwise_similarity", {}).get("cosine", {}))
    n_flags = len(result.get("data_quality_flags", []))
    print(f"[Macro analysis] done — {n_companies} companies, {n_pairs} pairwise similarities, "
          f"{n_flags} data quality flag(s).")
    return True


# ============ PARALLEL DISPATCH FOR PHASE 2 ============

async def run_phase_2(tickers: list, today_str: str, what_to_run: dict) -> bool:
    """Run the three analysis steps in parallel (where indicated by what_to_run dict)."""
    tasks = []
    if what_to_run.get("correlation"):
        tasks.append(asyncio.to_thread(run_correlation_step, tickers, today_str))
    if what_to_run.get("sector"):
        tasks.append(asyncio.to_thread(run_sector_step, today_str))
    if what_to_run.get("macro"):
        tasks.append(asyncio.to_thread(run_macro_step, tickers, today_str))

    if not tasks:
        return True

    results = await asyncio.gather(*tasks, return_exceptions=True)
    all_ok = True
    for r in results:
        if isinstance(r, Exception):
            logger.error(f"Phase 2 task raised exception: {r}")
            all_ok = False
        elif r is False:
            all_ok = False
    return all_ok


# ============ AGGREGATION ============

def aggregate_pre_optimization(today_str: str) -> bool:
    """Read the three sub-analysis files and combine into pre_optimization.json."""
    print(f"\n[Aggregating pre_optimization.json]...")

    if not os.path.exists(CORRELATION_PATH):
        logger.error("correlation_analysis.json missing; cannot aggregate")
        return False
    if not os.path.exists(SECTOR_PATH):
        logger.error("sector_analysis.json missing; cannot aggregate")
        return False
    if not os.path.exists(MACRO_PATH):
        logger.error("macro_analysis.json missing; cannot aggregate")
        return False

    try:
        with open(CORRELATION_PATH, "r", encoding="utf-8") as f:
            corr = json.load(f)
        with open(SECTOR_PATH, "r", encoding="utf-8") as f:
            sec = json.load(f)
        with open(MACRO_PATH, "r", encoding="utf-8") as f:
            mac = json.load(f)
    except Exception as e:
        logger.error(f"Could not read sub-analysis file: {e}")
        return False

    aggregate = {
        "analysis_date": today_str,
        "correlation_analysis": corr,
        "sector_analysis": sec,
        "macro_analysis": mac,
    }

    try:
        with open(AGGREGATE_PATH, "w", encoding="utf-8") as f:
            json.dump(aggregate, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Could not write {AGGREGATE_PATH}: {e}")
        return False

    print(f"[Aggregate] written to {AGGREGATE_PATH}.")
    return True


# ============ MAIN ============

async def main():
    today_str = datetime.now().date().isoformat()
    print(f"Stage 4 pre-optimization starting (today={today_str})")
    print(f"Output directory: {OUTPUT_DIR}")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    # Load candidate tickers from Stage 3 output
    candidate_tickers = load_candidate_tickers(STAGE3_OUTPUT_DIR)
    if not candidate_tickers:
        print(f"Error: no candidate tickers found in {STAGE3_OUTPUT_DIR}")
        sys.exit(1)
    print(f"\n{len(candidate_tickers)} candidate tickers loaded from Stage 3.")

    # ---------- PHASE 1: Price cache (always run; cache itself handles freshness) ----------
    ok = run_price_cache_step()
    if not ok:
        print("\n[FAIL] Price cache update failed. Stopping pipeline.")
        sys.exit(1)

    # ---------- PHASE 2: Three sub-analyses (parallel, only where stale) ----------
    what_to_run = {
        "correlation": is_correlation_stale(today_str),
        "sector": is_sector_stale(STAGE3_OUTPUT_DIR),
        "macro": is_macro_stale(today_str),
    }

    print(f"\nPhase 2 freshness check:")
    for name, stale in what_to_run.items():
        status = "STALE — will run" if stale else "current — skipping"
        print(f"  {name}: {status}")

    if any(what_to_run.values()):
        ok = await run_phase_2(candidate_tickers, today_str, what_to_run)
        if not ok:
            print("\n[FAIL] One or more analyses failed. Stopping pipeline.")
            sys.exit(1)
    else:
        print(f"\nPhase 2: all analyses current — skipped.")

    # ---------- PHASE 3: Aggregate ----------
    if is_aggregate_stale():
        ok = aggregate_pre_optimization(today_str)
        if not ok:
            print("\n[FAIL] Aggregation failed. Stopping pipeline.")
            sys.exit(1)
    else:
        print(f"\n[Aggregate] pre_optimization.json current — skipped.")

    # ---------- DONE ----------
    print(f"\n{'='*60}")
    print(f"  Stage 4 pre-optimization complete.")
    print(f"{'='*60}")
    print(f"\nOutputs:")
    print(f"  {CORRELATION_PATH}")
    print(f"  {SECTOR_PATH}")
    print(f"  {MACRO_PATH}")
    print(f"  {AGGREGATE_PATH}")


if __name__ == "__main__":
    asyncio.run(main())