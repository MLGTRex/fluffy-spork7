import argparse
import asyncio
import json
import os
from datetime import date

REPORT_TYPES = ["FINANCE", "NEWS", "ENVIRONMENT"]
BACKENDS = ["openrouter", "moonshot"]


def safe_filename(company: str) -> str:
    return (
        company.replace(" ", "_")
        .replace("(", "")
        .replace(")", "")
        .replace(".", "")
        .replace("/", "-")
    )


def _load_backend(backend: str):
    if backend == "openrouter":
        from deepResearch_openrouter import run_deep_research
    elif backend == "moonshot":
        from deepResearch_moonshot import run_deep_research
    else:
        raise ValueError(f"Unknown backend: {backend!r}")
    return run_deep_research


async def main():
    parser = argparse.ArgumentParser(
        description="Run the Deep Research demo against one company using the chosen backend.",
    )
    parser.add_argument("--backend", required=True, choices=BACKENDS,
                        help="Which Deep Research implementation to use.")
    parser.add_argument("company", help='Company name, e.g. "Apple Inc (AAPL)".')
    parser.add_argument("report_type", nargs="?", default="ALL",
                        help="FINANCE | NEWS | ENVIRONMENT | ALL (default ALL).")
    args = parser.parse_args()

    which = args.report_type.upper()
    if which == "ALL":
        types = REPORT_TYPES
    elif which in REPORT_TYPES:
        types = [which]
    else:
        print(f"Unknown report type {which!r}. Use one of: FINANCE, NEWS, ENVIRONMENT, ALL")
        raise SystemExit(1)

    run_deep_research = _load_backend(args.backend)

    today = date.today().isoformat()
    fname = f"{safe_filename(args.company)}_research_demo_{args.backend}.json"
    if os.path.exists(fname):
        with open(fname, "r", encoding="utf-8") as f:
            output = json.load(f)
    else:
        output = {"company_name": args.company, "backend": args.backend}
    output["backend"] = args.backend
    for rt in REPORT_TYPES:
        output.setdefault(f"{rt.lower()}_research_report_date", "")
        output.setdefault(f"{rt.lower()}_research_report", "")

    for rt in types:
        print(f"\n=== [{args.backend}] {rt} ===")
        result = await run_deep_research(
            question=f"{args.company} as of {today}",
            system_prompt_type=rt,
        )
        output[f"{rt.lower()}_research_report_date"] = today
        output[f"{rt.lower()}_research_report"] = result

        preview = result[:500] + ("..." if len(result) > 500 else "")
        print(f"\n--- [{args.backend}] {rt} report ({len(result)} chars) ---\n{preview}")

    with open(fname, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=4, ensure_ascii=False)
    print(f"\nSaved to {fname}")


if __name__ == "__main__":
    asyncio.run(main())
