"""
Workflow script for Stage 4 Consolidation.

Reads:
    - track_a_portfolio.json
    - track_b_portfolio.json
    - candidate_summaries.json
    - pre_optimization.json
    - Stage 3 outputs (for full per-company structured fields)

Orchestrates:
    1. Build union of Track A + Track B tickers
    2. Loop (max MAX_ITERATIONS):
         a. Call llmSelector to pick 15 names (with feedback if iter > 1)
         b. Call quantAllocator to allocate weights on those 15
         c. If allocator returns optimal: done
         d. If allocator returns infeasible: capture reason, retry
    3. Write consolidation_portfolio.json with full audit trail

Freshness: re-runs if consolidation_portfolio.json is missing OR older than any of:
    Track A, Track B, candidate_summaries, pre_optimization analysis_dates.
"""

import os
import sys
import json
import re
import asyncio
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# Allow importing siblings
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from llmSelector import select_consolidation_portfolio
from quantAllocator import allocate


# ============ CONFIG ============

MAX_ITERATIONS = 5  # soft cap on the feedback loop


# ============ LOGGING ============

log_filename = f"consolidation_log_{datetime.now().strftime('%Y-%m-%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(log_filename, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ============ PATHS ============

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STAGE4_ROOT = os.path.join(SCRIPT_DIR, "..")
OUTPUT_DIR = os.path.join(STAGE4_ROOT, "output")

CONSOLIDATION_PATH = os.path.join(OUTPUT_DIR, "consolidation_portfolio.json")
TRACK_A_PATH = os.path.join(OUTPUT_DIR, "track_a_portfolio.json")
TRACK_B_PATH = os.path.join(OUTPUT_DIR, "track_b_portfolio.json")
SUMMARIES_PATH = os.path.join(OUTPUT_DIR, "candidate_summaries.json")
PRE_OPT_PATH = os.path.join(OUTPUT_DIR, "pre_optimization.json")
STAGE3_OUTPUT_DIR = os.path.normpath(os.path.join(STAGE4_ROOT, "..", "Stage 3 DRAFT", "output"))
PROMPT_PATH = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "prompts", "consolidation_selector.md"))


# ============ HELPERS ============

def extract_ticker_from_company_name(company_name: str) -> str:
    if not company_name:
        return None
    match = re.search(r"\(([A-Z0-9\-\.]+)\)\s*$", company_name)
    return match.group(1) if match else None


def load_system_prompt() -> str:
    if not os.path.exists(PROMPT_PATH):
        raise FileNotFoundError(f"Prompt template not found at {PROMPT_PATH}")
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()


