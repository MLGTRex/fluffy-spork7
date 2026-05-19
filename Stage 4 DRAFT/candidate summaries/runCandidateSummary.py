"""
Workflow script for Stage 4 candidate summaries.

For each Stage 3 per-company JSON, generate a decision-ready summary via LLM.
Runs candidates in parallel with concurrency control to match the Stage 2/3 pattern.

Freshness check: per-candidate, re-runs only candidates where:
    - No summary exists in candidate_summaries.json, OR
    - The summary's source_date is older than the candidate's consolidation_date
"""

import os
import sys
import json
import re
import asyncio
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# Allow importing siblings
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from candidateSummary import summarize_candidate


# ============ CONFIG ============

# Parallel LLM calls (matches Stage 2/3 default)
COMPANY_CONCURRENCY = 10


# ============ LOGGING ============

log_filename = f"candidate_summaries_log_{datetime.now().strftime('%Y-%m-%d')}.log"
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
SUMMARIES_PATH = os.path.join(OUTPUT_DIR, "candidate_summaries.json")
STAGE3_OUTPUT_DIR = os.path.normpath(os.path.join(STAGE4_ROOT, "..", "Stage 3 DRAFT", "output"))
PROMPT_PATH = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "prompts", "candidate_summary.md"))


# ============ HELPERS ============

def extract_ticker_from_company_name(company_name: str) -> str:
    if not company_name:
        return None
    match = re.search(r"\(([A-Z0-9\-\.]+)\)\s*$", company_name)
    return match.group(1) if match else None


def load_prompt_template() -> str:
    if not os.path.exists(PROMPT_PATH):
        raise FileNotFoundError(f"Prompt template not found at {PROMPT_PATH}")
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()


