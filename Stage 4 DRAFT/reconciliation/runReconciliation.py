"""
Stage 4 Sub-stage 6 — Portfolio Reconciliation.

Stage 4 is otherwise stateless: every run rebuilds the optimal portfolio from
scratch. This sub-stage makes it stateful. When a portfolio already exists, it
treats construction as a *transition* problem — "given I already hold portfolio X,
what is the best portfolio to move to?" — and gates every name change behind an
adversarial debate so churn is never arbitrary.

Flow per run (after sub-stages 1-5 have written the fresh candidate to
output/consolidation_portfolio.json):

    1. Freshness check — skip if this candidate was already reconciled.
    2. Locate the incumbent = newest file in output/portfolio history/.
    3. First run (no incumbent): seed entry_date/entry_price on every position,
       stamp metadata, archive — no debates.
    4. Otherwise: refresh prices, compute realized P&L, then run the
       proposer<->debate loop to convergence and write the reconciled portfolio.

The reconciled portfolio is written back to consolidation_portfolio.json (the
single canonical deliverable Stage 5 consumes) and archived to portfolio history/.
"""

import os
import sys
import re
import json
import logging
import importlib.util
from datetime import datetime, timezone

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)  # so sibling modules import cleanly

from reconciliationOptimiser import propose
from reconciliationDebate import adjudicate


# ============ LOGGING ============

log_filename = f"reconciliation_log_{datetime.now().strftime('%Y-%m-%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(log_filename, encoding='utf-8'),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ============ PATHS ============

STAGE4_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
REPO_ROOT = os.path.normpath(os.path.join(STAGE4_ROOT, ".."))
OUTPUT_DIR = os.path.join(STAGE4_ROOT, "output")
HISTORY_DIR = os.path.join(OUTPUT_DIR, "portfolio history")
CONSOLIDATION_PATH = os.path.join(OUTPUT_DIR, "consolidation_portfolio.json")
PRICE_CACHE_DIR = os.path.join(STAGE4_ROOT, "cache", "prices")
STAGE3_OUTPUT_DIR = os.path.normpath(os.path.join(STAGE4_ROOT, "..", "Stage 3 DRAFT", "output"))
PRICE_CACHE_MODULE = os.path.join(STAGE4_ROOT, "pre optimisation", "price cache", "priceCache.py")
DEEP_RESEARCH_DIR = os.path.join(REPO_ROOT, "Stage 2 DRAFT", "deep research")


# ============ CONFIG ============

MAX_ITERATIONS = 40  # hard safety cap; the loop is bounded by union size (<=30)


# ============ DYNAMIC IMPORTS (folders contain spaces) ============

def _load_module(module_path: str, module_name: str, extra_syspath: str = None):
    if extra_syspath and extra_syspath not in sys.path:
        sys.path.insert(0, extra_syspath)
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_price_cache = _load_module(PRICE_CACHE_MODULE, "reconciliation_priceCache")
ensure_price_cache = _price_cache.ensure_price_cache
load_cached_prices = _price_cache.load_cached_prices

_deep_research = _load_module(
    os.path.join(DEEP_RESEARCH_DIR, "deepResearch.py"),
    "reconciliation_deepResearch",
    extra_syspath=DEEP_RESEARCH_DIR,
)
run_deep_research = _deep_research.run_deep_research


# ============ HELPERS ============

def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_ticker_from_company_name(company_name: str) -> str:
    if not company_name:
        return None
    match = re.search(r"\(([A-Z0-9\-\.]+)\)\s*$", company_name)
    return match.group(1) if match else None


def find_incumbent_path() -> str:
    """Newest portfolio in the archive, or None on first ever run."""
    if not os.path.isdir(HISTORY_DIR):
        return None
    files = [f for f in os.listdir(HISTORY_DIR) if f.endswith(".json")]
    if not files:
        return None
    # Filenames are portfolio_<UTC-timestamp>.json — lexical sort is chronological.
    return os.path.join(HISTORY_DIR, sorted(files)[-1])