def load_track(path: str, label: str) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Track {label} portfolio not found at {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_summaries() -> tuple:
    if not os.path.exists(SUMMARIES_PATH):
        raise FileNotFoundError(f"Candidate summaries not found at {SUMMARIES_PATH}")
    with open(SUMMARIES_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    raw = data.get("summaries", {})
    summaries_by_ticker = {ticker: entry.get("summary", "") for ticker, entry in raw.items()}
    return summaries_by_ticker, data.get("analysis_date", "")


def load_pre_optimization() -> dict:
    if not os.path.exists(PRE_OPT_PATH):
        raise FileNotFoundError(f"Pre-optimization output not found at {PRE_OPT_PATH}")
    with open(PRE_OPT_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_stage3_candidates() -> dict:
    """Load full Stage 3 candidate data, indexed by ticker."""
    candidates = {}
    if not os.path.isdir(STAGE3_OUTPUT_DIR):
        raise FileNotFoundError(f"Stage 3 output dir not found: {STAGE3_OUTPUT_DIR}")

    for fname in sorted(os.listdir(STAGE3_OUTPUT_DIR)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(STAGE3_OUTPUT_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"Could not read {fname}: {e}")
            continue

        ticker = data.get("ticker") or extract_ticker_from_company_name(data.get("company_name", ""))
        if not ticker:
            continue

        vm = data.get("valuation_metrics") or {}
        sector = vm.get("sector") if isinstance(vm, dict) else None
        industry = vm.get("industry") if isinstance(vm, dict) else None

        candidates[ticker] = {
            "ticker": ticker,
            "company_name": data.get("company_name", ""),
            "sector": sector,
            "industry": industry,
            "conviction": data.get("conviction"),
            "expected_return_12m": data.get("expected_return_12m"),
            "base_return_12m": data.get("base_return_12m"),
            "upside_return_12m": data.get("upside_return_12m"),
            "downside_return_12m": data.get("downside_return_12m"),
            "scenario_probability_bull": data.get("scenario_probability_bull"),
            "scenario_probability_base": data.get("scenario_probability_base"),
            "scenario_probability_bear": data.get("scenario_probability_bear"),
            "key_invalidation_triggers": data.get("key_invalidation_triggers"),
            "consolidation_date": data.get("consolidation_date", ""),
        }

    return candidates


def extract_track_tickers(track_data: dict) -> list:
    """Extract the list of selected tickers from a Track A or Track B portfolio."""
    portfolio_section = track_data.get("portfolio") or {}
    positions = portfolio_section.get("positions") or track_data.get("positions", [])
    return [p.get("ticker") for p in positions if p.get("ticker")]


# ============ FRESHNESS ============

def is_consolidation_stale(
    track_a: dict, track_b: dict, summaries_date: str, pre_opt: dict
) -> bool:
    if not os.path.exists(CONSOLIDATION_PATH):
        return True

    try:
        with open(CONSOLIDATION_PATH, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except Exception:
        return True

    existing_date = existing.get("consolidation_date", "")
    if not existing_date:
        return True

    for date_field, label in [
        (track_a.get("analysis_date", ""), "Track A"),
        (track_b.get("analysis_date", ""), "Track B"),
        (summaries_date, "candidate summaries"),
        (pre_opt.get("analysis_date", ""), "pre-optimization"),
    ]:
        if date_field and existing_date < date_field:
            logger.info(f"Consolidation stale because {label} ({date_field}) is newer than {existing_date}")
            return True

    return False


# ============ MAIN ============

async def main():
    today_str = datetime.now().date().isoformat()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("Stage 4 Consolidation starting...")

    track_a = load_track(TRACK_A_PATH, "A")
    track_b = load_track(TRACK_B_PATH, "B")
    summaries_by_ticker, summaries_date = load_summaries()
    pre_opt = load_pre_optimization()
    all_stage3 = load_stage3_candidates()

    print(f"Loaded Track A (analysis_date={track_a.get('analysis_date', '')}).")
    print(f"Loaded Track B (analysis_date={track_b.get('analysis_date', '')}).")
    print(f"Loaded candidate summaries (analysis_date={summaries_date}).")
    print(f"Loaded pre-optimization (analysis_date={pre_opt.get('analysis_date', '')}).")
    print(f"Loaded {len(all_stage3)} Stage 3 candidate records.")

    if not is_consolidation_stale(track_a, track_b, summaries_date, pre_opt):
        print("[Consolidation] portfolio is current — skipping.")
        return

    # Build union of A + B tickers
    a_tickers = extract_track_tickers(track_a)
    b_tickers = extract_track_tickers(track_b)
    union_tickers = list(dict.fromkeys(a_tickers + b_tickers))  # dedups, preserves order
    print(f"\nTrack A tickers ({len(a_tickers)}): {a_tickers}")
    print(f"Track B tickers ({len(b_tickers)}): {b_tickers}")
    print(f"Union ({len(union_tickers)} unique): {union_tickers}")

    union_candidates = []
    for ticker in union_tickers:
        if ticker not in all_stage3:
            print(f"WARNING: union ticker {ticker} not found in Stage 3 outputs — skipping")
            continue
        union_candidates.append(all_stage3[ticker])

    if len(union_candidates) < 15:
        print(f"ERROR: union has only {len(union_candidates)} candidates with Stage 3 data; need at least 15.")
        sys.exit(1)

    system_prompt = load_system_prompt()
    print(f"\nLoaded system prompt ({len(system_prompt)} chars).")

    # ============ FEEDBACK LOOP ============
    iterations = []
    final_status = None
    final_allocator_result = None
    final_llm_parsed = None

    previous_response = None
    violation_reason = None

    for iter_num in range(1, MAX_ITERATIONS + 1):
        print(f"\n{'='*60}\n  Iteration {iter_num} / {MAX_ITERATIONS}\n{'='*60}")

        # 1. Call the selector LLM
        print(f"\n[Iter {iter_num}] Calling consolidation selector...")
        selector_result = await select_consolidation_portfolio(
            union_candidates=union_candidates,
            summaries_by_ticker=summaries_by_ticker,
            track_a=track_a,
            track_b=track_b,
            pre_opt=pre_opt,
            system_prompt=system_prompt,
            previous_response=previous_response,
            violation_reason=violation_reason,
        )

        parsed = selector_result["parsed"]
        violations = selector_result["selection_violations"]
        raw_response = selector_result["raw_response"]

        iteration_record = {
            "iteration": iter_num,
            "llm_selection_violations": violations,
            "llm_parsed": parsed,
            "allocator_result": None,
        }

        if violations:
            print(f"[Iter {iter_num}] Selection structural violations:")
            for v in violations:
                print(f"  - {v}")
            iterations.append(iteration_record)
            previous_response = raw_response
            violation_reason = (
                "Your output had structural issues:\n"
                + "\n".join(f"- {v}" for v in violations)
            )
            continue

        # 2. Call the allocator on the selected names
        selected_tickers = parsed["selected_tickers"]
        picks_for_allocator = []
        missing = []
        for t in selected_tickers:
            if t not in all_stage3:
                missing.append(t)
                continue
            c = all_stage3[t]
            picks_for_allocator.append({
                "ticker": t,
                "expected_return_12m": c.get("expected_return_12m"),
                "sector": c.get("sector"),
            })
        if missing:
            reason = f"Selected ticker(s) not in Stage 3 candidates: {missing}"
            print(f"[Iter {iter_num}] {reason}")
            iteration_record["allocator_result"] = {"status": "error", "infeasibility_reason": reason}
            iterations.append(iteration_record)
            previous_response = raw_response
            violation_reason = reason
            continue

        print(f"[Iter {iter_num}] Calling quant allocator on 15 selected names...")
        allocator_result = allocate(picks_for_allocator)
        iteration_record["allocator_result"] = allocator_result

        status = allocator_result["status"]
        print(f"[Iter {iter_num}] Allocator status: {status}")

        iterations.append(iteration_record)

        if status == "optimal":
            final_status = "optimal"
            final_allocator_result = allocator_result
            final_llm_parsed = parsed
            print(f"[Iter {iter_num}] SUCCESS — feasible allocation found.")
            break

        reason = allocator_result.get("infeasibility_reason", "Unknown infeasibility")
        print(f"[Iter {iter_num}] INFEASIBLE: {reason}")
        previous_response = raw_response
        violation_reason = reason

    if final_status != "optimal":
        final_status = "failed_iteration_cap"
        print(f"\n[FAIL] Hit iteration cap ({MAX_ITERATIONS}) without finding a feasible allocation.")

    # ============ BUILD FINAL OUTPUT ============
    portfolio = None
    if final_status == "optimal":
        allocations = final_allocator_result["allocations"]
        rationale_lookup = {
            r["ticker"]: r.get("rationale", "")
            for r in (final_llm_parsed.get("per_pick_rationale") or [])
        }

        positions = []
        for a in allocations:
            positions.append({
                "ticker": a["ticker"],
                "allocation_pct": a["allocation_pct"],
                "sector": a["sector"],
                "expected_return_12m": a["expected_return_12m"],
                "rationale": rationale_lookup.get(a["ticker"], ""),
            })

        portfolio = {
            "positions": positions,
            "comparison_notes": final_llm_parsed.get("comparison_notes", ""),
            "notable_rejections": final_llm_parsed.get("notable_rejections", []),
            "portfolio_thesis": final_llm_parsed.get("portfolio_thesis", ""),
            "key_risks": final_llm_parsed.get("key_risks", []),
        }

    output = {
        "consolidation_date": today_str,
        "method": "LLM selector (from union of A+B) + Quant allocator (MIQP-equivalent, LAMBDA=2.0)",
        "status": final_status,
        "iterations_run": len(iterations),
        "max_iterations": MAX_ITERATIONS,
        "track_a_tickers": a_tickers,
        "track_b_tickers": b_tickers,
        "union_tickers": union_tickers,
        "iterations": iterations,
        "objective_value": final_allocator_result.get("objective_value") if final_allocator_result else None,
        "constraints": final_allocator_result.get("constraints") if final_allocator_result else None,
        "portfolio": portfolio,
    }

    try:
        with open(CONSOLIDATION_PATH, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"Error writing {CONSOLIDATION_PATH}: {e}")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  Consolidation complete ({final_status}).")
    print(f"{'='*60}")
    print(f"\nIterations run: {len(iterations)}")

    if final_status == "optimal":
        print(f"Objective value: {output['objective_value']:.4f}")
        print(f"\nFinal portfolio:")
        for p in portfolio["positions"]:
            print(f"  {p['ticker']:6s}  {p['allocation_pct']:6.2f}%  "
                  f"sector={p['sector']:22s}  exp_ret_12m={p['expected_return_12m']:7.4f}")
        sector_totals = {}
        for p in portfolio["positions"]:
            sector_totals[p["sector"]] = sector_totals.get(p["sector"], 0.0) + p["allocation_pct"]
        print(f"\nSector allocations:")
        for sec, pct in sorted(sector_totals.items(), key=lambda x: -x[1]):
            print(f"  {sec:25s}  {pct:6.2f}%")
        print(f"\nWritten to {CONSOLIDATION_PATH}")
    else:
        print(f"\nNo feasible portfolio produced. See {CONSOLIDATION_PATH} for iteration audit trail.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())