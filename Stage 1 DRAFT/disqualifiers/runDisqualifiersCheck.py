import asyncio
import json
import os
import logging
from datetime import datetime
from disqualifiersCheck import (
    fetch_ticker_cik_map,
    lookup_cik,
    check_company_filings_async,
)

# ============ CONFIG ============

# Concurrency for SEC API calls
SEC_CONCURRENCY = 5

# Lookback window for disqualifier checks (months)
LOOKBACK_MONTHS = 12

# Final ranking size
TOP_N_FINAL = 50

# ============ LOGGING ============

log_filename = f"disqualifier_log_{datetime.now().strftime('%Y-%m-%d')}.log"
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


# ============ JSON FIELD MANAGEMENT ============

DISQUALIFIER_FIELDS = {
    "disqualifier_flags": None,
    "disqualifier_check_date": "",
    "qualified": None,
}


def ensure_disqualifier_fields(company_data: dict) -> dict:
    for key, default in DISQUALIFIER_FIELDS.items():
        if key not in company_data:
            company_data[key] = default
    return company_data


# ============ HELPERS ============

def load_top_n_candidates(top_n_path: str) -> list:
    """Load the top-N candidates JSON produced by composite ranking."""
    with open(top_n_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("candidates", [])


def load_company_data(ticker: str, company_data_dir: str) -> dict:
    safe_ticker = ticker.replace("/", "-").replace(" ", "_")
    path = os.path.join(company_data_dir, f"{safe_ticker}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"[{ticker}] could not read {path}: {e}")
        return None


def write_company_data(company_data: dict, company_data_dir: str):
    ticker = company_data.get("ticker")
    if not ticker:
        return
    safe_ticker = ticker.replace("/", "-").replace(" ", "_")
    path = os.path.join(company_data_dir, f"{safe_ticker}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(company_data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"[{ticker}] could not write {path}: {e}")


def format_company_for_target_list(ticker: str, company_name: str) -> str:
    """Format as 'Company Name (TICKER)' for target_company_list.json."""
    return f"{company_name} ({ticker})"


# ============ DISQUALIFIER CHECK PIPELINE ============

async def check_one_candidate(candidate: dict, ticker_cik_map: dict, sem: asyncio.Semaphore) -> dict:
    """Run disqualifier check on one candidate. Returns the candidate dict augmented with check result."""
    async with sem:
        ticker = candidate["ticker"]
        cik = lookup_cik(ticker, ticker_cik_map)

        if cik is None:
            logger.warning(f"[{ticker}] no CIK found in SEC mapping")
        else:
            logger.info(f"[{ticker}] CIK={cik}, checking filings...")

        try:
            check_result = await check_company_filings_async(ticker, cik, LOOKBACK_MONTHS)
        except Exception as e:
            logger.error(f"[{ticker}] check failed: {e}")
            check_result = {
                "check_status": "error",
                "error_message": f"Exception: {e}",
                "bankruptcy_8k": {"triggered": False},
                "material_restatement_10ka": {"triggered": False},
                "qualified": None,
            }

        candidate["disqualifier_check_result"] = check_result

        if check_result.get("check_status") == "ok":
            if check_result.get("qualified"):
                logger.info(f"[{ticker}] PASS")
            else:
                triggered_flags = []
                if check_result["bankruptcy_8k"].get("triggered"):
                    triggered_flags.append("bankruptcy_8k")
                if check_result["material_restatement_10ka"].get("triggered"):
                    triggered_flags.append("material_restatement_10ka")
                logger.warning(f"[{ticker}] DISQUALIFIED: {', '.join(triggered_flags)}")
        else:
            logger.warning(f"[{ticker}] could not check: {check_result.get('error_message')}")

        return candidate


async def run_disqualifier_pass(candidates: list, ticker_cik_map: dict) -> list:
    """Run disqualifier checks on all candidates in parallel."""
    sem = asyncio.Semaphore(SEC_CONCURRENCY)
    print(f"Checking {len(candidates)} candidates against SEC EDGAR (concurrency={SEC_CONCURRENCY})...")
    tasks = [check_one_candidate(c, ticker_cik_map, sem) for c in candidates]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Filter out exception results, replacing with the original candidate
    cleaned = []
    for original, result in zip(candidates, results):
        if isinstance(result, Exception):
            logger.error(f"[{original.get('ticker')}] task failed: {result}")
            original["disqualifier_check_result"] = {
                "check_status": "error",
                "error_message": f"Task exception: {result}",
                "bankruptcy_8k": {"triggered": False},
                "material_restatement_10ka": {"triggered": False},
                "qualified": None,
            }
            cleaned.append(original)
        else:
            cleaned.append(result)
    return cleaned


# ============ MAIN ============

async def main():
    today_str = datetime.now().date().isoformat()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    stage1_root = os.path.join(script_dir, "..")
    output_dir = os.path.join(stage1_root, "output")
    company_data_dir = os.path.join(output_dir, "company_data")

    # Find the top-N candidates file
    top_n_path = os.path.join(output_dir, "top_75_candidates.json")
    if not os.path.exists(top_n_path):
        # Fallback: try to find any top_N_candidates.json
        fallback_candidates = [
            f for f in os.listdir(output_dir)
            if f.startswith("top_") and f.endswith("_candidates.json")
        ]
        if not fallback_candidates:
            print(f"Error: no top-N candidates file found in {output_dir}. Run composite ranking first.")
            return
        top_n_path = os.path.join(output_dir, fallback_candidates[0])

    candidates = load_top_n_candidates(top_n_path)
    if not candidates:
        print(f"Error: {top_n_path} contains no candidates.")
        return

    print(f"Loaded {len(candidates)} candidates from {os.path.basename(top_n_path)}.")

    # Fetch SEC ticker-to-CIK mapping
    print("Fetching SEC ticker-to-CIK mapping...")
    try:
        ticker_cik_map = fetch_ticker_cik_map()
    except RuntimeError as e:
        print(f"Error fetching ticker-to-CIK mapping: {e}")
        return

    # Run disqualifier checks
    candidates = await run_disqualifier_pass(candidates, ticker_cik_map)

    # Write disqualifier flags back to per-company JSONs
    print("Writing disqualifier flags to per-company JSONs...")
    for candidate in candidates:
        ticker = candidate["ticker"]
        check_result = candidate.get("disqualifier_check_result", {})

        company_data = load_company_data(ticker, company_data_dir)
        if company_data is None:
            continue

        company_data = ensure_disqualifier_fields(company_data)
        company_data["disqualifier_flags"] = {
            "bankruptcy_8k": check_result.get("bankruptcy_8k", {"triggered": False}),
            "material_restatement_10ka": check_result.get("material_restatement_10ka", {"triggered": False}),
            "check_status": check_result.get("check_status"),
            "error_message": check_result.get("error_message"),
        }
        company_data["disqualifier_check_date"] = today_str
        company_data["qualified"] = check_result.get("qualified")
        write_company_data(company_data, company_data_dir)

    # Build the final top 50
    # qualified=True -> include; qualified=False -> exclude (hard disqualified);
    # qualified=None -> include (treat "could not check" as soft flag, don't exclude)
    qualified_candidates = []
    disqualified_count = 0
    could_not_check_count = 0

    for candidate in candidates:
        result = candidate.get("disqualifier_check_result", {})
        qualified = result.get("qualified")
        if qualified is False:
            disqualified_count += 1
        elif qualified is None:
            could_not_check_count += 1
            qualified_candidates.append(candidate)
        else:
            qualified_candidates.append(candidate)

    # Take top N from qualified candidates
    final_candidates = qualified_candidates[:TOP_N_FINAL]

    print(f"\nResults:")
    print(f"  Total evaluated: {len(candidates)}")
    print(f"  Hard disqualified: {disqualified_count}")
    print(f"  Could not check: {could_not_check_count}")
    print(f"  Qualified (passed): {len(qualified_candidates)}")
    print(f"  Final top {TOP_N_FINAL}: {len(final_candidates)}")

    if len(final_candidates) < TOP_N_FINAL:
        print(f"  WARNING: only {len(final_candidates)} qualified — fewer than target {TOP_N_FINAL}")

    # Build top_50.json
    final_ranking = []
    for new_rank, candidate in enumerate(final_candidates, start=1):
        ticker = candidate["ticker"]
        company_name = candidate.get("company_name", "")
        company_data = load_company_data(ticker, company_data_dir) or {}
        check_result = candidate.get("disqualifier_check_result", {})

        final_ranking.append({
            "rank": new_rank,
            "original_composite_rank": candidate.get("rank"),
            "ticker": ticker,
            "company_name": company_name,
            "composite_score": candidate.get("composite_score"),
            "subscores": candidate.get("subscores", {}),
            "imputed_scores": candidate.get("imputed_scores", []),
            "disqualifier_flags": {
                "bankruptcy_8k": check_result.get("bankruptcy_8k", {"triggered": False}),
                "material_restatement_10ka": check_result.get("material_restatement_10ka", {"triggered": False}),
                "check_status": check_result.get("check_status"),
            },
        })

    top_50_path = os.path.join(output_dir, "top_50.json")
    with open(top_50_path, "w", encoding="utf-8") as f:
        json.dump({
            "ranking_date": today_str,
            "total_evaluated": len(candidates),
            "disqualified_count": disqualified_count,
            "could_not_check_count": could_not_check_count,
            "final_count": len(final_ranking),
            "ranking": final_ranking,
        }, f, indent=4, ensure_ascii=False)
    print(f"\nWrote {top_50_path}")

    # Build target_company_list.json (Stage 2's expected format)
    target_list = [
        format_company_for_target_list(c["ticker"], c.get("company_name", ""))
        for c in final_candidates
    ]
    target_list_path = os.path.join(output_dir, "target_company_list.json")
    with open(target_list_path, "w", encoding="utf-8") as f:
        json.dump(target_list, f, indent=4, ensure_ascii=False)
    print(f"Wrote {target_list_path}")

    print("\nDisqualifier filtering complete.")


if __name__ == "__main__":
    asyncio.run(main())