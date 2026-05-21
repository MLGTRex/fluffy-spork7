import asyncio
import json
import datetime
import os
import shutil
import sys
from scenarioModelling import run_scenario_agent

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pipeline tools"))
from pipeline_git import commit_company_progress

COMPANY_CONCURRENCY = 5

# Required Stage 2 fields that must be present and non-empty before any Stage 3b agents run
REQUIRED_STAGE2_FIELDS = [
    "bull_case",
    "bear_case",
    "bull_rebuttal",
    "bear_rebuttal",
    "synthesis",
    "synthesis_date",
    "finance_research_report",
    "news_research_report",
    "environment_research_report",
]

# Stage 3b field pairs (content + date) — used both for resets and per-agent freshness checks
STAGE3B_FIELD_PAIRS = [
    ("scenario_bull", "scenario_bull_date"),
    ("scenario_bear", "scenario_bear_date"),
    ("scenario_base_initial", "scenario_base_initial_date"),
    ("scenario_bull_rebuttal", "scenario_bull_rebuttal_date"),
    ("scenario_bear_rebuttal", "scenario_bear_rebuttal_date"),
    ("scenario_base_final", "scenario_base_final_date"),
]

STAGE3A_FIELDS_TO_CLEAR = ["valuation_metrics", "valuation_metrics_date"]

STAGE3C_FIELDS_TO_CLEAR = [
    "consolidation", "consolidation_date",
    "ticker", "current_price", "current_price_date",
    "price_target_bull_1m", "price_target_bull_3m", "price_target_bull_6m", "price_target_bull_12m",
    "price_target_base_1m", "price_target_base_3m", "price_target_base_6m", "price_target_base_12m",
    "price_target_bear_1m", "price_target_bear_3m", "price_target_bear_6m", "price_target_bear_12m",
    "scenario_probability_bull", "scenario_probability_base", "scenario_probability_bear",
    "conviction", "thesis_summary", "key_invalidation_triggers",
    "expected_return_1m", "expected_return_3m", "expected_return_6m", "expected_return_12m",
    "upside_return_12m", "base_return_12m", "downside_return_12m",
]

# All Stage 3 date fields used to compute "most recent Stage 3 activity"
ALL_STAGE3_DATE_FIELDS = (
    [date_key for _, date_key in STAGE3B_FIELD_PAIRS]
    + ["valuation_metrics_date", "consolidation_date"]
)

# Per-agent dependency map: which upstream date fields each agent's freshness depends on.
# An agent re-runs if its own date is older than ANY of these dates, OR if its content is empty.
AGENT_DEPENDENCY_DATES = {
    # Phase 1 — only synthesis is upstream
    "scenario_bull": ["synthesis_date"],
    "scenario_bear": ["synthesis_date"],
    "scenario_base_initial": ["synthesis_date"],
    # Phase 2 — depends on synthesis + all phase 1 outputs
    "scenario_bull_rebuttal": [
        "synthesis_date",
        "scenario_bull_date",
        "scenario_bear_date",
        "scenario_base_initial_date",
    ],
    "scenario_bear_rebuttal": [
        "synthesis_date",
        "scenario_bull_date",
        "scenario_bear_date",
        "scenario_base_initial_date",
    ],
    # Phase 3 — depends on synthesis + all phase 1 + all phase 2 outputs
    "scenario_base_final": [
        "synthesis_date",
        "scenario_bull_date",
        "scenario_bear_date",
        "scenario_base_initial_date",
        "scenario_bull_rebuttal_date",
        "scenario_bear_rebuttal_date",
    ],
}

PHASE1_SCENARIO_FIELDS = ["scenario_bull", "scenario_bear", "scenario_base_initial"]
PHASE2_REBUTTAL_FIELDS = ["scenario_bull_rebuttal", "scenario_bear_rebuttal"]


def max_stage3_date(company_data: dict) -> str:
    """Return the most recent date string across all Stage 3 date fields. Empty string if none."""
    dates = [company_data.get(k, "") for k in ALL_STAGE3_DATE_FIELDS]
    dates = [d for d in dates if d]
    if not dates:
        return ""
    return max(dates)


def stage2_inputs_valid(company_data: dict, target_company: str) -> bool:
    """Verify all required Stage 2 fields exist and are non-empty."""
    missing = [f for f in REQUIRED_STAGE2_FIELDS if not company_data.get(f)]
    if missing:
        print(f"[{target_company}] FAIL: missing required Stage 2 fields: {missing}")
        return False
    return True


