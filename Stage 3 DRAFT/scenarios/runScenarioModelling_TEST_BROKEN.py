import asyncio
import json
import datetime
import os
import sys
import shutil
from scenarioModelling import run_scenario_agent

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from pipeline_git import commit_company_progress

COMPANY_CONCURRENCY = 5

# Required Stage 2 fields that must be present and non-empty before Stage 3b runs
REQUIRED_STAGE2_FIELDS = [
    "bull_case",
    "bear_case",
    "bull_rebuttal",
    "bear_rebuttal",
    "synthesis",
    "finance_research_report",
    "news_research_report",
    "environment_research_report",
]

# New Stage 3b fields written to each company JSON
SCENARIO_FIELDS = {
    "scenario_bull_date": "",
    "scenario_bull": "",
    "scenario_bear_date": "",
    "scenario_bear": "",
    "scenario_base_initial_date": "",
    "scenario_base_initial": "",
    "scenario_bull_rebuttal_date": "",
    "scenario_bull_rebuttal": "",
    "scenario_bear_rebuttal_date": "",
    "scenario_bear_rebuttal": "",
    "scenario_base_final_date": "",
    "scenario_base_final": "",
}

def ensure_scenario_fields(company_data: dict) -> dict:
    """Add Stage 3b fields to JSON if they don't exist. Idempotent."""
    for key, default in SCENARIO_FIELDS.items():
        if key not in company_data:
            company_data[key] = default
    return company_data

def assemble_research_dump(company_data: dict) -> str:
    """Concatenate Stage 2 research reports + debate outputs into a single labeled block."""
    return f"""# FINANCIAL RESEARCH
{company_data['finance_research_report']}
---
# NEWS & NARRATIVE RESEARCH
{company_data['news_research_report']}
---
# COMPETITIVE & MACRO RESEARCH
{company_data['environment_research_report']}
---
# BULL CASE
{company_data['bull_case']}
---
# BEAR CASE
{company_data['bear_case']}
---
# BULL REBUTTAL
{company_data['bull_rebuttal']}
---
# BEAR REBUTTAL
{company_data['bear_rebuttal']}
---
# SYNTHESIS
{company_data['synthesis']}
"""

def stage2_inputs_valid(company_data: dict, target_company: str) -> bool:
    """Verify all required Stage 2 fields exist and are non-empty. Loud-fail on missing."""
    missing = [f for f in REQUIRED_STAGE2_FIELDS if not company_data.get(f)]
    if missing:
        print(f"[{target_company}] FAIL: missing required Stage 2 fields: {missing}")
        return False
    return True

def sync_stage2_json(target_company: str, stage2_path: str, stage3_path: str) -> bool:
    """
    Sync Stage 2 JSON to Stage 3 /output.
    Uses dictionary merging instead of file mtime to avoid GitHub Actions wiping data.
    Returns True if Stage 3 file is ready to use, False otherwise.
    """
    if not os.path.exists(stage2_path):
        print(f"[{target_company}] Skipping: Stage 2 file not found at {stage2_path}.")
        return False

    if not os.path.exists(stage3_path):
        # File doesn't exist at all yet, safe to do a full copy
        shutil.copy2(stage2_path, stage3_path)
        print(f"[{target_company}] Copied Stage 2 JSON to Stage 3 /output (new file).")
        return True

    # If Stage 3 exists, safely merge Stage 2 data into it so scenarios aren't overwritten
    with open(stage2_path, "r", encoding="utf-8") as f:
        stage2_data = json.load(f)

    with open(stage3_path, "r", encoding="utf-8") as f:
        stage3_data = json.load(f)

    # Overwrite the Stage 3 dict with Stage 2's keys (updates research but ignores scenarios)
    for key, value in stage2_data.items():
        stage3_data[key] = value

    with open(stage3_path, "w", encoding="utf-8") as f:
        json.dump(stage3_data, f, indent=4, ensure_ascii=False)

    return True

async def run_and_save(
    agent_key: str,
    field_name: str,
    target_company: str,
    today_str: str,
    company_data: dict,
    file_name: str,
    lock: asyncio.Lock,
    **agent_inputs,
) -> str:
    """
    Run one scenario agent, then save its output + date to the JSON.
    Returns the generated content (or empty string on failure).
    """
    existing_content = company_data.get(field_name, "")
    existing_date_str = company_data.get(f"{field_name}_date", "")
    synthesis_date_str = company_data.get("synthesis_date", "")

    should_run = False
    
    # Check 1: Is the content missing or empty?
    if not isinstance(existing_content, str) or not existing_content.strip():
        should_run = True
        print(f"[{target_company}] Generating {agent_key} scenario (currently empty/missing).")
    
    # Check 2: Is the content outdated relative to the Stage 2 synthesis date?
    elif synthesis_date_str and existing_date_str and existing_date_str < synthesis_date_str:
        should_run = True
        print(f"[{target_company}] Regenerating {agent_key} scenario (outdated: {existing_date_str} < {synthesis_date_str}).")

    # If neither condition is met, skip the generation and return the existing content
    if not should_run:
        print(f"[{target_company}] {agent_key} scenario already exists and is up to date. Skipping generation.")
        return existing_content

    try:
        content = await run_scenario_agent(
            agent_key=agent_key,
            company_name=target_company,
            **agent_inputs,
        )
    except Exception as e:
        print(f"[{target_company}] {agent_key} scenario failed: {e}")
        return ""
        
    async with lock:
        company_data[field_name] = content
        company_data[f"{field_name}_date"] = today_str
        with open(file_name, "w", encoding="utf-8") as f:
            json.dump(company_data, f, indent=4, ensure_ascii=False)
            
    print(f"[{target_company}] {agent_key} scenario saved")
    return content

