import asyncio
import json
import os
import logging
from datetime import datetime
from professionalDataExtraction import extract_raw_metrics_async
from professionalScoring import compute_peer_stats, score_company

# Concurrency cap for parallel yfinance fetches.
COMPANY_CONCURRENCY = 3

# Configure logging
log_filename = f"professional_scoring_log_{datetime.now().strftime('%Y-%m-%d')}.log"
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


# Fields this module owns
PROFESSIONAL_FIELDS = {
    "professional_raw_metrics": None,
    "professional_raw_metrics_date": "",
    "professional_score": None,
    "professional_score_date": "",
    "professional_subscores": None,
    "professional_drivers": None,
}


def ensure_professional_fields(company_data: dict) -> dict:
    """Add Stage 1 professional fields to JSON if they don't exist. Idempotent."""
    for key, default in PROFESSIONAL_FIELDS.items():
        if key not in company_data:
            company_data[key] = default
    return company_data


def needs_extraction(company_data: dict, universe_date: str) -> bool:
    """Pass 1 freshness."""
    raw = company_data.get("professional_raw_metrics")
    if not raw:
        return True
    raw_date = company_data.get("professional_raw_metrics_date", "")
    if not raw_date:
        return True
    if raw_date < universe_date:
        return True
    return False


def needs_scoring(company_data: dict) -> bool:
    """Pass 2 freshness."""
    score = company_data.get("professional_score")
    if score is None:
        return True
    score_date = company_data.get("professional_score_date", "")
    if not score_date:
        return True
    raw_date = company_data.get("professional_raw_metrics_date", "")
    if not raw_date:
        return False
    if score_date < raw_date:
        return True
    return False


# ============ PASS 1: EXTRACTION ============