def reset_stage3_fields(company_data: dict) -> dict:
    """Clear all Stage 3 fields (3b + 3a + 3c)."""
    for content_key, date_key in STAGE3B_FIELD_PAIRS:
        company_data[content_key] = ""
        company_data[date_key] = ""
    for k in STAGE3A_FIELDS_TO_CLEAR:
        company_data[k] = "" if k.endswith("_date") else None
    for k in STAGE3C_FIELDS_TO_CLEAR:
        if k.endswith("_date") or k in ("ticker", "conviction", "thesis_summary", "consolidation"):
            company_data[k] = ""
        elif k == "key_invalidation_triggers":
            company_data[k] = []
        else:
            company_data[k] = None
    return company_data


def needs_run(company_data: dict, content_key: str) -> bool:
    """
    Decide whether to run an agent based on:
        - Empty content, OR
        - Own date is older than ANY of its upstream dependency dates (cascading freshness).

    Looks up the agent's dependencies from AGENT_DEPENDENCY_DATES.
    """
    content = company_data.get(content_key)
    if not content:
        return True

    own_date = company_data.get(f"{content_key}_date", "")
    if not own_date:
        return True

    upstream_keys = AGENT_DEPENDENCY_DATES.get(content_key, [])
    for upstream_key in upstream_keys:
        upstream_date = company_data.get(upstream_key, "")
        if upstream_date and upstream_date > own_date:
            return True

    return False


def sync_stage2_json(target_company: str, stage2_path: str, stage3_path: str) -> bool:
    """
    Ensure the Stage 3 JSON exists and carries the current Stage 2 research.

    - Stage 3 file missing: copy it from Stage 2.
    - Stage 3 file exists but Stage 2 has since changed (a targeted rerun):
      merge Stage 2's fields back into the Stage 3 file, so the freshness/reset
      checks downstream see the new synthesis_date. Stage 3-generated fields
      (scenario_*, valuation_metrics, consolidation, current_price, ...) are
      left in place — reset_stage3_fields() clears them once the newer
      synthesis_date is detected.
    - Stage 3 file exists and Stage 2 is unchanged: no-op.

    Staleness within an existing file is then handled via reset_stage3_fields().
    """
    if not os.path.exists(stage2_path):
        print(f"[{target_company}] Skipping: Stage 2 file not found at {stage2_path}.")
        return False

    if not os.path.exists(stage3_path):
        shutil.copy2(stage2_path, stage3_path)
        print(f"[{target_company}] Copied Stage 2 JSON to Stage 3 /output (new file).")
        return True

    # Stage 3 file already exists — refresh the Stage 2-owned fields if Stage 2
    # has changed since they were last synced (otherwise Stage 3 would keep
    # comparing freshness against its own stale embedded dates).
    try:
        with open(stage2_path, "r", encoding="utf-8") as f:
            stage2_data = json.load(f)
        with open(stage3_path, "r", encoding="utf-8") as f:
            stage3_data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[{target_company}] Could not read JSON to sync Stage 2 -> Stage 3: {e}")
        return False

    changed = [k for k, v in stage2_data.items() if stage3_data.get(k) != v]
    if changed:
        stage3_data.update(stage2_data)
        with open(stage3_path, "w", encoding="utf-8") as f:
            json.dump(stage3_data, f, indent=4, ensure_ascii=False)
        print(
            f"[{target_company}] Stage 2 has changed — refreshed {len(changed)} "
            f"field(s) in the Stage 3 file "
            f"(synthesis_date={stage2_data.get('synthesis_date')})."
        )

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
    """Run one scenario agent, then save its output + date to the JSON."""
    print(f"[{target_company}] Generating {agent_key} scenario")
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


def assemble_research_dump(company_data: dict) -> str:
    """Concatenate Stage 2 research + debate outputs into a single labeled block for the agents."""
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


