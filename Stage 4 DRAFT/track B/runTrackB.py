"""
Workflow script for Track B — Pure LLM Portfolio Construction.

Reads:
    - Stage 3 outputs (structured fields only — NOT the raw narratives)
    - candidate_summaries.json (decision-ready summary per company)
    - pre_optimization.json
    - track_b_construction.md (system prompt)

Calls Track B's functional logic, writes track_b_portfolio.json.

Freshness check: re-runs if track_b_portfolio.json is missing OR older than:
    - the latest Stage 3 consolidation_date, OR
    - candidate_summaries analysis_date, OR
    - pre_optimization analysis_date.
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

from trackB import (
    construct_track_b_portfolio,
    PORTFOLIO_SIZE,
    MIN_POSITION_WEIGHT,
    MAX_POSITION_WEIGHT,
    SECTOR_CAP,
    MODEL,
)


# ============ LOGGING ============

log_filename = f"track_b_log_{datetime.now().strftime('%Y-%m-%d')}.log"
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
TRACK_B_PATH = os.path.join(OUTPUT_DIR, "track_b_portfolio.json")
PRE_OPT_PATH = os.path.join(OUTPUT_DIR, "pre_optimization.json")
SUMMARIES_PATH = os.path.join(OUTPUT_DIR, "candidate_summaries.json")
STAGE3_OUTPUT_DIR = os.path.normpath(os.path.join(STAGE4_ROOT, "..", "Stage 3 DRAFT", "output"))
PROMPT_PATH = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "prompts", "trackB_prompt.md"))


# ============ HELPERS ============

def extract_ticker_from_company_name(company_name: str) -> str:
    if not company_name:
        return None
    match = re.search(r"\(([A-Z0-9\-\.]+)\)\s*$", company_name)
    return match.group(1) if match else None


def load_system_prompt() -> str:
    if not os.path.exists(PROMPT_PATH):
        raise FileNotFoundError(f"Prompt template not found at {PROMPT_PATH}")
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()


def load_candidates() -> list:
    """Load Stage 3 candidates — structured fields only (NO raw narratives)."""
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
            "key_invalidation_triggers": data.get("key_invalidation_triggers"),
            "consolidation_date": data.get("consolidation_date", ""),
        })

    return candidates


def load_summaries() -> tuple:
    """
    Load candidate_summaries.json.
    Returns (summaries_by_ticker_dict, analysis_date_string).
    Raises FileNotFoundError if missing.
    """
    if not os.path.exists(SUMMARIES_PATH):
        raise FileNotFoundError(
            f"Candidate summaries not found at {SUMMARIES_PATH}. "
            f"Run runCandidateSummaries.py first."
        )
    with open(SUMMARIES_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    raw = data.get("summaries", {})
    # Convert {ticker: {summary, source_date, error}} -> {ticker: summary_text}
    out = {}
    for ticker, entry in raw.items():
        out[ticker] = entry.get("summary", "")
    return out, data.get("analysis_date", "")


def load_pre_optimization() -> dict:
    if not os.path.exists(PRE_OPT_PATH):
        raise FileNotFoundError(f"Pre-optimization output not found at {PRE_OPT_PATH}")
    with open(PRE_OPT_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ============ FRESHNESS ============

def is_track_b_stale(candidates: list, summaries_date: str, pre_opt: dict) -> bool:
    if not os.path.exists(TRACK_B_PATH):
        return True

    try:
        with open(TRACK_B_PATH, "r", encoding="utf-8") as f:
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

    if summaries_date and existing_date < summaries_date:
        return True

    pre_opt_date = pre_opt.get("analysis_date", "")
    if pre_opt_date and existing_date < pre_opt_date:
        return True

    return False


# ============ MAIN ============

async def main():
    today_str = datetime.now().date().isoformat()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("Stage 4 Track B (Pure LLM) starting...")

    candidates = load_candidates()
    if not candidates:
        print("Error: no candidates loaded.")
        sys.exit(1)
    print(f"Loaded {len(candidates)} candidates from Stage 3.")

    try:
        summaries_by_ticker, summaries_date = load_summaries()
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)
    print(f"Loaded candidate summaries (analysis_date={summaries_date}, "
          f"n_summaries={sum(1 for s in summaries_by_ticker.values() if s)}).")

    pre_opt = load_pre_optimization()
    print(f"Loaded pre-optimization data (analysis_date={pre_opt.get('analysis_date', '')}).")

    # Warn if any candidates lack summaries
    missing_summaries = [c["ticker"] for c in candidates if not summaries_by_ticker.get(c["ticker"])]
    if missing_summaries:
        print(f"WARNING: {len(missing_summaries)} candidates have no summary: {missing_summaries}")

    # Freshness check
    if not is_track_b_stale(candidates, summaries_date, pre_opt):
        print("[Track B] portfolio is current — skipping.")
        return

    system_prompt = load_system_prompt()
    print(f"Loaded system prompt ({len(system_prompt)} chars).")

    print("\n[Track B] running LLM portfolio construction...")
    try:
        result = await construct_track_b_portfolio(
            candidates, summaries_by_ticker, pre_opt, system_prompt
        )
    except Exception as e:
        print(f"Track B failed: {e}")
        logger.exception("Track B failed")
        sys.exit(1)

    # Build output dict
    output = {
        "track": "B",
        "method": "Pure LLM (Kimi K2.6)",
        "analysis_date": today_str,
        "model": result["model"],
        "n_input_candidates": len(candidates),
        "constraints": {
            "portfolio_size": PORTFOLIO_SIZE,
            "min_position_weight": MIN_POSITION_WEIGHT,
            "max_position_weight": MAX_POSITION_WEIGHT,
            "sector_cap": SECTOR_CAP,
            "base_return_filter": "base_return_12m > 0",
        },
        "constraint_violations": result["constraint_violations"],
        "portfolio": result.get("portfolio") or {},
    }

    try:
        with open(TRACK_B_PATH, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"Error writing {TRACK_B_PATH}: {e}")
        sys.exit(1)

    # Summary
    print(f"\n{'='*60}")
    print("  Track B complete.")
    print(f"{'='*60}")

    violations = result["constraint_violations"]
    print(f"\nConstraint violations: {len(violations)}")
    if violations:
        for v in violations:
            print(f"  - {v}")
    else:
        positions = output["portfolio"].get("positions", [])
        print(f"Positions ({len(positions)}):")
        for p in positions:
            tk = p.get("ticker", "?")
            wt = p.get("allocation_pct", 0)
            print(f"  {tk:6s}  {wt:6.2f}%")

        rejections = output["portfolio"].get("notable_rejections", [])
        if rejections:
            print(f"\nNotable rejections: {len(rejections)}")
            for r in rejections:
                print(f"  - {r.get('ticker', '?')}: {r.get('rationale', '')[:80]}")

    print(f"\nWritten to {TRACK_B_PATH}")


if __name__ == "__main__":
    asyncio.run(main())