async def process_target_company(target_company: str, today_str: str, stage2_dir: str, stage3_output_dir: str):
    safe_company_name = (
        target_company.replace(" ", "_")
        .replace("(", "")
        .replace(")", "")
        .replace(".", "")
        .replace("/", "-")
    )
    file_name_only = f"{safe_company_name}_research.json"
    stage2_path = os.path.join(stage2_dir, file_name_only)
    stage3_path = os.path.join(stage3_output_dir, file_name_only)
    
    # 1. Sync Stage 2 → Stage 3 if needed
    if not sync_stage2_json(target_company, stage2_path, stage3_path):
        return
        
    # 2. Load Stage 3 JSON
    with open(stage3_path, "r", encoding="utf-8") as f:
        company_data = json.load(f)
        
    company_data = ensure_scenario_fields(company_data)
    
    # 3. Validate Stage 2 inputs
    if not stage2_inputs_valid(company_data, target_company):
        return
        
    research_dump = assemble_research_dump(company_data)
    lock = asyncio.Lock()
    
    # 4. Phase 1: bull, bear, base_initial in parallel (all blind to each other)
    print(f"[{target_company}] Phase 1: running bull, bear, base initial scenarios in parallel.")
    phase1_results = await asyncio.gather(
        run_and_save("bull", "scenario_bull", target_company, today_str, company_data, stage3_path, lock,
                     research_dump=research_dump),
        run_and_save("bear", "scenario_bear", target_company, today_str, company_data, stage3_path, lock,
                     research_dump=research_dump),
        run_and_save("base_initial", "scenario_base_initial", target_company, today_str, company_data, stage3_path, lock,
                     research_dump=research_dump),
        return_exceptions=True,
    )
    bull_initial, bear_initial, base_initial = phase1_results
    
    # If any phase 1 result is an exception or empty, abort downstream agents for this company
    if not all(isinstance(r, str) and r for r in phase1_results):
        print(f"[{target_company}] Aborting: phase 1 incomplete (one or more initial scenarios failed).")
        return
        
    # 5. Phase 2: bull_rebuttal, bear_rebuttal in parallel
    print(f"[{target_company}] Phase 2: running bull and bear rebuttals in parallel.")
    phase2_results = await asyncio.gather(
        run_and_save("bull_rebuttal", "scenario_bull_rebuttal", target_company, today_str, company_data, stage3_path, lock,
                     research_dump=research_dump,
                     bull_initial=bull_initial,
                     bear_initial=bear_initial),
        run_and_save("bear_rebuttal", "scenario_bear_rebuttal", target_company, today_str, company_data, stage3_path, lock,
                     research_dump=research_dump,
                     bull_initial=bull_initial,
                     bear_initial=bear_initial),
        return_exceptions=True,
    )
    bull_rebuttal, bear_rebuttal = phase2_results
    
    if not all(isinstance(r, str) and r for r in phase2_results):
        print(f"[{target_company}] Aborting: phase 2 incomplete (one or more rebuttals failed).")
        return
        
    # 6. Phase 3: base_arbitration (sees everything)
    print(f"[{target_company}] Phase 3: running base arbitration.")
    await run_and_save(
        "base_arbitration", "scenario_base_final", target_company, today_str, company_data, stage3_path, lock,
        research_dump=research_dump,
        bull_initial=bull_initial,
        bear_initial=bear_initial,
        base_initial=base_initial,
        bull_rebuttal=bull_rebuttal,
        bear_rebuttal=bear_rebuttal,
    )
    print(f"[{target_company}] All scenario agents complete.")
    await commit_company_progress(stage3_path, "scenarios", target_company)

async def main():
    today_str = datetime.date.today().isoformat()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # Stage 3 root is one level up from /scenario modelling
    stage3_root = os.path.join(script_dir, "..")
    stage3_output_dir = os.path.join(stage3_root, "output")
    # Stage 2 source is two levels up, then into "Stage 2 DRAFT/output"
    stage2_dir = os.path.join(stage3_root, "..", "Stage 2 DRAFT", "output")
    # Ensure Stage 3 /output exists
    os.makedirs(stage3_output_dir, exist_ok=True)
    if not os.path.isdir(stage2_dir):
        print(f"Error: Stage 2 output directory not found at {stage2_dir}.")
        return
    # Source of truth: whatever JSON files exist in Stage 2's /output
    stage2_files = sorted(
        f for f in os.listdir(stage2_dir)
        if f.endswith("_research.json")
    )
    if not stage2_files:
        print(f"Error: no *_research.json files found in {stage2_dir}.")
        return
    # Reconstruct company display names from filenames
    target_companies = [
        f[:-len("_research.json")].replace("_", " ")
        for f in stage2_files
    ]
    sem = asyncio.Semaphore(COMPANY_CONCURRENCY)
    async def bounded(company):
        async with sem:
            await process_target_company(company, today_str, stage2_dir, stage3_output_dir)
    print(f"Processing {len(target_companies)} companies, up to {COMPANY_CONCURRENCY} at a time.")
    await asyncio.gather(
        *(bounded(c) for c in target_companies),
        return_exceptions=True,
    )
    print("\nAll companies processed.")

if __name__ == "__main__":
    asyncio.run(main())