def load_stage3() -> dict:
    """Load Stage 3 per-ticker research, indexed by ticker."""
    out = {}
    if not os.path.isdir(STAGE3_OUTPUT_DIR):
        logger.warning(f"Stage 3 output dir not found: {STAGE3_OUTPUT_DIR}")
        return out
    for fname in sorted(os.listdir(STAGE3_OUTPUT_DIR)):
        if not fname.endswith(".json"):
            continue
        try:
            data = _load_json(os.path.join(STAGE3_OUTPUT_DIR, fname))
        except Exception as e:
            logger.warning(f"Could not read {fname}: {e}")
            continue
        ticker = data.get("ticker") or extract_ticker_from_company_name(
            data.get("company_name", ""))
        if not ticker:
            continue
        vm = data.get("valuation_metrics") or {}
        out[ticker] = {
            "ticker": ticker,
            "company_name": data.get("company_name", "") or ticker,
            "sector": vm.get("sector") if isinstance(vm, dict) else None,
            "conviction": data.get("conviction"),
            "expected_return_12m": data.get("expected_return_12m"),
            "base_return_12m": data.get("base_return_12m"),
            "thesis_summary": data.get("thesis_summary"),
            "synthesis": data.get("synthesis"),
            "key_invalidation_triggers": data.get("key_invalidation_triggers"),
            "consolidation_date": data.get("consolidation_date", ""),
        }
    return out


def positions_of(portfolio_doc: dict) -> list:
    return ((portfolio_doc or {}).get("portfolio") or {}).get("positions") or []


def latest_close(ticker: str):
    """(price, date_str) of the most recent cached close, or (None, None)."""
    df = load_cached_prices(ticker, PRICE_CACHE_DIR)
    if df is None or df.empty:
        return None, None
    df = df.sort_values("Date")
    return float(df["Close"].iloc[-1]), str(pd.Timestamp(df["Date"].iloc[-1]).date())


def close_on_or_before(ticker: str, date_str: str):
    """Last cached close on or before date_str, or None."""
    df = load_cached_prices(ticker, PRICE_CACHE_DIR)
    if df is None or df.empty or not date_str:
        return None
    df = df.sort_values("Date")
    sub = df[df["Date"] <= pd.Timestamp(date_str)]
    if sub.empty:
        return None
    return float(sub["Close"].iloc[-1])


def refresh_prices(tickers):
    for t in sorted(set(tickers)):
        try:
            result = ensure_price_cache(t, PRICE_CACHE_DIR)
            logger.info(f"[price] {t}: {result.get('status')}")
        except Exception as e:
            logger.warning(f"[price] {t}: refresh failed — {e}")


