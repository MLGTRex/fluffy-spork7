import asyncio
import json
import datetime
import os
import sys
from debateRebuttals import run_debate_rebuttal

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


def assemble_research_dump(company_data: dict) -> str:
    """Concatenate the three research reports into a single labeled block."""
    return f"""# FINANCIAL RESEARCH

{company_data['finance_research_report']}

---

# NEWS & NARRATIVE RESEARCH

{company_data['news_research_report']}

---

# COMPETITIVE & MACRO RESEARCH

{company_data['environment_research_report']}
"""


def cases_complete(company_data: dict) -> bool:
    """Check that both bull and bear cases exist and are non-empty."""
    return all([
        company_data.get("bull_case"),
        company_data.get("bear_case"),
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


async def run_one_rebuttal(rebuttal_type, own_case, opposing_case, research_dump, target_company, today_str, company_data, file_name, lock):
    rebuttal_key = f"{rebuttal_type.lower()}_rebuttal"
    date_key = f"{rebuttal_key}_date"

    print(f"[{target_company}] Generating {rebuttal_type.lower()} rebuttal")
    try:
        content = await run_debate_rebuttal(
            rebuttal_type=rebuttal_type,
            own_case=own_case,
            opposing_case=opposing_case,
            research_dump=research_dump,
            company_name=target_company,
        )
    except Exception as e:
        print(f"[{target_company}] {rebuttal_type.lower()} rebuttal failed: {e}")
        return

    async with lock:
        company_data[rebuttal_key] = content
        company_data[date_key] = today_str
        with open(file_name, "w", encoding="utf-8") as f:
            json.dump(company_data, f, indent=4, ensure_ascii=False)
    print(f"[{target_company}] {rebuttal_type.lower()} rebuttal saved")


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

    if not cases_complete(company_data):
        print(f"[{target_company}] Skipping: bull/bear case missing.")
        return

    research_dump = assemble_research_dump(company_data)
    bull_case = company_data["bull_case"]
    bear_case = company_data["bear_case"]
    lock = asyncio.Lock()

    tasks = []
    for rebuttal_type in ("BULL", "BEAR"):
        rebuttal_key = f"{rebuttal_type.lower()}_rebuttal"
        if not needs_run(company_data, rebuttal_key, today_str):
            print(f"[{target_company}] Skipping {rebuttal_type.lower()} rebuttal: already completed today.")
            continue
        own_case = bull_case if rebuttal_type == "BULL" else bear_case
        opposing_case = bear_case if rebuttal_type == "BULL" else bull_case
        tasks.append(run_one_rebuttal(rebuttal_type, own_case, opposing_case, research_dump, target_company, today_str, company_data, file_name, lock))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    await commit_company_progress(file_name, "debate rebuttals", target_company)


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
