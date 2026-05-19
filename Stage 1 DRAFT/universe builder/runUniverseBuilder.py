import json
import os
import logging
from datetime import datetime
from universeBuilder import fetch_universe

# Configure logging
log_filename = f"universe_log_{datetime.now().strftime('%Y-%m-%d')}.log"
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


def stub_company_file(ticker: str, company_name: str, today_str: str) -> dict:
    """Build the minimal initial per-company JSON stub."""
    return {
        "ticker": ticker,
        "company_name": company_name,
        "universe_added_date": today_str,
    }


def main():
    today_str = datetime.now().date().isoformat()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    # Stage 1 root is one level up from /universe
    stage1_root = os.path.join(script_dir, "..")
    output_dir = os.path.join(stage1_root, "output")
    company_data_dir = os.path.join(output_dir, "company_data")
    universe_path = os.path.join(output_dir, "universe.json")

    os.makedirs(company_data_dir, exist_ok=True)

    # Skip re-fetch if universe.json was fetched today
    if os.path.exists(universe_path):
        try:
            with open(universe_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            existing_date = existing.get("fetched_date", "")
            if existing_date == today_str:
                logger.info(
                    f"Universe already fetched today ({existing_date}). "
                    f"Skipping re-fetch. {existing.get('company_count', 0)} companies on file."
                )
                return
        except Exception as e:
            logger.warning(f"Could not read existing universe.json: {e}. Will refetch.")

    # Fetch the universe
    print("Fetching Russell 1000 universe...")
    try:
        universe_data = fetch_universe()
    except Exception as e:
        logger.error(f"Universe fetch failed completely (both IWB and Wikipedia): {e}")
        raise

    logger.info(
        f"Fetched {universe_data['company_count']} companies from {universe_data['source']}."
    )

    # Write universe.json
    with open(universe_path, "w", encoding="utf-8") as f:
        json.dump(universe_data, f, indent=4, ensure_ascii=False)
    logger.info(f"Wrote universe.json to {universe_path}.")

    # Create stub per-company JSONs (only if not already present)
    created = 0
    skipped = 0
    for company in universe_data["companies"]:
        ticker = company["ticker"]
        name = company["company_name"]
        # Use ticker as filename (sanitize defensively)
        safe_ticker = ticker.replace("/", "-").replace(" ", "_")
        path = os.path.join(company_data_dir, f"{safe_ticker}.json")
        if os.path.exists(path):
            skipped += 1
            continue
        stub = stub_company_file(ticker, name, today_str)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(stub, f, indent=4, ensure_ascii=False)
        created += 1

    logger.info(
        f"Per-company JSON stubs: {created} created, {skipped} already existed."
    )
    logger.info("Universe builder complete.")


if __name__ == "__main__":
    main()