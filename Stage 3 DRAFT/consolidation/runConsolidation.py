import asyncio
import json
import datetime
import os
from consolidation import run_consolidation

COMPANY_CONCURRENCY = 5

# Required input fields that must be present and non-empty before Stage 3c runs
REQUIRED_INPUT_FIELDS = [
    "scenario_bull",
    "scenario_bear",
    "scenario_base_final",
    "synthesis",
    "valuation_metrics",
]

# Upstream date fields whose values must all be older than consolidation_date for 3c to be considered current
UPSTREAM_DATE_FIELDS = [
    "synthesis_date",
    "scenario_bull_date",
    "scenario_bear_date",
    "scenario_base_final_date",
    "valuation_metrics_date",
]

# New Stage 3c fields written to each company JSON
CONSOLIDATION_FIELDS = {
    "consolidation_date": "",
    "consolidation": "",
    "ticker": "",
    "current_price": None,
    "current_price_date": "",
    "price_target_bull_1m": None,
    "price_target_bull_3m": None,
    "price_target_bull_6m": None,
    "price_target_bull_12m": None,
    "price_target_base_1m": None,
    "price_target_base_3m": None,
    "price_target_base_6m": None,
    "price_target_base_12m": None,
    "price_target_bear_1m": None,
    "price_target_bear_3m": None,
    "price_target_bear_6m": None,
    "price_target_bear_12m": None,
    "scenario_probability_bull": None,
    "scenario_probability_base": None,
    "scenario_probability_bear": None,
    "conviction": "",
    "thesis_summary": "",
    "key_invalidation_triggers": [],
    "expected_return_1m": None,
    "expected_return_3m": None,
    "expected_return_6m": None,
    "expected_return_12m": None,
    "upside_return_12m": None,
    "base_return_12m": None,
    "downside_return_12m": None,
}


def ensure_consolidation_fields(company_data: dict) -> dict:
    """Add Stage 3c fields to JSON if they don't exist. Idempotent."""
    for key, default in CONSOLIDATION_FIELDS.items():
        if key not in company_data:
            company_data[key] = default
    return company_data


def required_inputs_valid(company_data: dict, target_company: str) -> bool:
    """Verify all required input fields exist and are non-empty. Loud-fail on missing."""
    missing = [f for f in REQUIRED_INPUT_FIELDS if not company_data.get(f)]
    if missing:
        print(f"[{target_company}] FAIL: missing required input fields: {missing}")
        return False
    return True


def needs_run(company_data: dict) -> bool:
    """
    Decide whether to run 3c for a company.
    Run if:
        - consolidation is empty, OR
        - consolidation_date is older than ANY upstream date (synthesis, 3b, 3a)
    """
    consolidation = company_data.get("consolidation")
    if not consolidation:
        return True
    cons_date = company_data.get("consolidation_date", "")
    if not cons_date:
        return True
    for upstream_key in UPSTREAM_DATE_FIELDS:
        upstream_date = company_data.get(upstream_key, "")
        if upstream_date and upstream_date > cons_date:
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

    company_data = ensure_consolidation_fields(company_data)

    if not required_inputs_valid(company_data, target_company):
        return

    if not needs_run(company_data):
        print(f"[{target_company}] Skipping consolidation: already current.")
        return

    scenario_bull = company_data["scenario_bull"]
    scenario_bear = company_data["scenario_bear"]
    scenario_base_final = company_data["scenario_base_final"]
    synthesis = company_data["synthesis"]
    valuation_metrics = company_data["valuation_metrics"]

    print(f"[{target_company}] Generating consolidation")
    try:
        result = await run_consolidation(
            scenario_bull=scenario_bull,
            scenario_bear=scenario_bear,
            scenario_base_final=scenario_base_final,
            synthesis=synthesis,
            valuation_metrics=valuation_metrics,
            company_name=target_company,
        )
    except Exception as e:
        print(f"[{target_company}] consolidation failed: {e}")
        return

    company_data["consolidation"] = result["content"]
    company_data["consolidation_date"] = today_str
    company_data["ticker"] = result["ticker"]
    company_data["current_price"] = result["current_price"]
    company_data["current_price_date"] = today_str

    raw_fields = [
        "price_target_bull_1m", "price_target_bull_3m", "price_target_bull_6m", "price_target_bull_12m",
        "price_target_base_1m", "price_target_base_3m", "price_target_base_6m", "price_target_base_12m",
        "price_target_bear_1m", "price_target_bear_3m", "price_target_bear_6m", "price_target_bear_12m",
        "scenario_probability_bull", "scenario_probability_base", "scenario_probability_bear",
        "conviction", "thesis_summary", "key_invalidation_triggers",
    ]
    for f in raw_fields:
        company_data[f] = result.get(f)

    computed_fields = [
        "expected_return_1m", "expected_return_3m", "expected_return_6m", "expected_return_12m",
        "upside_return_12m", "base_return_12m", "downside_return_12m",
    ]
    for f in computed_fields:
        company_data[f] = result.get(f)

    with open(file_name, "w", encoding="utf-8") as f:
        json.dump(company_data, f, indent=4, ensure_ascii=False)

    if result.get("expected_return_12m") is not None:
        print(
            f"[{target_company}] consolidation saved: "
            f"price=${result['current_price']:.2f}, "
            f"E[r_12m]={result['expected_return_12m']*100:.1f}%, "
            f"conviction={result['conviction']}."
        )
    else:
        print(f"[{target_company}] consolidation saved (WARNING: structured field parsing or return computation incomplete).")


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