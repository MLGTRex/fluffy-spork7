import asyncio
import json
import datetime
import os
import sys
from debateCases import run_debate_case

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pipeline tools"))
from pipeline_git import commit_company_progress
from section_runner import run_section_until_complete

COMPANY_CONCURRENCY = 5
SECTION_KEY = "debate_cases"


def _safe_filename(target_company: str) -> str:
    safe = target_company.replace(" ", "_").replace("(", "").replace(")", "").replace(".", "").replace("/", "-")
    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output")
    return os.path.join(output_dir, f"{safe}_research.json")


def _is_section_complete(target_company: str) -> bool:
    """Predicate for section_runner: bull_case and bear_case both populated."""
    file_name = _safe_filename(target_company)
    if not os.path.exists(file_name):
        return False
    try:
        with open(file_name, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False
    return bool(data.get("bull_case")) and bool(data.get("bear_case"))


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


def research_complete(company_data: dict) -> bool:
    """Check that all three research reports exist and are non-empty."""
    return all([
        company_data.get("finance_research_report"),
        company_data.get("news_research_report"),
        company_data.get("environment_research_report"),
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


async def run_one_case(case_type, research_dump, target_company, today_str, company_data, file_name, lock):
    case_key = f"{case_type.lower()}_case"
    date_key = f"{case_key}_date"

    print(f"[{target_company}] Generating {case_type.lower()} case")
    try:
        content = await run_debate_case(case_type, research_dump, target_company)
    except Exception as e:
        print(f"[{target_company}] {case_type.lower()} case failed: {e}")
        return

    async with lock:
        company_data[case_key] = content
        company_data[date_key] = today_str
        with open(file_name, "w", encoding="utf-8") as f:
            json.dump(company_data, f, indent=4, ensure_ascii=False)
    print(f"[{target_company}] {case_type.lower()} case saved")


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

    if not research_complete(company_data):
        print(f"[{target_company}] Skipping: research stage incomplete.")
        return

    research_dump = assemble_research_dump(company_data)
    lock = asyncio.Lock()

    tasks = []
    for case_type in ("BULL", "BEAR"):
        case_key = f"{case_type.lower()}_case"
        if needs_run(company_data, case_key, today_str):
            tasks.append(run_one_case(case_type, research_dump, target_company, today_str, company_data, file_name, lock))
        else:
            print(f"[{target_company}] Skipping {case_type.lower()} case: already completed today.")

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    await commit_company_progress(file_name, "debate cases", target_company)


async def main():
    today_str = datetime.date.today().isoformat()
    input_file = os.path.normpath(
       os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "Stage 1 DRAFT", "output", "target_company_list.json")
   )

    if not os.path.exists(input_file):
        print(f"Error: Could not find {input_file}.")
        sys.exit(1)

    with open(input_file, "r", encoding="utf-8") as f:
        target_companies = json.load(f)

    async def process_one(company):
        await process_target_company(company, today_str)

    result = await run_section_until_complete(
        target_companies,
        process_one,
        _is_section_complete,
        section_key=SECTION_KEY,
        concurrency=COMPANY_CONCURRENCY,
    )

    if not result.is_complete:
        print(
            f"\n[{SECTION_KEY}] HALT: {len(result.incomplete_companies)} companies still "
            f"incomplete after {result.attempts_used} attempt(s): {result.incomplete_companies}"
        )
        sys.exit(1)
    print(f"\n[{SECTION_KEY}] All {len(target_companies)} companies complete in {result.attempts_used} attempt(s).")


if __name__ == "__main__":
    asyncio.run(main())