async def extract_one(ticker: str, file_name: str, today_str: str, universe_date: str, sem: asyncio.Semaphore):
    async with sem:
        try:
            with open(file_name, "r", encoding="utf-8") as f:
                company_data = json.load(f)
        except Exception as e:
            logger.warning(f"[{ticker}] could not read {file_name}: {e}")
            return

        company_data = ensure_professional_fields(company_data)

        if not needs_extraction(company_data, universe_date):
            return

        try:
            raw_metrics = await extract_raw_metrics_async(ticker)
        except Exception as e:
            logger.error(f"[{ticker}] raw metrics extraction failed: {e}")
            return

        company_data["professional_raw_metrics"] = raw_metrics
        company_data["professional_raw_metrics_date"] = today_str

        try:
            with open(file_name, "w", encoding="utf-8") as f:
                json.dump(company_data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[{ticker}] could not write {file_name}: {e}")
            return

        flag_count = len(raw_metrics.get("data_quality_flags", []))
        if flag_count > 0:
            logger.info(f"[{ticker}] raw metrics extracted ({flag_count} flag(s))")
        else:
            logger.info(f"[{ticker}] raw metrics extracted")


async def run_extraction_pass(companies: list, company_data_dir: str, today_str: str, universe_date: str):
    sem = asyncio.Semaphore(COMPANY_CONCURRENCY)
    tasks = []
    for company in companies:
        ticker = company["ticker"]
        safe_ticker = ticker.replace("/", "-").replace(" ", "_")
        file_name = os.path.join(company_data_dir, f"{safe_ticker}.json")
        if not os.path.exists(file_name):
            logger.warning(f"[{ticker}] no per-company JSON found at {file_name}; skipping")
            continue
        tasks.append(extract_one(ticker, file_name, today_str, universe_date, sem))

    if not tasks:
        logger.info("Pass 1: nothing to extract.")
        return

    print(f"Pass 1: extracting professional metrics for up to {len(tasks)} companies (concurrency={COMPANY_CONCURRENCY})...")
    await asyncio.gather(*tasks, return_exceptions=True)
    print(f"Pass 1: extraction complete.")


# ============ PASS 2: SCORING ============

def load_all_raw_metrics(companies: list, company_data_dir: str) -> dict:
    out = {}
    for company in companies:
        ticker = company["ticker"]
        safe_ticker = ticker.replace("/", "-").replace(" ", "_")
        file_name = os.path.join(company_data_dir, f"{safe_ticker}.json")
        if not os.path.exists(file_name):
            out[ticker] = None
            continue
        try:
            with open(file_name, "r", encoding="utf-8") as f:
                data = json.load(f)
            out[ticker] = data.get("professional_raw_metrics")
        except Exception as e:
            logger.warning(f"[{ticker}] could not read raw metrics: {e}")
            out[ticker] = None
    return out


def score_one(ticker: str, file_name: str, peer_stats: dict, today_str: str):
    try:
        with open(file_name, "r", encoding="utf-8") as f:
            company_data = json.load(f)
    except Exception as e:
        logger.warning(f"[{ticker}] could not read {file_name}: {e}")
        return

    company_data = ensure_professional_fields(company_data)

    if not needs_scoring(company_data):
        return

    raw_metrics = company_data.get("professional_raw_metrics")
    if not raw_metrics:
        logger.warning(f"[{ticker}] no raw metrics available; skipping scoring")
        return

    try:
        result = score_company(raw_metrics, peer_stats)
    except Exception as e:
        logger.error(f"[{ticker}] scoring failed: {e}")
        return

    company_data["professional_score"] = result["score"]
    company_data["professional_score_date"] = today_str
    company_data["professional_subscores"] = result["subscores"]
    company_data["professional_drivers"] = result["drivers"]

    try:
        with open(file_name, "w", encoding="utf-8") as f:
            json.dump(company_data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"[{ticker}] could not write {file_name}: {e}")
        return

    score = result["score"]
    if score is None:
        logger.info(f"[{ticker}] scored: insufficient data (no composite score)")
    else:
        logger.info(f"[{ticker}] scored: {score:.1f}")


def run_scoring_pass(companies: list, company_data_dir: str, today_str: str):
    print(f"Pass 2: loading raw metrics for peer stats...")
    all_raw = load_all_raw_metrics(companies, company_data_dir)

    populated_count = sum(1 for v in all_raw.values() if v is not None)
    print(f"Pass 2: {populated_count} of {len(companies)} companies have raw metrics.")

    if populated_count == 0:
        print("Pass 2: no raw metrics available; nothing to score.")
        return

    print(f"Pass 2: computing peer statistics...")
    peer_stats = compute_peer_stats(all_raw)

    industry_count = len(peer_stats.get("by_industry", {}))
    sector_count = len(peer_stats.get("by_sector", {}))
    print(f"Pass 2: peer stats computed for {industry_count} industries and {sector_count} sectors.")

    print(f"Pass 2: scoring companies...")
    scored = 0
    for company in companies:
        ticker = company["ticker"]
        safe_ticker = ticker.replace("/", "-").replace(" ", "_")
        file_name = os.path.join(company_data_dir, f"{safe_ticker}.json")
        if not os.path.exists(file_name):
            continue
        score_one(ticker, file_name, peer_stats, today_str)
        scored += 1

    print(f"Pass 2: scoring complete ({scored} companies processed).")


# ============ MAIN ============

async def main():
    today_str = datetime.now().date().isoformat()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    stage1_root = os.path.join(script_dir, "..")
    output_dir = os.path.join(stage1_root, "output")
    company_data_dir = os.path.join(output_dir, "company_data")
    universe_path = os.path.join(output_dir, "universe.json")

    if not os.path.exists(universe_path):
        print(f"Error: universe.json not found at {universe_path}. Run universeBuilder first.")
        return

    with open(universe_path, "r", encoding="utf-8") as f:
        universe = json.load(f)

    companies = universe.get("companies", [])
    universe_date = universe.get("fetched_date", "")
    if not companies:
        print("Error: universe.json has no companies.")
        return
    if not universe_date:
        print("Warning: universe.json has no fetched_date; using today as anchor.")
        universe_date = today_str

    print(f"Stage 1 professional scoring: {len(companies)} companies (universe fetched {universe_date}).")

    await run_extraction_pass(companies, company_data_dir, today_str, universe_date)
    run_scoring_pass(companies, company_data_dir, today_str)

    print("Professional scoring complete.")


if __name__ == "__main__":
    asyncio.run(main())