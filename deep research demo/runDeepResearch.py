import asyncio
import json
import os
import sys
from datetime import date

from deepResearch import run_deep_research

REPORT_TYPES = ["FINANCE", "NEWS", "ENVIRONMENT"]


def safe_filename(company: str) -> str:
    return (
        company.replace(" ", "_")
        .replace("(", "")
        .replace(")", "")
        .replace(".", "")
        .replace("/", "-")
    )


async def main():
    if len(sys.argv) < 2:
        print('Usage: python runDeepResearch.py "<company name>" [FINANCE|NEWS|ENVIRONMENT|ALL]')
        sys.exit(1)

    company = sys.argv[1]
    which = sys.argv[2].upper() if len(sys.argv) >= 3 else "ALL"

    if which == "ALL":
        types = REPORT_TYPES
    elif which in REPORT_TYPES:
        types = [which]
    else:
        print(f"Unknown report type {which!r}. Use one of: FINANCE, NEWS, ENVIRONMENT, ALL")
        sys.exit(1)

    today = date.today().isoformat()
    fname = f"{safe_filename(company)}_research_demo.json"
    if os.path.exists(fname):
        with open(fname, "r", encoding="utf-8") as f:
            output = json.load(f)
    else:
        output = {"company_name": company}
    for rt in REPORT_TYPES:
        output.setdefault(f"{rt.lower()}_research_report_date", "")
        output.setdefault(f"{rt.lower()}_research_report", "")

    for rt in types:
        print(f"\n=== {rt} ===")
        result = await run_deep_research(
            question=f"{company} as of {today}",
            system_prompt_type=rt,
        )
        output[f"{rt.lower()}_research_report_date"] = today
        output[f"{rt.lower()}_research_report"] = result

        preview = result[:500] + ("..." if len(result) > 500 else "")
        print(f"\n--- {rt} report ({len(result)} chars) ---\n{preview}")

    with open(fname, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=4, ensure_ascii=False)
    print(f"\nSaved to {fname}")


if __name__ == "__main__":
    asyncio.run(main())
