import asyncio
import json
import datetime
import os
import re
import sys
from valuationMetrics import run_valuation_metrics

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pipeline tools"))
from pipeline_git import commit_company_progress

COMPANY_CONCURRENCY = 5

# Required upstream field — must be present to anchor the freshness check
REQUIRED_INPUT_FIELDS = [
    "synthesis",
    "synthesis_date",
]

# New Stage 3a fields written to each company JSON
VALUATION_FIELDS = {
    "valuation_metrics_date": "",
    "valuation_metrics": None,
}


def ensure_valuation_fields(company_data: dict) -> dict:
    """Add Stage 3a fields to JSON if they don't exist. Idempotent."""
    for key, default in VALUATION_FIELDS.items():
        if key not in company_data:
            company_data[key] = default
    return company_data


def required_inputs_valid(company_data: dict, target_company: str) -> bool:
    """Verify the upstream anchor fields exist and are non-empty."""
    missing = [f for f in REQUIRED_INPUT_FIELDS if not company_data.get(f)]
    if missing:
        print(f"[{target_company}] FAIL: missing required upstream fields: {missing}")
        return False
    return True


def extract_ticker(company_name: str) -> str:
    """Extract ticker from a company name formatted as 'Company Name (TICKER)'."""
    match = re.search(r'\(([A-Z][A-Z0-9.\-]*)\)', company_name)
    if not match:
        raise ValueError(f"Could not extract ticker from company name: {company_name}")
    return match.group(1)


def needs_run(company_data: dict, anchor_date: str) -> bool:
    """
    Decide whether to run 3a for a company.
    Run if:
        - valuation_metrics is empty/missing, OR
        - valuation_metrics_date is older than the anchor (Stage 2's synthesis_date)
    """
    metrics = company_data.get("valuation_metrics")
    if not metrics:
        return True
    field_date = company_data.get("valuation_metrics_date", "")
    if not field_date:
        return True
    if field_date < anchor_date:
        return True
    return False


async def process_target_company(target_company: str, today_str: str, output_dir: str):
    safe_company_name = (
        target_company.replace(" ", "_")
        .replace("(", "")
        .replace(")", "")
        .replace(".", "")
        .replace("/", "-")
    )
    file_name = os.path.join(output_dir, f"{safe_company_name}_research.json")

    if not os.path.exists(file_name):
        print(f"[{target_company}] Skipping: no research file at {file_name}.")
        return

    with open(file_name, "r", encoding="utf-8") as f:
        company_data = json.load(f)

    company_data = ensure_valuation_fields(company_data)

    if not required_inputs_valid(company_data, target_company):
        return

    synthesis_date = company_data["synthesis_date"]

    # Freshness check
    if not needs_run(company_data, synthesis_date):
        print(f"[{target_company}] Skipping valuation metrics: already current.")
        return

    # Extract ticker
    try:
        ticker_symbol = extract_ticker(target_company)
    except ValueError as e:
        print(f"[{target_company}] FAIL: {e}")
        return

    print(f"[{target_company}] Generating valuation metrics (ticker={ticker_symbol})")
    try:
        result = await run_valuation_metrics(
            company_name=target_company,
            ticker_symbol=ticker_symbol,
        )
    except Exception as e:
        print(f"[{target_company}] valuation metrics failed: {e}")
        return

    company_data["valuation_metrics"] = result
    company_data["valuation_metrics_date"] = today_str

    with open(file_name, "w", encoding="utf-8") as f:
        json.dump(company_data, f, indent=4, ensure_ascii=False)

    flag_count = len(result.get("data_quality_flags", []))
    peer_count = len(result.get("peer_comparison", {}).get("peer_tickers", []))
    print(
        f"[{target_company}] valuation metrics saved: "
        f"{peer_count} peers, {flag_count} data quality flag(s)."
    )

    await commit_company_progress(file_name, "valuation metrics", target_company)


async def main():
    today_str = datetime.date.today().isoformat()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    stage3_root = os.path.join(script_dir, "..")
    output_dir = os.path.join(stage3_root, "output")

    if not os.path.isdir(output_dir):
        print(f"Error: Stage 3 output directory not found at {output_dir}.")
        return

    json_files = sorted(
        f for f in os.listdir(output_dir)
        if f.endswith("_research.json")
    )

    if not json_files:
        print(f"Error: no *_research.json files found in {output_dir}.")
        return

    target_companies = []
    for fname in json_files:
        path = os.path.join(output_dir, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            cname = data.get("company_name")
            if cname:
                target_companies.append(cname)
            else:
                print(f"Warning: {fname} has no company_name field; skipping.")
        except Exception as e:
            print(f"Warning: could not read {fname}: {e}")

    if not target_companies:
        print("Error: no valid companies to process.")
        return

    sem = asyncio.Semaphore(COMPANY_CONCURRENCY)

    async def bounded(company):
        async with sem:
            await process_target_company(company, today_str, output_dir)

    print(f"Processing {len(target_companies)} companies, up to {COMPANY_CONCURRENCY} at a time.")
    await asyncio.gather(
        *(bounded(c) for c in target_companies),
        return_exceptions=True,
    )
    print("\nAll companies processed.")


if __name__ == "__main__":
    asyncio.run(main())