import asyncio
import json
import datetime
import os
import sys
from researchDigest import run_research_digest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pipeline tools"))
from pipeline_git import commit_company_progress

COMPANY_CONCURRENCY = 5

REQUIRED_REPORT_FIELDS = [
    "finance_research_report",
    "news_research_report",
    "environment_research_report",
]


def ensure_digest_fields(company_data: dict) -> dict:
    """Add digest-stage fields to JSON if they don't exist. Idempotent."""
    digest_fields = {
        "research_digest": "",
        "research_digest_date": "",
    }
    for key, default in digest_fields.items():
        if key not in company_data:
            company_data[key] = default
    return company_data


def reports_complete(company_data: dict) -> bool:
    """Check that all three deep-research reports exist and are non-empty."""
    return all(company_data.get(f) for f in REQUIRED_REPORT_FIELDS)


def needs_run(company_data: dict, stage_key: str, today_str: str) -> bool:
    """Re-run if content is empty or date is not today."""
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

    company_data = ensure_digest_fields(company_data)

    if not reports_complete(company_data):
        missing = [f for f in REQUIRED_REPORT_FIELDS if not company_data.get(f)]
        print(f"[{target_company}] Skipping: missing deep-research report(s): {missing}.")
        return

    if not needs_run(company_data, "research_digest", today_str):
        print(f"[{target_company}] Skipping research digest: already completed today.")
        return

    print(f"[{target_company}] Generating research digest")
    try:
        content = await run_research_digest(
            finance_report=company_data["finance_research_report"],
            news_report=company_data["news_research_report"],
            environment_report=company_data["environment_research_report"],
            company_name=target_company,
        )
    except Exception as e:
        print(f"[{target_company}] research digest failed: {e}")
        return

    if not content or not content.strip():
        print(f"[{target_company}] research digest returned empty content; not saving.")
        return

    company_data["research_digest"] = content
    company_data["research_digest_date"] = today_str

    with open(file_name, "w", encoding="utf-8") as f:
        json.dump(company_data, f, indent=4, ensure_ascii=False)

    print(f"[{target_company}] research digest saved.")

    await commit_company_progress(file_name, "research digest", target_company)


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
