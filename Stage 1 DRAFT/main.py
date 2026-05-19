"""
Stage 1 main orchestrator.

Pipeline:
    1. Universe builder
    2. Three scoring modules in parallel (financial, professional, news sentiment)
    3. Composite ranking
    4. Disqualifier filters

Each stage has its own freshness check. If a stage's outputs are current relative to
its inputs, the stage is skipped. Otherwise it runs. If any stage fails (subprocess
exits with non-zero status), the pipeline stops — the next run will resume from the
failing stage.

Date-anchored cascading freshness:
    Universe is the root anchor (universe.fetched_date)
    Each scoring module is fresh if its outputs are dated >= universe.fetched_date
    Composite is fresh if dated >= max(scoring module dates)
    Disqualifier is fresh if dated >= composite ranking date
"""

import asyncio
import json
import os
import sys
import random
import subprocess
import logging
from datetime import datetime

# ============ CONFIG ============

# Sample size for detecting whether a per-company scoring module has completed
SAMPLE_SIZE_FOR_STAGE_FRESHNESS = 10

# ============ LOGGING ============

log_filename = f"stage1_main_log_{datetime.now().strftime('%Y-%m-%d')}.log"
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


# ============ PATH RESOLUTION ============

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
COMPANY_DATA_DIR = os.path.join(OUTPUT_DIR, "company_data")

# Each stage's run script lives in its own folder under SCRIPT_DIR
STAGE_PATHS = {
    "universe": os.path.join(SCRIPT_DIR, "universe builder", "runUniverseBuilder.py"),
    "financial_score": os.path.join(SCRIPT_DIR, "financial score", "runFinancialScoring.py"),
    "professional_score": os.path.join(SCRIPT_DIR, "professional score", "runProfessionalScoring.py"),
    "news_sentiment_score": os.path.join(SCRIPT_DIR, "news sentiment score", "runNewsSentimentScoring.py"),
    "composite_ranking": os.path.join(SCRIPT_DIR, "composite ranking", "runCompositeRanking.py"),
    "disqualifiers": os.path.join(SCRIPT_DIR, "disqualifiers", "runDisqualifiersCheck.py"),
}


# ============ FRESHNESS CHECKS ============