def load_candidates() -> list:
    """Load all Stage 3 per-company JSONs and extract everything the summary needs."""
    candidates = []
    if not os.path.isdir(STAGE3_OUTPUT_DIR):
        raise FileNotFoundError(f"Stage 3 output dir not found: {STAGE3_OUTPUT_DIR}")

    for fname in sorted(os.listdir(STAGE3_OUTPUT_DIR)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(STAGE3_OUTPUT_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"Could not read {fname}: {e}")
            continue

        ticker = data.get("ticker") or extract_ticker_from_company_name(data.get("company_name", ""))
        if not ticker:
            logger.warning(f"Could not extract ticker from {fname}")
            continue

        vm = data.get("valuation_metrics") or {}
        sector = vm.get("sector") if isinstance(vm, dict) else None
        industry = vm.get("industry") if isinstance(vm, dict) else None

        candidates.append({
            "ticker": ticker,
            "company_name": data.get("company_name", ""),
            "sector": sector,
            "industry": industry,
            "conviction": data.get("conviction"),
            "expected_return_12m": data.get("expected_return_12m"),
            "base_return_12m": data.get("base_return_12m"),
            "upside_return_12m": data.get("upside_return_12m"),
            "downside_return_12m": data.get("downside_return_12m"),
            "scenario_probability_bull": data.get("scenario_probability_bull"),
            "scenario_probability_base": data.get("scenario_probability_base"),
            "scenario_probability_bear": data.get("scenario_probability_bear"),
            "scenario_bull": data.get("scenario_bull"),
            "scenario_bear": data.get("scenario_bear"),
            "scenario_base_final": data.get("scenario_base_final"),
            "consolidation": data.get("consolidation"),
            "consolidation_date": data.get("consolidation_date", ""),
        })

    return candidates


def load_existing_summaries() -> dict:
    """Load the existing summaries file if present. Returns full dict or empty structure."""
    if not os.path.exists(SUMMARIES_PATH):
        return {"analysis_date": "", "model": "", "summaries": {}}
    try:
        with open(SUMMARIES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Could not read existing summaries: {e}; treating as empty")
        return {"analysis_date": "", "model": "", "summaries": {}}


def save_summaries(summaries_data: dict):
    try:
        with open(SUMMARIES_PATH, "w", encoding="utf-8") as f:
            json.dump(summaries_data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Could not write {SUMMARIES_PATH}: {e}")


def needs_summary(candidate: dict, existing_summaries: dict) -> bool:
    """
    A candidate needs a fresh summary if:
        - No existing entry for this ticker, OR
        - The existing entry's source_date is older than the candidate's consolidation_date, OR
        - The existing summary is empty/error
    """
    ticker = candidate.get("ticker")
    entry = existing_summaries.get(ticker)
    if not entry:
        return True
    if not entry.get("summary"):
        return True
    existing_source = entry.get("source_date", "")
    current_source = candidate.get("consolidation_date", "")
    if not existing_source:
        return True
    if current_source and existing_source < current_source:
        return True
    return False


# ============ PER-CANDIDATE PROCESSING ============

async def process_one(candidate: dict, prompt_template: str, sem: asyncio.Semaphore) -> dict:
    """Run summarize_candidate for one candidate, respecting the concurrency cap."""
    async with sem:
        result = await summarize_candidate(candidate, prompt_template)
        # Tag the result with the source date so freshness checks work
        result["source_date"] = candidate.get("consolidation_date", "")
        return result


# ============ MAIN ============

async def main():
    today_str = datetime.now().date().isoformat()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("Stage 4 Candidate Summaries starting...")

    candidates = load_candidates()
    if not candidates:
        print("Error: no candidates loaded from Stage 3.")
        sys.exit(1)
    print(f"Loaded {len(candidates)} candidates from Stage 3.")

    prompt_template = load_prompt_template()
    print(f"Loaded prompt template ({len(prompt_template)} chars).")

    existing = load_existing_summaries()
    existing_summaries = existing.get("summaries", {})

    # Determine which candidates need fresh summaries
    to_run = [c for c in candidates if needs_summary(c, existing_summaries)]
    to_skip = [c for c in candidates if not needs_summary(c, existing_summaries)]

    print(f"\nFreshness check:")
    print(f"  {len(to_run)} candidates need summaries")
    print(f"  {len(to_skip)} candidates already current — skipping")

    if not to_run:
        print("\nAll summaries current — nothing to do.")
        return

    # Run summaries in parallel
    print(f"\nGenerating summaries (concurrency={COMPANY_CONCURRENCY})...")
    sem = asyncio.Semaphore(COMPANY_CONCURRENCY)
    tasks = [process_one(c, prompt_template, sem) for c in to_run]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Merge results into existing summaries
    updated_summaries = dict(existing_summaries)  # copy
    successes = 0
    failures = []
    for original, result in zip(to_run, results):
        ticker = original.get("ticker")
        if isinstance(result, Exception):
            logger.error(f"[{ticker}] task raised exception: {result}")
            failures.append((ticker, f"Task exception: {result}"))
            continue
        if result.get("error"):
            failures.append((ticker, result["error"]))
            # Still update the entry so it can be retried next run
            updated_summaries[ticker] = {
                "summary": "",
                "source_date": result.get("source_date", ""),
                "error": result["error"],
            }
            continue
        updated_summaries[ticker] = {
            "summary": result["summary"],
            "source_date": result.get("source_date", ""),
            "error": None,
        }
        successes += 1

    # Save
    output = {
        "analysis_date": today_str,
        "model": "kimi-k2.6",
        "n_candidates": len(candidates),
        "n_summaries": len([s for s in updated_summaries.values() if s.get("summary")]),
        "summaries": updated_summaries,
    }
    save_summaries(output)

    # Summary
    print(f"\n{'='*60}")
    print("  Candidate Summaries complete.")
    print(f"{'='*60}")
    print(f"\nSummaries generated this run: {successes}")
    print(f"Failures: {len(failures)}")
    if failures:
        for ticker, err in failures:
            print(f"  - {ticker}: {err}")
    print(f"Total summaries on file: {output['n_summaries']} of {len(candidates)} candidates")
    print(f"\nWritten to {SUMMARIES_PATH}")


if __name__ == "__main__":
    asyncio.run(main())