def archive_portfolio(portfolio_doc: dict):
    os.makedirs(HISTORY_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(HISTORY_DIR, f"portfolio_{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(portfolio_doc, f, indent=4, ensure_ascii=False)
    logger.info(f"Archived portfolio to {path}")


def write_output(portfolio_doc: dict):
    with open(CONSOLIDATION_PATH, "w", encoding="utf-8") as f:
        json.dump(portfolio_doc, f, indent=4, ensure_ascii=False)
    logger.info(f"Wrote reconciled portfolio to {CONSOLIDATION_PATH}")


def build_research_dump(change_kind, ticker, s3, candidate_pos, incumbent_pos,
                        pnl, delta_research, is_stale) -> str:
    parts = []
    research_date = (s3 or {}).get("consolidation_date") or "unknown date"
    parts.append(f"## Original investment thesis (researched {research_date})")
    if s3:
        parts.append(f"- Conviction: {s3.get('conviction')}")
        parts.append(f"- Expected 12m return: {s3.get('expected_return_12m')}")
        parts.append(f"- Base-case 12m return: {s3.get('base_return_12m')}")
        if s3.get("thesis_summary"):
            parts.append(f"\n### Thesis summary\n{s3['thesis_summary']}")
        if s3.get("synthesis"):
            parts.append(f"\n### Prior debate synthesis\n{s3['synthesis']}")
        triggers = s3.get("key_invalidation_triggers")
        if triggers:
            parts.append("\n### Key invalidation triggers\n"
                          + "\n".join(f"- {t}" for t in triggers))
    else:
        parts.append("(No Stage 3 research record found for this ticker.)")
    if is_stale:
        parts.append("\n**Staleness note:** this name's Stage 3 research was not "
                      "refreshed in the latest pipeline run — treat its expected "
                      "return as potentially stale.")

    parts.append(f"\n## Lightweight update — what has changed since\n{delta_research}")

    if change_kind == "drop_incumbent" and pnl:
        rtd = pnl.get("return_to_date")
        rtd_str = "unknown" if rtd is None else f"{rtd * 100:.1f}%"
        parts.append("\n## Realized performance since entry")
        parts.append(f"- Entry date: {pnl.get('entry_date')}")
        parts.append(f"- Entry price: {pnl.get('entry_price')}")
        parts.append(f"- Current price: {pnl.get('current_price')}")
        parts.append(f"- Return to date: {rtd_str}")

    if incumbent_pos and incumbent_pos.get("rationale"):
        parts.append("\n## Rationale when this position was last adopted\n"
                      + incumbent_pos["rationale"])
    if candidate_pos and candidate_pos.get("rationale"):
        parts.append("\n## Fresh candidate portfolio's rationale for this name\n"
                      + candidate_pos["rationale"])
    return "\n".join(parts)


async def lightweight_delta_research(company_name, ticker, since_date) -> str:
    question = (
        f"What has materially changed for {company_name} ({ticker}) since "
        f"{since_date or 'its last research'}? Focus on news, earnings, guidance, "
        f"and any developments that would confirm or invalidate the investment "
        f"thesis. Keep it concise."
    )
    try:
        result = await run_deep_research(question=question, system_prompt_type="NEWS")
        return result or "(No update returned.)"
    except Exception as e:
        logger.warning(f"[{ticker}] delta research failed: {e}")
        return f"(Delta research unavailable: {e})"


# ============ FIRST RUN ============

def seed_entry_fields(positions, today_str):
    """Seed entry_date/entry_price on positions that lack them."""
    for p in positions:
        if not p.get("entry_date"):
            p["entry_date"] = today_str
        if p.get("entry_price") is None:
            price, _ = latest_close(p["ticker"])
            p["entry_price"] = round(price, 4) if price is not None else None


def do_first_run(candidate: dict, today_str: str):
    logger.info("No incumbent found — first run. Seeding entry data, no debates.")
    positions = positions_of(candidate)
    refresh_prices([p["ticker"] for p in positions])
    seed_entry_fields(positions, today_str)
    candidate["reconciled_date"] = today_str
    candidate["reconciliation"] = {
        "status": "first_run_no_incumbent",
        "note": "No prior portfolio existed; the candidate was adopted as-is and "
                "entry data was seeded for future reconciliations.",
        "incumbent_file": None,
    }
    write_output(candidate)
    archive_portfolio(candidate)
    logger.info("First-run reconciliation complete.")


# ============ MAIN ============

async def main():
    today_str = datetime.now().date().isoformat()

    if not os.path.exists(CONSOLIDATION_PATH):
        logger.error(f"Candidate portfolio not found at {CONSOLIDATION_PATH}")
        sys.exit(1)

    candidate = _load_json(CONSOLIDATION_PATH)
    consolidation_date = candidate.get("consolidation_date", "")

    # Freshness — skip if this candidate was already reconciled.
    reconciled_date = candidate.get("reconciled_date", "")
    if reconciled_date and consolidation_date and reconciled_date >= consolidation_date:
        logger.info("[Reconciliation] candidate already reconciled — skipping.")
        return

    incumbent_path = find_incumbent_path()
    if incumbent_path is None:
        do_first_run(candidate, today_str)
        return

    incumbent = _load_json(incumbent_path)
    incumbent_date = incumbent.get("reconciled_date") or incumbent.get("consolidation_date", "")
    logger.info(f"Incumbent: {os.path.basename(incumbent_path)} (date {incumbent_date})")

    incumbent_positions = positions_of(incumbent)
    candidate_positions = positions_of(candidate)
    if not incumbent_positions or not candidate_positions:
        logger.error("Incumbent or candidate has no positions — cannot reconcile.")
        sys.exit(1)

    incumbent_pos_by_ticker = {p["ticker"]: p for p in incumbent_positions}
    candidate_pos_by_ticker = {p["ticker"]: p for p in candidate_positions}
    incumbent_weights = {
        p["ticker"]: float(p.get("allocation_pct", 0.0)) / 100.0
        for p in incumbent_positions
    }
    candidate_tickers = set(candidate_pos_by_ticker)
    union_tickers = list(dict.fromkeys(
        list(incumbent_pos_by_ticker) + list(candidate_pos_by_ticker)))

    # Refresh prices for the whole union so current prices are truly current.
    refresh_prices(union_tickers)

    stage3 = load_stage3()

    # Build the optimiser's union (needs expected_return_12m + sector per name).
    union_for_opt = []
    skipped = []
    for t in union_tickers:
        s3 = stage3.get(t)
        cand_pos = candidate_pos_by_ticker.get(t)
        inc_pos = incumbent_pos_by_ticker.get(t)
        exp_ret = (s3 or {}).get("expected_return_12m")
        if exp_ret is None and cand_pos:
            exp_ret = cand_pos.get("expected_return_12m")
        if exp_ret is None and inc_pos:
            exp_ret = inc_pos.get("expected_return_12m")
        sector = (s3 or {}).get("sector")
        if not sector and cand_pos:
            sector = cand_pos.get("sector")
        if not sector and inc_pos:
            sector = inc_pos.get("sector")
        if exp_ret is None or not sector:
            skipped.append(t)
            continue
        union_for_opt.append({
            "ticker": t,
            "expected_return_12m": exp_ret,
            "sector": sector,
        })
    if skipped:
        logger.warning(f"Skipped union tickers missing return/sector data: {skipped}")
    if len(union_for_opt) < 15:
        logger.error(f"Only {len(union_for_opt)} union names have usable data; "
                      f"need at least 15. Cannot reconcile.")
        sys.exit(1)

    # Staleness — flag names not refreshed in the latest research pass.
    s3_dates = [v.get("consolidation_date") for v in stage3.values()
                if v.get("consolidation_date")]
    latest_research_date = max(s3_dates) if s3_dates else ""
    stale_tickers = {
        t for t in union_tickers
        if stage3.get(t) and stage3[t].get("consolidation_date", "") < latest_research_date
    }

    # Realized P&L per incumbent position.
    pnl_by_ticker = {}
    for p in incumbent_positions:
        t = p["ticker"]
        entry_date = p.get("entry_date") or incumbent_date
        entry_price = p.get("entry_price")
        if entry_price is None:
            entry_price = close_on_or_before(t, entry_date)
        current_price, _ = latest_close(t)
        return_to_date = None
        if entry_price and current_price:
            return_to_date = current_price / entry_price - 1.0
        pnl_by_ticker[t] = {
            "entry_date": entry_date,
            "entry_price": round(entry_price, 4) if entry_price else None,
            "current_price": round(current_price, 4) if current_price else None,
            "return_to_date": return_to_date,
        }

    # ============ PROPOSER <-> DEBATE LOOP ============
    must_hold, must_drop = set(), set()
    iterations = []
    last_proposal = None
    status = "reconciled"

    for iter_num in range(1, MAX_ITERATIONS + 1):
        proposal = propose(
            union=union_for_opt,
            incumbent_weights=incumbent_weights,
            candidate_tickers=candidate_tickers,
            must_include=must_hold,
            must_exclude=must_drop,
        )
        if proposal["status"] != "optimal":
            logger.error(f"[Iter {iter_num}] proposer infeasible: "
                          f"{proposal['infeasibility_reason']}")
            status = "failed_reconciliation_infeasible"
            iterations.append({
                "iteration": iter_num,
                "proposer_status": proposal["status"],
                "infeasibility_reason": proposal["infeasibility_reason"],
            })
            break

        last_proposal = proposal
        proposal_tickers = {p["ticker"] for p in proposal["positions"]}
        incumbent_set = set(incumbent_weights)
        dropped = incumbent_set - proposal_tickers
        added = proposal_tickers - incumbent_set

        unadjudicated = []
        for t in dropped:
            if t not in must_drop:
                unadjudicated.append(("drop_incumbent", t, incumbent_weights[t]))
        proposal_weight = {p["ticker"]: p["allocation_pct"] / 100.0
                           for p in proposal["positions"]}
        for t in added:
            if t not in must_hold:
                unadjudicated.append(("add_name", t, proposal_weight.get(t, 0.0)))

        if not unadjudicated:
            logger.info(f"[Iter {iter_num}] all name changes adjudicated — converged.")
            break

        # Highest-impact change first.
        unadjudicated.sort(key=lambda x: -x[2])
        change_kind, ticker, impact = unadjudicated[0]
        s3 = stage3.get(ticker)
        company_name = (s3 or {}).get("company_name") or ticker
        logger.info(f"[Iter {iter_num}] debating {change_kind} for {ticker} "
                    f"(impact weight {impact:.4f})")

        delta = await lightweight_delta_research(
            company_name, ticker, (s3 or {}).get("consolidation_date"))
        dump = build_research_dump(
            change_kind, ticker, s3,
            candidate_pos_by_ticker.get(ticker),
            incumbent_pos_by_ticker.get(ticker),
            pnl_by_ticker.get(ticker),
            delta, ticker in stale_tickers,
        )
        verdict = await adjudicate(change_kind, ticker, company_name, dump)

        if change_kind == "drop_incumbent":
            lock = "must_hold" if verdict["resist_change"] else "must_drop"
        else:  # add_name
            lock = "must_drop" if verdict["resist_change"] else "must_hold"
        (must_hold if lock == "must_hold" else must_drop).add(ticker)

        iterations.append({
            "iteration": iter_num,
            "proposal_tickers": sorted(proposal_tickers),
            "change_debated": {"kind": change_kind, "ticker": ticker,
                               "impact_weight": round(impact, 4)},
            "verdict": {
                "score": verdict["score"],
                "categorical": verdict["categorical"],
                "score_confidence": verdict["score_confidence"],
                "resist_change": verdict["resist_change"],
            },
            "lock_applied": {lock: ticker},
        })
    else:
        status = "failed_iteration_cap"
        logger.error(f"Hit iteration cap ({MAX_ITERATIONS}) without converging.")

    if last_proposal is None:
        logger.error("No feasible proposal was ever produced — cannot write output.")
        sys.exit(1)

    # ============ BUILD RECONCILED PORTFOLIO ============
    final_positions = []
    for p in last_proposal["positions"]:
        t = p["ticker"]
        cand_pos = candidate_pos_by_ticker.get(t)
        inc_pos = incumbent_pos_by_ticker.get(t)
        rationale = ""
        if cand_pos and cand_pos.get("rationale"):
            rationale = cand_pos["rationale"]
        elif inc_pos and inc_pos.get("rationale"):
            rationale = inc_pos["rationale"]

        if inc_pos:  # kept name — carry entry data forward
            entry_date = inc_pos.get("entry_date") or incumbent_date
            entry_price = inc_pos.get("entry_price")
            if entry_price is None:
                entry_price = close_on_or_before(t, entry_date)
        else:  # newly added — entry is today
            entry_date = today_str
            entry_price, _ = latest_close(t)

        final_positions.append({
            "ticker": t,
            "allocation_pct": p["allocation_pct"],
            "sector": p["sector"],
            "expected_return_12m": p["expected_return_12m"],
            "rationale": rationale,
            "entry_date": entry_date,
            "entry_price": round(entry_price, 4) if entry_price else None,
        })
    final_positions.sort(key=lambda p: -p["allocation_pct"])

    final_tickers = {p["ticker"] for p in final_positions}
    incumbent_set = set(incumbent_weights)
    final_weight = {p["ticker"]: p["allocation_pct"] / 100.0 for p in final_positions}
    total_abs_weight_change = sum(
        abs(final_weight.get(t, 0.0) - incumbent_weights.get(t, 0.0))
        for t in set(final_weight) | incumbent_set
    )

    candidate["reconciled_date"] = today_str
    candidate["reconciliation"] = {
        "status": status,
        "incumbent_file": os.path.basename(incumbent_path),
        "incumbent_date": incumbent_date,
        "candidate_consolidation_date": consolidation_date,
        "iterations_run": len(iterations),
        "max_iterations": MAX_ITERATIONS,
        "debates_run": sum(1 for it in iterations if "verdict" in it),
        "turnover_summary": {
            "added": sorted(final_tickers - incumbent_set),
            "dropped": sorted(incumbent_set - final_tickers),
            "kept": sorted(final_tickers & incumbent_set),
            "total_abs_weight_change": round(total_abs_weight_change, 6),
        },
        "iterations": iterations,
    }
    if candidate.get("portfolio") is None:
        candidate["portfolio"] = {}
    candidate["portfolio"]["positions"] = final_positions

    write_output(candidate)
    archive_portfolio(candidate)

    summary = candidate["reconciliation"]["turnover_summary"]
    logger.info(f"Reconciliation complete ({status}). "
                f"added={summary['added']} dropped={summary['dropped']} "
                f"total_abs_weight_change={summary['total_abs_weight_change']}")
    if status not in ("reconciled",):
        logger.warning(f"Reconciliation finished with status '{status}' — the last "
                        f"feasible proposal was written.")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