def get_universe_date() -> str:
    universe_path = os.path.join(OUTPUT_DIR, "universe.json")
    if not os.path.exists(universe_path):
        return ""
    try:
        with open(universe_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("fetched_date", "")
    except Exception as e:
        logger.warning(f"Could not read universe.json: {e}")
        return ""


def get_universe_companies() -> list:
    universe_path = os.path.join(OUTPUT_DIR, "universe.json")
    if not os.path.exists(universe_path):
        return []
    try:
        with open(universe_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("companies", [])
    except Exception as e:
        logger.warning(f"Could not read universe.json: {e}")
        return []


def is_universe_stale(today_str: str) -> bool:
    fetched_date = get_universe_date()
    if not fetched_date:
        return True
    return fetched_date < today_str


def is_scoring_module_stale(score_field_prefix: str, sample_size: int) -> bool:
    """
    Check if a scoring module is stale by sampling per-company JSONs.

    Returns True if the module needs to run.
    """
    universe_date = get_universe_date()
    if not universe_date:
        return True

    companies = get_universe_companies()
    if not companies:
        return True

    score_date_field = f"{score_field_prefix}_score_date"

    shuffled = list(companies)
    random.shuffle(shuffled)

    valid_samples = 0
    for company in shuffled:
        if valid_samples >= sample_size:
            break

        ticker = company.get("ticker")
        if not ticker:
            continue

        safe_ticker = ticker.replace("/", "-").replace(" ", "_")
        path = os.path.join(COMPANY_DATA_DIR, f"{safe_ticker}.json")
        if not os.path.exists(path):
            continue  # skip — no JSON to sample

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue

        score_date = data.get(score_date_field, "")
        valid_samples += 1

        if not score_date or score_date < universe_date:
            return True  # found a stale sample

    if valid_samples < sample_size:
        logger.info(
            f"Could only validate {valid_samples} of {sample_size} requested samples "
            f"for {score_field_prefix}; assuming stale."
        )
        return True

    return False


def is_composite_ranking_stale() -> bool:
    """Composite is stale if outputs missing or older than scoring module outputs."""
    composite_path = os.path.join(OUTPUT_DIR, "composite_ranking.json")
    top_75_path = os.path.join(OUTPUT_DIR, "top_75_candidates.json")

    if not os.path.exists(composite_path) or not os.path.exists(top_75_path):
        return True

    try:
        with open(composite_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        ranking_date = data.get("ranking_date", "")
    except Exception:
        return True

    if not ranking_date:
        return True

    universe_date = get_universe_date()
    if universe_date and ranking_date < universe_date:
        return True

    # Sample check: any per-company score_date newer than ranking_date means stale
    companies = get_universe_companies()
    if not companies:
        return True

    score_fields = ["financial", "professional", "news_sentiment"]
    shuffled = list(companies)
    random.shuffle(shuffled)

    checked = 0
    for company in shuffled:
        if checked >= 5:
            break
        ticker = company.get("ticker")
        if not ticker:
            continue
        safe_ticker = ticker.replace("/", "-").replace(" ", "_")
        path = os.path.join(COMPANY_DATA_DIR, f"{safe_ticker}.json")
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        for sf in score_fields:
            sd = data.get(f"{sf}_score_date", "")
            if sd and sd > ranking_date:
                return True
        checked += 1

    return False


def is_disqualifier_stale() -> bool:
    """Disqualifier is stale if outputs missing or older than composite ranking."""
    top_50_path = os.path.join(OUTPUT_DIR, "top_50.json")
    target_list_path = os.path.join(OUTPUT_DIR, "target_company_list.json")

    if not os.path.exists(top_50_path) or not os.path.exists(target_list_path):
        return True

    try:
        with open(top_50_path, "r", encoding="utf-8") as f:
            top_50_data = json.load(f)
        top_50_date = top_50_data.get("ranking_date", "")
    except Exception:
        return True

    if not top_50_date:
        return True

    composite_path = os.path.join(OUTPUT_DIR, "composite_ranking.json")
    if not os.path.exists(composite_path):
        return True

    try:
        with open(composite_path, "r", encoding="utf-8") as f:
            comp_data = json.load(f)
        comp_date = comp_data.get("ranking_date", "")
    except Exception:
        return True

    if comp_date and top_50_date < comp_date:
        return True

    return False


# ============ SUBPROCESS RUNNERS ============

def run_stage_subprocess(stage_name: str, script_path: str) -> bool:
    """
    Launch a single stage's run script as a subprocess.
    Returns True on success, False on failure.
    """
    if not os.path.exists(script_path):
        logger.error(f"[{stage_name}] script not found at {script_path}")
        return False

    script_dir = os.path.dirname(script_path)
    script_name = os.path.basename(script_path)

    logger.info(f"[{stage_name}] launching {script_name}...")
    print(f"\n{'='*60}\n  Running: {stage_name}\n{'='*60}")

    try:
        result = subprocess.run(
            [sys.executable, script_name],
            cwd=script_dir,
            check=False,
        )
    except Exception as e:
        logger.error(f"[{stage_name}] subprocess launch failed: {e}")
        return False

    if result.returncode != 0:
        logger.error(f"[{stage_name}] exited with code {result.returncode}")
        return False

    logger.info(f"[{stage_name}] completed successfully.")
    return True


async def run_stage_subprocess_async(stage_name: str, script_path: str) -> bool:
    return await asyncio.to_thread(run_stage_subprocess, stage_name, script_path)


async def run_parallel_stages(stage_specs: list) -> bool:
    """Run multiple stages in parallel. Returns True if ALL succeed."""
    print(f"\n{'='*60}\n  Running {len(stage_specs)} stages in parallel:")
    for name, _ in stage_specs:
        print(f"    - {name}")
    print(f"{'='*60}")

    tasks = [
        run_stage_subprocess_async(name, path)
        for name, path in stage_specs
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_succeeded = True
    for (name, _), result in zip(stage_specs, results):
        if isinstance(result, Exception):
            logger.error(f"[{name}] task raised exception: {result}")
            all_succeeded = False
        elif result is False:
            all_succeeded = False

    return all_succeeded


# ============ MAIN PIPELINE ============

async def main():
    today_str = datetime.now().date().isoformat()
    print(f"Stage 1 pipeline starting (today={today_str})")
    print(f"Output directory: {OUTPUT_DIR}")

    os.makedirs(COMPANY_DATA_DIR, exist_ok=True)

    # ---------- STAGE 1: Universe ----------
    if is_universe_stale(today_str):
        print(f"\n[Universe] stale or missing — running.")
        ok = run_stage_subprocess("universe", STAGE_PATHS["universe"])
        if not ok:
            print("\n[FAIL] Universe builder failed. Stopping pipeline.")
            sys.exit(1)
    else:
        print(f"\n[Universe] current (fetched {get_universe_date()}) — skipping.")

    # ---------- STAGE 2: Three scoring modules sequentially ----------
    # Sequential (not parallel) to avoid stacking yfinance concurrency across modules:
    # 3 parallel modules × 3 internal concurrency = 9 concurrent yfinance calls, which
    # triggers rate limiting. Running sequentially keeps it at 3 concurrent calls max.
    score_field_map = {
        "financial_score": "financial",
        "professional_score": "professional",
        "news_sentiment_score": "news_sentiment",
    }

    any_scoring_ran = False
    for stage_name, score_field in score_field_map.items():
        if is_scoring_module_stale(score_field, SAMPLE_SIZE_FOR_STAGE_FRESHNESS):
            print(f"\n[{stage_name}] stale or missing — running.")
            ok = run_stage_subprocess(stage_name, STAGE_PATHS[stage_name])
            if not ok:
                print(f"\n[FAIL] {stage_name} failed. Stopping pipeline.")
                sys.exit(1)
            any_scoring_ran = True
        else:
            print(f"[{stage_name}] current — skipping.")

    if not any_scoring_ran:
        print(f"\n[Scoring modules] all current — skipping.")

    # ---------- STAGE 3: Composite ranking ----------
    if is_composite_ranking_stale():
        print(f"\n[Composite ranking] stale or missing — running.")
        ok = run_stage_subprocess("composite_ranking", STAGE_PATHS["composite_ranking"])
        if not ok:
            print("\n[FAIL] Composite ranking failed. Stopping pipeline.")
            sys.exit(1)
    else:
        print(f"\n[Composite ranking] current — skipping.")

    # ---------- STAGE 4: Disqualifier filters ----------
    if is_disqualifier_stale():
        print(f"\n[Disqualifiers] stale or missing — running.")
        ok = run_stage_subprocess("disqualifiers", STAGE_PATHS["disqualifiers"])
        if not ok:
            print("\n[FAIL] Disqualifier filtering failed. Stopping pipeline.")
            sys.exit(1)
    else:
        print(f"\n[Disqualifiers] current — skipping.")

    # ---------- DONE ----------
    print(f"\n{'='*60}")
    print(f"  Stage 1 pipeline complete.")
    print(f"{'='*60}")
    print(f"\nFinal outputs:")
    print(f"  {os.path.join(OUTPUT_DIR, 'top_50.json')}")
    print(f"  {os.path.join(OUTPUT_DIR, 'target_company_list.json')}")


if __name__ == "__main__":
    asyncio.run(main())