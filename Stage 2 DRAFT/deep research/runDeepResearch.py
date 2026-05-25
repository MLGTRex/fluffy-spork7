import asyncio
import json
import datetime
import os
import re
import sys
from deepResearch import run_deep_research

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pipeline tools"))
from pipeline_git import commit_company_progress

REPORT_TYPES = ["FINANCE", "NEWS", "ENVIRONMENT"]
COMPANY_CONCURRENCY = 5

# Inline citations in the deep-research output look like
#   [Source: <publisher> / Title: <title> / Date: <YYYY-MM-DD>]
# They exist to keep the model honest to itself during fact-finding. Strip
# them before saving so every downstream Stage 2 consumer (debate cases,
# debate rebuttals, research digest) reads clean text.
_CITATION_RE = re.compile(r'\s*\[Source:[^\]]*\]')


def _strip_citations(report: str) -> str:
   return _CITATION_RE.sub('', report)


def safe_filename(target_company: str) -> str:
   safe = target_company.replace(" ", "_").replace("(", "").replace(")", "").replace(".", "").replace("/", "-")
   script_dir = os.path.dirname(os.path.abspath(__file__))
   output_dir = os.path.join(script_dir, "..", "output")
   os.makedirs(output_dir, exist_ok=True)
   return os.path.join(output_dir, f"{safe}_research.json")


def load_or_init_company(target_company: str, file_name: str) -> dict:
   if os.path.exists(file_name):
      with open(file_name, "r", encoding="utf-8") as f:
         data = json.load(f)
      print(f"Loaded existing research for {target_company} from {file_name}")
   else:
      print(f"No existing file for {target_company}. Creating new profile.")
      data = {
         "company_name": target_company,
         "finance_research_report_date": "",
         "finance_research_report": "",
         "news_research_report_date": "",
         "news_research_report": "",
         "environment_research_report_date": "",
         "environment_research_report": "",
      }
   return data


async def run_one_report(target_company, report_type, today_str, company_data, file_name, lock):
   date_key = f"{report_type.lower()}_research_report_date"
   report_key = f"{report_type.lower()}_research_report"

   print(f"[{target_company}] Starting {report_type} research")
   try:
      result = await run_deep_research(
         question=f"{target_company} as of {today_str}",
         system_prompt_type=report_type,
         formulas=["moonshot/web-search:latest", "moonshot/rethink:latest"],
      )
   except Exception as e:
      print(f"[{target_company}] {report_type} failed: {e}")
      return

   result = _strip_citations(result)

   print(f"[{target_company}] {report_type} complete (snippet: {result[:80]}...)")

   async with lock:
      company_data[date_key] = today_str
      company_data[report_key] = result
      with open(file_name, "w", encoding="utf-8") as f:
         json.dump(company_data, f, indent=4, ensure_ascii=False)
   print(f"[{target_company}] {report_type} saved to {file_name}")


async def process_target_company(target_company: str, today_str: str):
   file_name = safe_filename(target_company)
   company_data = load_or_init_company(target_company, file_name)
   lock = asyncio.Lock()

   tasks = []
   for report_type in REPORT_TYPES:
      date_key = f"{report_type.lower()}_research_report_date"
      report_key = f"{report_type.lower()}_research_report"
      if company_data.get(date_key) == today_str and company_data.get(report_key):
         print(f"[{target_company}] Skipping {report_type}: already completed today.")
         continue
      tasks.append(run_one_report(target_company, report_type, today_str, company_data, file_name, lock))

   if tasks:
      await asyncio.gather(*tasks, return_exceptions=True)

   await commit_company_progress(file_name, "deep research", target_company)


async def main():
   today_str = datetime.date.today().isoformat()
   script_dir = os.path.dirname(os.path.abspath(__file__))
   input_file = os.path.normpath(
       os.path.join(script_dir, "..", "..", "Stage 1 DRAFT", "output", "target_company_list.json")
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
