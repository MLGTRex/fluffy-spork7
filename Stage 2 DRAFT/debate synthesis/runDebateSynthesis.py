import asyncio
import json
import datetime
import os
import sys
from debateSynthesis import run_synthesis

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pipeline tools"))
from pipeline_git import commit_company_progress

COMPANY_CONCURRENCY = 5


def ensure_debate_fields(company_data: dict) -> dict:
    """Add debate-stage fields to JSON if they don't exist. Idempotent."""
    debate_fields = {
        "bull_case_date": "",
        "bull_case": "",
        "bear_case_date": "",
        "bear_case": "",
        "bull_rebuttal_date": "",
        "bull_rebuttal": "",
        "bear_rebuttal_date": "",
        "bear_rebuttal": "",
        "synthesis_date": "",
        "synthesis": "",
        "synthesis_score": None,
        "synthesis_categorical": "",
        "synthesis_score_confidence": "",
    }
    for key, default in debate_fields.items():
        if key not in company_data:
            company_data[key] = default
    return company_data


def rebuttals_complete(company_data: dict) -> bool:
    """Check that both rebuttals exist and are non-empty."""
    return all([
        company_data.get("bull_rebuttal"),
        company_data.get("bear_rebuttal"),
    ])


def needs_run(company_data: dict, stage_key: str, today_str: str) -> bool:
    """Check if a stage needs to run (empty content or stale date)."""
    content = company_data.get(stage_key)
    date = company_data.get(f"{stage_key}_date")
    if not content:
        return True
    if date != today_str:
        return True
    return False


async def process_target_company(target_company: str, today_str: str):
    safe_company_name = target_company.replace(" ", "_").replace("(", "").replace(")", "").replace(".", "").replace("/", "-")
    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output")
    file_name = os.path.join(output_dir, f"{safe_company_name}_research.json")

    if not os.path.exists(file_name):
        print(f"[{target_company}] Skipping: no research file at {file_name}.")
        return

    with open(file_name, "r", encoding="utf-8") as f:
        company_data = json.load(f)

    company_data = ensure_debate_fields(company_data)

    if not rebuttals_complete(company_data):
        print(f"[{target_company}] Skipping: bull/bear rebuttal missing.")
        return

    if not needs_run(company_data, "synthesis", today_str):
        print(f"[{target_company}] Skipping synthesis: already completed today.")
        return

    bull_case = company_data["bull_case"]
    bear_case = company_data["bear_case"]
    bull_rebuttal = company_data["bull_rebuttal"]
    bear_rebuttal = company_data["bear_rebuttal"]

    print(f"[{target_company}] Generating synthesis")
    try:
        result = await run_synthesis(
            bull_case=bull_case,
            bear_case=bear_case,
            bull_rebuttal=bull_rebuttal,
            bear_rebuttal=bear_rebuttal,
            company_name=target_company,
        )
    except Exception as e:
        print(f"[{target_company}] synthesis failed: {e}")
        return

    company_data["synthesis"] = result["content"]
    company_data["synthesis_date"] = today_str
    company_data["synthesis_score"] = result["score"]
    company_data["synthesis_categorical"] = result["categorical"]
    company_data["synthesis_score_confidence"] = result["score_confidence"]

    with open(file_name, "w", encoding="utf-8") as f:
        json.dump(company_data, f, indent=4, ensure_ascii=False)

    if result["score"] is None:
        print(f"[{target_company}] synthesis saved (WARNING: structured field parsing failed).")
    else:
        print(f"[{target_company}] synthesis saved: score={result['score']}, {result['categorical']}, confidence={result['score_confidence']}.")

    await commit_company_progress(file_name, "debate synthesis", target_company)


async def main():
    today_str = datetime.date.today().isoformat()
    input_file = os.path.normpath(
       os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "Stage 1 DRAFT", "output", "target_company_list.json")
   )

    if not os.path.exists(input_file):
        print(f"Error: Could not find {input_file}.")
        return

    with open(input_file, "r", encoding="utf-8") as f:
        target_companies = json.load(f)

    sem = asyncio.Semaphore(COMPANY_CONCURRENCY)

    async def bounded(company):
        async with sem:
            await process_target_company(company, today_str)

    print(f"Processing {len(target_companies)} companies, up to {COMPANY_CONCURRENCY} at a time.")
    await asyncio.gather(
        *(bounded(c) for c in target_companies),
        return_exceptions=True,
    )
    print("\nAll companies processed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