def all_present(company_data: dict, fields: list) -> bool:
    """Check that every named field is present and non-empty."""
    return all(company_data.get(f) for f in fields)


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

    if not sync_stage2_json(target_company, stage2_path, stage3_path):
        return

    with open(stage3_path, "r", encoding="utf-8") as f:
        company_data = json.load(f)

    if not stage2_inputs_valid(company_data, target_company):
        return

    synthesis_date = company_data["synthesis_date"]

    # Reset check: if Stage 2's synthesis is newer than the most recent Stage 3 activity, clear Stage 3 fields
    latest_s3 = max_stage3_date(company_data)
    if latest_s3 and synthesis_date > latest_s3:
        print(
            f"[{target_company}] Stage 2 synthesis ({synthesis_date}) is newer than most recent "
            f"Stage 3 activity ({latest_s3}); resetting all Stage 3 fields."
        )
        company_data = reset_stage3_fields(company_data)
        with open(stage3_path, "w", encoding="utf-8") as f:
            json.dump(company_data, f, indent=4, ensure_ascii=False)

    research_dump = assemble_research_dump(company_data)
    lock = asyncio.Lock()

    # ============= PHASE 1 =============
    phase1_tasks = []
    phase1_field_map = {
        "bull": "scenario_bull",
        "bear": "scenario_bear",
        "base_initial": "scenario_base_initial",
    }
    for agent_key, field_name in phase1_field_map.items():
        if needs_run(company_data, field_name):
            phase1_tasks.append(
                run_and_save(
                    agent_key, field_name, target_company, today_str,
                    company_data, stage3_path, lock,
                    research_dump=research_dump,
                )
            )
        else:
            print(f"[{target_company}] Skipping {agent_key} scenario: already current.")

    if phase1_tasks:
        print(f"[{target_company}] Phase 1: running {len(phase1_tasks)} initial scenario(s) in parallel.")
        await asyncio.gather(*phase1_tasks, return_exceptions=True)

    # Refresh in-memory state from disk before phase 2 freshness checks
    with open(stage3_path, "r", encoding="utf-8") as f:
        company_data = json.load(f)

    if not all_present(company_data, PHASE1_SCENARIO_FIELDS):
        missing = [f for f in PHASE1_SCENARIO_FIELDS if not company_data.get(f)]
        print(f"[{target_company}] Aborting: phase 1 incomplete (missing: {missing}). Skipping phase 2 and 3.")
        return

    bull_initial = company_data["scenario_bull"]
    bear_initial = company_data["scenario_bear"]
    base_initial = company_data["scenario_base_initial"]

    # ============= PHASE 2 =============
    phase2_tasks = []
    phase2_field_map = {
        "bull_rebuttal": "scenario_bull_rebuttal",
        "bear_rebuttal": "scenario_bear_rebuttal",
    }
    for agent_key, field_name in phase2_field_map.items():
        if needs_run(company_data, field_name):
            phase2_tasks.append(
                run_and_save(
                    agent_key, field_name, target_company, today_str,
                    company_data, stage3_path, lock,
                    research_dump=research_dump,
                    bull_initial=bull_initial,
                    bear_initial=bear_initial,
                )
            )
        else:
            print(f"[{target_company}] Skipping {agent_key} scenario: already current.")

    if phase2_tasks:
        print(f"[{target_company}] Phase 2: running {len(phase2_tasks)} rebuttal(s) in parallel.")
        await asyncio.gather(*phase2_tasks, return_exceptions=True)

    # Refresh in-memory state from disk before phase 3 freshness check
    with open(stage3_path, "r", encoding="utf-8") as f:
        company_data = json.load(f)

    if not all_present(company_data, PHASE2_REBUTTAL_FIELDS):
        missing = [f for f in PHASE2_REBUTTAL_FIELDS if not company_data.get(f)]
        print(f"[{target_company}] Aborting: phase 2 incomplete (missing: {missing}). Skipping phase 3.")
        return

    bull_rebuttal = company_data["scenario_bull_rebuttal"]
    bear_rebuttal = company_data["scenario_bear_rebuttal"]

    # ============= PHASE 3 =============
    if needs_run(company_data, "scenario_base_final"):
        print(f"[{target_company}] Phase 3: running base arbitration.")
        await run_and_save(
            "base_arbitration", "scenario_base_final", target_company, today_str,
            company_data, stage3_path, lock,
            research_dump=research_dump,
            bull_initial=bull_initial,
            bear_initial=bear_initial,
            base_initial=base_initial,
            bull_rebuttal=bull_rebuttal,
            bear_rebuttal=bear_rebuttal,
        )
    else:
        print(f"[{target_company}] Skipping base arbitration: already current.")

    print(f"[{target_company}] Stage 3b processing complete.")

    await commit_company_progress(stage3_path, "scenarios", target_company)


async def main():
    today_str = datetime.date.today().isoformat()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    stage3_root = os.path.join(script_dir, "..")
    stage3_output_dir = os.path.join(stage3_root, "output")
    stage2_dir = os.path.join(stage3_root, "..", "Stage 2 DRAFT", "output")

    os.makedirs(stage3_output_dir, exist_ok=True)

    if not os.path.isdir(stage2_dir):
        print(f"Error: Stage 2 output directory not found at {stage2_dir}.")
        return

    stage2_files = sorted(
        f for f in os.listdir(stage2_dir)
        if f.endswith("_research.json")
    )

    if not stage2_files:
        print(f"Error: no *_research.json files found in {stage2_dir}.")
        return

    target_companies = []
    for fname in stage2_files:
        path = os.path.join(stage2_dir, fname)
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
            await process_target_company(company, today_str, stage2_dir, stage3_output_dir)

    print(f"Processing {len(target_companies)} companies, up to {COMPANY_CONCURRENCY} at a time.")
    await asyncio.gather(
        *(bounded(c) for c in target_companies),
        return_exceptions=True,
    )
    print("\nAll companies processed.")


if __name__ == "__main__":
    asyncio.run(main())