"""
Stage 5 Monitor — main orchestrator.

Per-run flow:
  1. Setup logging, load config, determine cadence window
  2. Drain pending reruns queue (from previous runs that couldn't invoke)
  3. Refresh baselines if first run of day (percentile + beta)
  4. Fetch live data (price, volume, macro context, earnings)
  5. Run signal analyzers in parallel
  6. Aggregate signals → triggered tickers
  7. Gate 0 (mechanical macro filter)
  8. Call 1 + Call 2 LLM gates (bounded concurrency)
  9. Build rerun list from Call 2 verdicts
  10. Invoke pipeline OR queue
  11. Write observation log (JSONL, append-only)
  12. Write aggregate run log + evidence packets

Designed to be:
  - Best-effort and idempotent: no resume semantics, every run starts fresh
  - Stage-independent: only consumes finished Stage 4 outputs
  - Conservative on failures: defaults to no-rerun whenever data is missing
  - Time-bounded: bounded concurrency, retries with backoff, terminal failures
                  surface as no-rerun rather than blocking

Usage:
    python3 runMonitor.py                          # full normal run
    python3 runMonitor.py --dry-run                # do everything except invoke
    python3 runMonitor.py --skip-llm               # do mechanical work only
    python3 runMonitor.py --cadence post_close     # force a specific cadence
    python3 runMonitor.py --tickers ARDX NFLX      # subset for testing
"""

import os
import sys
import glob
import json
import asyncio
import logging
import argparse
import tempfile
import traceback
import importlib.util
from datetime import datetime, timezone, timedelta
from typing import Optional


# ============ PATHS ============

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MONITOR_ROOT = SCRIPT_DIR
STAGE_5_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
PROJECT_ROOT = os.path.normpath(os.path.join(STAGE_5_ROOT, ".."))

DATA_SOURCES_DIR = os.path.join(MONITOR_ROOT, "data sources")
BASELINES_DIR = os.path.join(MONITOR_ROOT, "baselines")
SIGNAL_ANALYZERS_DIR = os.path.join(MONITOR_ROOT, "signal analyzers")
GATES_DIR = os.path.join(MONITOR_ROOT, "gates")
STATE_DIR = os.path.join(MONITOR_ROOT, "state")
EVIDENCE_DIR = os.path.join(MONITOR_ROOT, "evidence_packets")
LOGS_DIR = os.path.join(STAGE_5_ROOT, "logs")
RUN_OUTPUT_DIR = os.path.join(MONITOR_ROOT, "output")

# Stage 3 outputs — used as the authoritative source for the candidate
# universe this cycle. Stage 3's output folder is pruned each cycle so its
# contents always match the Stage 1 target candidate list.
STAGE_3_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "Stage 3 DRAFT", "output")

# Stage 4 outputs
STAGE_4_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "Stage 4 DRAFT", "output")
CONSOLIDATION_PORTFOLIO_PATH = os.path.join(STAGE_4_OUTPUT_DIR, "consolidation_portfolio.json")
CANDIDATE_SUMMARIES_PATH = os.path.join(STAGE_4_OUTPUT_DIR, "candidate_summaries.json")

# Monitor state files
PERCENTILE_BASELINES_PATH = os.path.join(STATE_DIR, "percentile_baselines.json")
BETA_BASELINES_PATH = os.path.join(STATE_DIR, "beta_baselines.json")
ANCHORS_PATH = os.path.join(STATE_DIR, "ticker_evaluation_anchors.json")
OBSERVATIONS_PATH = os.path.join(STATE_DIR, "monitor_observations.jsonl")
PENDING_RERUNS_PATH = os.path.join(STAGE_5_ROOT, "state", "monitor_pending_reruns.json")
BASELINE_REFRESH_MARKER_PATH = os.path.join(STATE_DIR, "baseline_last_refreshed.txt")

# Pipeline invoker
PIPELINE_INVOKER_DIR = os.path.join(STAGE_5_ROOT, "pipeline invoker")
PIPELINE_INVOKER_PATH = os.path.join(PIPELINE_INVOKER_DIR, "invokePipeline.py")


# ============ CONFIG ============

# Cadence windows. Each window has a name and the ticker scope.
# Times are in America/New_York timezone. The window is the "intended" run
# time; an actual run within ±20 minutes of a window time is mapped to it.
CADENCE_WINDOWS = {
    "post_open":    {"time_et": "10:00", "scope": "held"},       # just after open settling
    "pre_open":     {"time_et": "09:00", "scope": "held"},       # before US open
    "pre_close":    {"time_et": "15:30", "scope": "held"},       # before US close
    "post_close":   {"time_et": "16:30", "scope": "full"},       # after close — full universe
}
CADENCE_MATCH_TOLERANCE_MINUTES = 20

# Concurrency caps. Data fetching is mostly IO-bound (yfinance/network);
# LLM calls are also IO-bound but more expensive per call.
DATA_FETCH_CONCURRENCY = 5
SIGNAL_ANALYZER_CONCURRENCY = 8
LLM_CONCURRENCY = 3

# Pending reruns queue
PENDING_RERUNS_SOFT_CAP = 20

# Baselines
BASELINE_LOOKBACK_DAYS = 60


# ============ LOGGER ============

logger = logging.getLogger("monitor.runMonitor")


def _setup_logging(run_id: str):
    os.makedirs(LOGS_DIR, exist_ok=True)
    log_path = os.path.join(LOGS_DIR, f"monitor_{run_id}.log")
    if not logger.handlers:
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        stream_handler = logging.StreamHandler()
        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(fmt)
        stream_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
        logger.addHandler(stream_handler)
        logger.setLevel(logging.INFO)


# ============ MODULE IMPORTS ============
# Import the monitor submodules. Done dynamically so the orchestrator works
# regardless of cwd.

def _import_modules():
    """Add subdirectories to sys.path and import all submodules.
    Returns a dict of module references."""
    for d in (DATA_SOURCES_DIR, BASELINES_DIR, SIGNAL_ANALYZERS_DIR, GATES_DIR):
        if d not in sys.path:
            sys.path.insert(0, d)

    import priceData
    import volumeData
    import macroContext
    import alpacaPositions
    import earningsCalendar
    import percentileBaselines
    import betaBaselines
    import priceSignalAnalyzer
    import volumeSignalAnalyzer
    import cumulativeSignalAnalyzer
    import signalAggregator
    import gate0Filter
    import callOneInvestigator
    import callTwoDecider

    return {
        "priceData": priceData,
        "volumeData": volumeData,
        "macroContext": macroContext,
        "alpacaPositions": alpacaPositions,
        "earningsCalendar": earningsCalendar,
        "percentileBaselines": percentileBaselines,
        "betaBaselines": betaBaselines,
        "priceSignalAnalyzer": priceSignalAnalyzer,
        "volumeSignalAnalyzer": volumeSignalAnalyzer,
        "cumulativeSignalAnalyzer": cumulativeSignalAnalyzer,
        "signalAggregator": signalAggregator,
        "gate0Filter": gate0Filter,
        "callOneInvestigator": callOneInvestigator,
        "callTwoDecider": callTwoDecider,
    }


# ============ ATOMIC IO ============

def _atomic_write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    parent = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".monitor.", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def _append_jsonl(path: str, obj) -> None:
    """Append one JSON object as a line to a .jsonl file. Best-effort."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, default=str, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"Could not append to {path}: {e}")


# ============ CADENCE WINDOW DETERMINATION ============

def determine_cadence_window(now: Optional[datetime] = None,
                              override: Optional[str] = None) -> dict:
    """
    Determine which cadence window we're in based on the current time
    (in America/New_York timezone). Returns the matching window dict + name,
    or a default if we're not within tolerance of any configured time.

    Args:
        now: current UTC datetime (defaults to now)
        override: force a specific cadence name (e.g., "post_close")

    Returns:
        {"name": str, "scope": "held" | "full", "matched": bool,
         "actual_time_et": "HH:MM"}
    """
    if override:
        if override not in CADENCE_WINDOWS:
            raise ValueError(f"Unknown cadence override: {override}. "
                             f"Valid: {list(CADENCE_WINDOWS.keys())}")
        w = CADENCE_WINDOWS[override]
        return {
            "name": override,
            "scope": w["scope"],
            "matched": True,
            "actual_time_et": w["time_et"],
            "forced": True,
        }

    if now is None:
        now = datetime.now(timezone.utc)

    # Convert to America/New_York. We do this without pytz/zoneinfo
    # by using a simple offset — but that misses DST. So we use zoneinfo
    # if available, falling back to UTC-5 (EST, no DST).
    try:
        from zoneinfo import ZoneInfo
        et_now = now.astimezone(ZoneInfo("America/New_York"))
    except (ImportError, Exception):
        # Fall back: assume EST (UTC-5). Less accurate during DST months.
        et_now = now.astimezone(timezone(timedelta(hours=-5)))

    current_minutes = et_now.hour * 60 + et_now.minute
    best_match = None
    best_distance = None

    for name, w in CADENCE_WINDOWS.items():
        hh, mm = w["time_et"].split(":")
        window_minutes = int(hh) * 60 + int(mm)
        distance = abs(current_minutes - window_minutes)
        if distance <= CADENCE_MATCH_TOLERANCE_MINUTES:
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_match = name

    if best_match:
        w = CADENCE_WINDOWS[best_match]
        return {
            "name": best_match,
            "scope": w["scope"],
            "matched": True,
            "actual_time_et": et_now.strftime("%H:%M"),
            "forced": False,
        }

    # No match — return a synthetic window with scope=held as a safe default
    return {
        "name": "unscheduled",
        "scope": "held",
        "matched": False,
        "actual_time_et": et_now.strftime("%H:%M"),
        "forced": False,
    }


# ============ STAGE 3 UNIVERSE SCAN ============

def _scan_stage_3_universe() -> dict:
    """
    Scan Stage 3 output directory to find all candidates in the current cycle.
    Stage 3's output folder is pruned each cycle to match the target candidate
    list from Stage 1, so its contents are authoritative for "the 50 candidates
    we're tracking this cycle".

    Ticker extraction prefers valuation_metrics.ticker; falls back to parsing
    the parenthesized ticker out of company_name like "NETFLIX INC (NFLX)".
    Sector extraction prefers valuation_metrics.sector.

    Returns:
        {
            "tickers": [list of ticker symbols found],
            "sector_by_ticker": {ticker: sector_str, ...},
            "files_scanned": int,
            "files_skipped": int,
            "errors": [str],
        }
    """
    result = {
        "tickers": [],
        "sector_by_ticker": {},
        "files_scanned": 0,
        "files_skipped": 0,
        "errors": [],
    }

    if not os.path.isdir(STAGE_3_OUTPUT_DIR):
        logger.warning(f"Stage 3 output directory not found at {STAGE_3_OUTPUT_DIR}")
        return result

    pattern = os.path.join(STAGE_3_OUTPUT_DIR, "*_research.json")
    files = sorted(glob.glob(pattern))

    if not files:
        logger.warning(f"No *_research.json files found in {STAGE_3_OUTPUT_DIR}")
        return result

    seen_tickers = set()  # dedupe in case of any accidental duplicates

    for fpath in files:
        result["files_scanned"] += 1
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            result["errors"].append(f"{os.path.basename(fpath)}: {e}")
            result["files_skipped"] += 1
            continue

        # Prefer the explicit valuation_metrics.ticker; fall back to parsing
        # company_name like "NETFLIX INC (NFLX)".
        ticker = None
        sector = None

        vm = data.get("valuation_metrics") or {}
        if isinstance(vm, dict):
            ticker = vm.get("ticker") or None
            sector = vm.get("sector") or None

        if not ticker:
            company_name = data.get("company_name") or ""
            # Pattern: "NETFLIX INC (NFLX)" — extract content of last parens
            if "(" in company_name and ")" in company_name:
                try:
                    ticker = company_name.rsplit("(", 1)[1].rsplit(")", 1)[0].strip()
                except Exception:
                    ticker = None

        if not ticker:
            result["errors"].append(
                f"{os.path.basename(fpath)}: could not extract ticker"
            )
            result["files_skipped"] += 1
            continue

        if ticker in seen_tickers:
            result["files_skipped"] += 1
            continue
        seen_tickers.add(ticker)

        result["tickers"].append(ticker)
        if sector:
            result["sector_by_ticker"][ticker] = sector

    logger.info(
        f"Stage 3 universe scan: {len(result['tickers'])} ticker(s) found "
        f"({result['files_scanned']} files scanned, "
        f"{result['files_skipped']} skipped, "
        f"{len(result['errors'])} errors)"
    )
    return result


# ============ PORTFOLIO LOADING ============

def load_portfolio_tickers(scope: str) -> dict:
    """
    Load ticker universe.

    Held positions come from Stage 4's consolidation_portfolio.json.
    Unselected candidates are DERIVED: scan Stage 3's output directory for
    the full candidate universe this cycle, then subtract held tickers.
    (Stage 3's output folder is pruned each cycle to match Stage 1's target
    candidate list, so it's the authoritative source for "all 50 candidates".)

    Args:
        scope: "held" | "full"

    Returns:
        {
            "held": [list of held ticker symbols],
            "unselected": [list of unselected ticker symbols],
            "all_in_scope": [tickers to monitor this run],
            "thesis_by_ticker": {ticker: thesis_dict, ...},
            "sector_by_ticker": {ticker: sector_str, ...},
            "consolidation_date": str or None,
        }
    """
    result = {
        "held": [],
        "unselected": [],
        "all_in_scope": [],
        "thesis_by_ticker": {},
        "sector_by_ticker": {},
        "consolidation_date": None,
    }

    if not os.path.exists(CONSOLIDATION_PORTFOLIO_PATH):
        logger.error(f"consolidation_portfolio.json not found at {CONSOLIDATION_PORTFOLIO_PATH}")
        return result

    try:
        with open(CONSOLIDATION_PORTFOLIO_PATH, "r", encoding="utf-8") as f:
            consol = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Could not load consolidation_portfolio.json: {e}")
        return result

    result["consolidation_date"] = consol.get("consolidation_date")

    # Held positions
    portfolio = consol.get("portfolio") or {}
    positions = portfolio.get("positions") or []
    for p in positions:
        t = p.get("ticker")
        if t:
            result["held"].append(t)
            if p.get("sector"):
                result["sector_by_ticker"][t] = p["sector"]

    # Unselected candidates: derived from Stage 3's output directory
    # (which is pruned each cycle to match the Stage 1 target candidate list).
    # Unselected = all candidates in Stage 3 output − held tickers.
    held_set = set(result["held"])
    stage_3_scan = _scan_stage_3_universe()
    for t in stage_3_scan["tickers"]:
        if t in held_set:
            # Already accounted for in held; just absorb sector if missing
            if t not in result["sector_by_ticker"] and t in stage_3_scan["sector_by_ticker"]:
                result["sector_by_ticker"][t] = stage_3_scan["sector_by_ticker"][t]
            continue
        result["unselected"].append(t)
        if t in stage_3_scan["sector_by_ticker"]:
            result["sector_by_ticker"][t] = stage_3_scan["sector_by_ticker"][t]

    if stage_3_scan["errors"]:
        logger.warning(
            f"Stage 3 universe scan had {len(stage_3_scan['errors'])} error(s); "
            f"first few: {stage_3_scan['errors'][:3]}"
        )

    # Load thesis from candidate_summaries.json
    if os.path.exists(CANDIDATE_SUMMARIES_PATH):
        try:
            with open(CANDIDATE_SUMMARIES_PATH, "r", encoding="utf-8") as f:
                summaries_doc = json.load(f)
            # candidate_summaries.json wraps the per-ticker theses under a
            # "summaries" key (alongside analysis_date / model / counts) —
            # they are not at the top level.
            per_ticker = (summaries_doc or {}).get("summaries", {})
            if isinstance(per_ticker, dict):
                for t, thesis in per_ticker.items():
                    result["thesis_by_ticker"][t] = thesis
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not load candidate_summaries.json: {e}")

    # Determine in-scope based on cadence
    if scope == "held":
        result["all_in_scope"] = list(result["held"])
    else:
        result["all_in_scope"] = list(result["held"]) + list(result["unselected"])

    return result


# ============ BASELINE REFRESH ============

def _baseline_needs_refresh() -> bool:
    """True if the baseline refresh marker is missing or older than today (UTC)."""
    if not os.path.exists(BASELINE_REFRESH_MARKER_PATH):
        return True
    try:
        with open(BASELINE_REFRESH_MARKER_PATH, "r", encoding="utf-8") as f:
            marker = f.read().strip()
        today = datetime.now(timezone.utc).date().isoformat()
        return marker != today
    except OSError:
        return True


def _write_baseline_marker():
    os.makedirs(STATE_DIR, exist_ok=True)
    today = datetime.now(timezone.utc).date().isoformat()
    with open(BASELINE_REFRESH_MARKER_PATH, "w", encoding="utf-8") as f:
        f.write(today)


async def refresh_baselines(modules: dict, tickers: list,
                              sector_by_ticker: dict) -> dict:
    """
    Refresh percentile + beta baselines for all in-scope tickers.

    Pure best-effort. Failures are logged but don't abort the run.
    Returns a summary dict for the run log.
    """
    logger.info(f"Refreshing baselines for {len(tickers)} ticker(s)...")
    summary = {
        "tickers_attempted": len(tickers),
        "percentile_succeeded": 0,
        "beta_succeeded": 0,
        "errors": [],
    }

    # Build per-ticker data inputs by fetching historical price/volume/earnings
    semaphore = asyncio.Semaphore(DATA_FETCH_CONCURRENCY)

    async def _fetch_for_ticker(ticker: str) -> dict:
        async with semaphore:
            return await asyncio.to_thread(_fetch_baseline_data_one, modules, ticker)

    tasks = [_fetch_for_ticker(t) for t in tickers]
    per_ticker_data = {}
    fetched = await asyncio.gather(*tasks, return_exceptions=True)
    for t, r in zip(tickers, fetched):
        if isinstance(r, Exception):
            summary["errors"].append(f"{t}: fetch raised {r}")
            logger.warning(f"[{t}] baseline data fetch raised: {r}")
            continue
        per_ticker_data[t] = r

    # Macro context (single fetch for all)
    macro_result = await asyncio.to_thread(modules["macroContext"].fetch_macro_context,
                                            lookback_days=BASELINE_LOOKBACK_DAYS)
    if macro_result.get("status") == "fetch_failed":
        summary["errors"].append(f"macro_context: {macro_result.get('data_quality_flags')}")
        logger.warning("Macro context fetch failed during baseline refresh")

    # Build sector bars by symbol dict for beta computation
    indicators = macro_result.get("indicators", {})
    sector_bars_by_symbol = {}
    for label, ind in indicators.items():
        closes = (ind.get("historical") or {}).get("daily_closes") or []
        sector_bars_by_symbol[label] = closes

    spy_bars = sector_bars_by_symbol.get("SPY") or []

    # Compute percentile baselines
    try:
        pct_inputs = {
            t: {
                "daily_bars": per_ticker_data[t].get("daily_bars", []),
                "daily_volumes": per_ticker_data[t].get("daily_volumes", []),
                "earnings_dates": per_ticker_data[t].get("earnings_dates", []),
            }
            for t in per_ticker_data
        }
        pct_baselines = modules["percentileBaselines"].compute_baselines_for_universe(
            pct_inputs, lookback_days=BASELINE_LOOKBACK_DAYS,
        )
        modules["percentileBaselines"].save_baselines(pct_baselines, PERCENTILE_BASELINES_PATH)
        summary["percentile_succeeded"] = pct_baselines["summary"]["tickers_with_baseline"]
        logger.info(
            f"Percentile baselines: "
            f"{summary['percentile_succeeded']}/{len(per_ticker_data)} succeeded"
        )
    except Exception as e:
        summary["errors"].append(f"percentile baselines: {e}")
        logger.exception("Percentile baseline computation failed:")

    # Compute beta baselines
    try:
        beta_inputs = {
            t: {
                "sector": sector_by_ticker.get(t),
                "daily_bars": per_ticker_data[t].get("daily_bars", []),
            }
            for t in per_ticker_data
        }
        beta_baselines = modules["betaBaselines"].compute_beta_baselines_for_universe(
            per_ticker_inputs=beta_inputs,
            market_daily_bars=spy_bars,
            sector_bars_by_symbol=sector_bars_by_symbol,
            lookback_days=BASELINE_LOOKBACK_DAYS,
        )
        modules["betaBaselines"].save_beta_baselines(beta_baselines, BETA_BASELINES_PATH)
        summary["beta_succeeded"] = beta_baselines["summary"]["tickers_with_full_beta"]
        logger.info(
            f"Beta baselines: "
            f"{summary['beta_succeeded']}/{len(per_ticker_data)} succeeded"
        )
    except Exception as e:
        summary["errors"].append(f"beta baselines: {e}")
        logger.exception("Beta baseline computation failed:")

    _write_baseline_marker()
    return summary


def _fetch_baseline_data_one(modules: dict, ticker: str) -> dict:
    """
    Fetch the historical data needed for baseline computation for one ticker.
    Synchronous (called via asyncio.to_thread).
    """
    price = modules["priceData"].fetch_price_data(ticker, lookback_days=BASELINE_LOOKBACK_DAYS)
    volume = modules["volumeData"].fetch_volume_data(ticker, lookback_days=BASELINE_LOOKBACK_DAYS)
    earnings = modules["earningsCalendar"].fetch_earnings_calendar(ticker)
    return {
        "daily_bars": price.get("historical", {}).get("daily_bars", []),
        "daily_volumes": volume.get("historical", {}).get("daily_volumes", []),
        "earnings_dates": earnings.get("historical_earnings_dates", []),
    }


# ============ LIVE DATA FETCHING ============

async def fetch_live_data(modules: dict, tickers: list) -> dict:
    """
    Fetch current price + volume per ticker and macro context.

    Returns:
        {
            "price_by_ticker": {ticker: price_data_result, ...},
            "volume_by_ticker": {ticker: volume_data_result, ...},
            "macro_context": macro_context_result,
            "fetch_errors": [str],
        }
    """
    logger.info(f"Fetching live data for {len(tickers)} ticker(s)...")
    result = {
        "price_by_ticker": {},
        "volume_by_ticker": {},
        "macro_context": None,
        "fetch_errors": [],
    }

    semaphore = asyncio.Semaphore(DATA_FETCH_CONCURRENCY)

    async def _fetch_one_ticker(t: str):
        async with semaphore:
            try:
                price = await asyncio.to_thread(
                    modules["priceData"].fetch_price_data, t, BASELINE_LOOKBACK_DAYS
                )
                volume = await asyncio.to_thread(
                    modules["volumeData"].fetch_volume_data, t, BASELINE_LOOKBACK_DAYS
                )
                return t, price, volume, None
            except Exception as e:
                return t, None, None, str(e)

    # Run all ticker fetches in parallel (bounded by semaphore)
    ticker_tasks = [_fetch_one_ticker(t) for t in tickers]

    # Macro context fetch in parallel with tickers
    macro_task = asyncio.to_thread(
        modules["macroContext"].fetch_macro_context,
        lookback_days=BASELINE_LOOKBACK_DAYS,
    )

    results = await asyncio.gather(*ticker_tasks, macro_task, return_exceptions=True)
    macro_result = results[-1]
    ticker_results = results[:-1]

    if isinstance(macro_result, Exception):
        result["fetch_errors"].append(f"macro_context: {macro_result}")
        result["macro_context"] = None
    else:
        result["macro_context"] = macro_result

    for r in ticker_results:
        if isinstance(r, Exception):
            result["fetch_errors"].append(f"ticker fetch raised: {r}")
            continue
        t, price, volume, err = r
        if err:
            result["fetch_errors"].append(f"{t}: {err}")
            continue
        result["price_by_ticker"][t] = price
        result["volume_by_ticker"][t] = volume

    logger.info(
        f"Live data: prices={len(result['price_by_ticker'])}, "
        f"volumes={len(result['volume_by_ticker'])}, "
        f"macro={'ok' if result['macro_context'] else 'failed'}, "
        f"errors={len(result['fetch_errors'])}"
    )

    return result


# ============ ANCHOR SEEDING ============

def build_effective_anchors(file_anchors: dict, held_tickers: list,
                             alpaca_positions: dict) -> dict:
    """
    Build the per-run anchor map used by the cumulative-drift signal.

    A held position only gets a cumulative-drift signal if it has an evaluation
    anchor. File anchors (ticker_evaluation_anchors.json) are written only after
    a pipeline rerun, so a freshly-built position has none and its slow-erosion
    detector is silently dormant. Here we seed a synthetic anchor for every held
    position from its real Alpaca average cost basis (scaling-adjusted).

    Precedence: a file (rerun) anchor always wins over a synthetic one, since a
    rerun is a more recent decision than the original buy. Synthetic anchors are
    in-memory only and never persisted, so they always reflect current cost
    basis. Tickers absent from Alpaca (e.g. non-US names) simply get no
    synthetic anchor and behave exactly as before.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    synthetic = {}
    for ticker in held_tickers:
        pos = alpaca_positions.get(ticker)
        if not pos:
            continue
        avg_cost = pos.get("avg_entry_price")
        if not isinstance(avg_cost, (int, float)) or avg_cost <= 0:
            continue
        synthetic[ticker] = {
            "evaluated_at": now_iso,
            "evaluated_at_price": avg_cost,
            "source": "alpaca_avg_entry_price",
        }
    return {**synthetic, **file_anchors}


# ============ SIGNAL ANALYSIS ============

async def run_signal_analyzers(modules: dict, tickers: list,
                                  live_data: dict,
                                  percentile_baselines: dict,
                                  anchors: dict) -> dict:
    """
    Run all 3 signal analyzers per ticker, then aggregate.

    Returns:
        {
            "per_ticker_signals": {ticker: {price_signal, volume_signal,
                                            cumulative_signal}, ...},
            "aggregation": signalAggregator output (with decisions, triggered_tickers),
        }
    """
    logger.info(f"Running signal analyzers for {len(tickers)} ticker(s)...")

    per_ticker_signals = {}
    semaphore = asyncio.Semaphore(SIGNAL_ANALYZER_CONCURRENCY)

    async def _analyze_one(t: str):
        async with semaphore:
            return await asyncio.to_thread(_analyze_one_sync, modules, t, live_data,
                                            percentile_baselines, anchors)

    tasks = [_analyze_one(t) for t in tickers]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for t, r in zip(tickers, results):
        if isinstance(r, Exception):
            logger.warning(f"[{t}] signal analyzer raised: {r}")
            per_ticker_signals[t] = {
                "price_signal": None,
                "volume_signal": None,
                "cumulative_signal": None,
            }
        else:
            per_ticker_signals[t] = r

    # Aggregate
    aggregation = modules["signalAggregator"].aggregate_signals_for_universe(
        per_ticker_signals
    )
    logger.info(
        f"Signal aggregation: {aggregation['summary']['tickers_triggered']}/"
        f"{aggregation['summary']['tickers_total']} triggered "
        f"(paths: {aggregation['summary']['tickers_by_path']})"
    )

    return {
        "per_ticker_signals": per_ticker_signals,
        "aggregation": aggregation,
    }


def _analyze_one_sync(modules: dict, ticker: str, live_data: dict,
                       percentile_baselines: dict, anchors: dict) -> dict:
    """Run all three signal analyzers for one ticker (sync, called via to_thread)."""
    price_data = live_data["price_by_ticker"].get(ticker)
    volume_data = live_data["volume_by_ticker"].get(ticker)
    ticker_baseline = (percentile_baselines.get("tickers") or {}).get(ticker)

    # Price signal
    price_signal = modules["priceSignalAnalyzer"].analyze_price_signal(
        ticker=ticker, price_data=price_data, baseline=ticker_baseline,
    )

    # Volume signal
    volume_signal = modules["volumeSignalAnalyzer"].analyze_volume_signal(
        ticker=ticker, volume_data=volume_data, baseline=ticker_baseline,
    )

    # Cumulative signal
    current_price = None
    if price_data:
        current_price = (price_data.get("current") or {}).get("price")
    anchor = anchors.get(ticker)
    cumulative_signal = modules["cumulativeSignalAnalyzer"].analyze_cumulative_signal(
        ticker=ticker,
        current_price=current_price,
        anchor=anchor,
        baseline=ticker_baseline,
    )

    return {
        "price_signal": price_signal,
        "volume_signal": volume_signal,
        "cumulative_signal": cumulative_signal,
    }


# ============ LLM GATES ============

async def run_llm_gates(modules: dict, gate0_decisions: dict,
                         portfolio: dict, anchors: dict,
                         macro_context: dict, cadence_window: dict,
                         beta_baselines: dict,
                         skip_llm: bool = False) -> dict:
    """
    For each ticker marked 'investigate' by Gate 0, run Call 1 + Call 2.

    Returns:
        {
            "per_ticker_results": {ticker: {call_one, call_two, rerun_decision}, ...},
            "summary": {...},
        }
    """
    to_investigate = [
        t for t, decision in gate0_decisions.items()
        if decision.get("action") == "investigate"
    ]
    logger.info(f"LLM gates: {len(to_investigate)} ticker(s) to investigate "
                f"(skip_llm={skip_llm})")

    per_ticker_results = {}
    summary = {
        "tickers_to_investigate": len(to_investigate),
        "call_one_succeeded": 0,
        "call_one_failed": 0,
        "call_two_succeeded": 0,
        "call_two_failed": 0,
        "rerun_yes": 0,
        "rerun_no": 0,
        "skipped_no_llm": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
    }

    if skip_llm:
        for t in to_investigate:
            per_ticker_results[t] = {
                "call_one": None,
                "call_two": None,
                "rerun_decision": False,
                "skipped_reason": "skip_llm flag",
            }
            summary["skipped_no_llm"] += 1
        return {"per_ticker_results": per_ticker_results, "summary": summary}

    semaphore = asyncio.Semaphore(LLM_CONCURRENCY)

    async def _process_one(t: str):
        async with semaphore:
            return await _run_call_one_and_two(
                t, modules, portfolio, anchors, macro_context,
                cadence_window, beta_baselines,
            )

    tasks = [_process_one(t) for t in to_investigate]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for t, r in zip(to_investigate, results):
        if isinstance(r, Exception):
            logger.exception(f"[{t}] LLM gate processing raised:")
            per_ticker_results[t] = {
                "call_one": None,
                "call_two": None,
                "rerun_decision": False,
                "skipped_reason": f"exception: {r}",
            }
            summary["call_one_failed"] += 1
            continue

        per_ticker_results[t] = r

        c1 = r.get("call_one")
        c2 = r.get("call_two")
        if c1 and c1.get("status") == "ok":
            summary["call_one_succeeded"] += 1
        else:
            summary["call_one_failed"] += 1

        if c2 and c2.get("status") == "ok":
            summary["call_two_succeeded"] += 1
        else:
            summary["call_two_failed"] += 1

        if r.get("rerun_decision"):
            summary["rerun_yes"] += 1
        else:
            summary["rerun_no"] += 1

        for c in (c1, c2):
            if c and c.get("token_usage"):
                in_tok = c["token_usage"].get("input_tokens") or 0
                out_tok = c["token_usage"].get("output_tokens") or 0
                summary["total_input_tokens"] += in_tok
                summary["total_output_tokens"] += out_tok

    logger.info(
        f"LLM gates done: rerun_yes={summary['rerun_yes']}, "
        f"rerun_no={summary['rerun_no']}, "
        f"tokens={summary['total_input_tokens']}in/{summary['total_output_tokens']}out"
    )

    return {"per_ticker_results": per_ticker_results, "summary": summary}


async def _run_call_one_and_two(ticker: str, modules: dict, portfolio: dict,
                                  anchors: dict, macro_context: dict,
                                  cadence_window: dict,
                                  beta_baselines: dict) -> dict:
    """Run Call 1 then Call 2 for one ticker."""
    thesis = portfolio["thesis_by_ticker"].get(ticker, {})
    anchor = anchors.get(ticker, {})

    # Look up sector ETF from beta baselines
    sector_etf_symbol = None
    ticker_beta = (beta_baselines.get("tickers") or {}).get(ticker, {})
    if ticker_beta:
        sector_etf_symbol = ticker_beta.get("sector_etf_symbol")

    # Build Call 1 packet (also needs the signal decision — pulled from
    # the cached aggregation result; for now we pass empty since the
    # orchestrator-level state has it)
    # NOTE: this function expects the orchestrator to wire in the signal
    # decision via a closure-level dict. For this implementation, we use a
    # module-level pseudo-state below.

    signal_decision = _SIGNAL_DECISIONS_BY_TICKER.get(ticker, {})

    packet_one = modules["callOneInvestigator"].build_investigation_packet(
        ticker=ticker,
        thesis=thesis,
        signal_decision=signal_decision,
        macro_context=macro_context,
        cadence_window=cadence_window.get("name"),
        anchor=anchor,
        sector_etf_symbol=sector_etf_symbol,
    )

    call_one_result = await modules["callOneInvestigator"].investigate_ticker(packet_one)
    logger.info(
        f"[{ticker}] Call 1 status={call_one_result.get('status')}, "
        f"searches={call_one_result.get('web_search_calls_made')}, "
        f"tokens={call_one_result.get('token_usage', {}).get('total_tokens')}"
    )
    _c1_inv = call_one_result.get("investigation") or {}
    if _c1_inv:
        _c1_summary = (_c1_inv.get("investigation_summary") or "").replace("\n", " ")
        logger.info(
            f"[{ticker}] Call 1 investigation "
            f"(confidence={_c1_inv.get('investigation_confidence')}): {_c1_summary[:500]}"
        )
    elif call_one_result.get("status") != "ok":
        logger.info(
            f"[{ticker}] Call 1 produced no parsed investigation "
            f"(status={call_one_result.get('status')}, error={call_one_result.get('error')})"
        )

    # Build Call 2 packet
    packet_two = modules["callTwoDecider"].build_decision_packet(
        ticker=ticker,
        investigation_result=call_one_result,
        thesis=thesis,
        signal_decision=signal_decision,
        anchor=anchor,
    )

    call_two_result = await modules["callTwoDecider"].decide_rerun(packet_two)
    logger.info(
        f"[{ticker}] Call 2 status={call_two_result.get('status')}, "
        f"rerun={call_two_result.get('decision', {}).get('rerun_decision') if call_two_result.get('decision') else 'n/a'}"
    )
    _c2_dec = call_two_result.get("decision") or {}
    if _c2_dec:
        _c2_rationale = (_c2_dec.get("rationale") or "").replace("\n", " ")
        logger.info(
            f"[{ticker}] Call 2 decision "
            f"(strength={_c2_dec.get('evidence_strength')}): {_c2_rationale[:500]}"
        )
    elif call_two_result.get("status") != "ok":
        logger.info(
            f"[{ticker}] Call 2 produced no parsed decision "
            f"(status={call_two_result.get('status')}, error={call_two_result.get('error')})"
        )

    rerun = modules["callTwoDecider"].orchestrator_should_rerun(call_two_result)

    # Persist evidence packet
    try:
        os.makedirs(EVIDENCE_DIR, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        evidence_path = os.path.join(EVIDENCE_DIR, f"{ticker}_{ts}.json")
        _atomic_write_json(evidence_path, {
            "ticker": ticker,
            "evidence_written_at": datetime.now(timezone.utc).isoformat(),
            "call_one_packet": packet_one,
            "call_one_result": call_one_result,
            "call_two_packet": packet_two,
            "call_two_result": call_two_result,
            "final_rerun_decision": rerun,
        })
    except Exception as e:
        logger.warning(f"[{ticker}] Could not write evidence packet: {e}")

    return {
        "call_one": call_one_result,
        "call_two": call_two_result,
        "rerun_decision": rerun,
    }


# Module-level pseudo-state used by _run_call_one_and_two. Populated by the
# orchestrator before LLM gates run, cleared after.
_SIGNAL_DECISIONS_BY_TICKER: dict = {}


# ============ PIPELINE INVOCATION ============

def _load_pipeline_invoker():
    """Dynamically import invokePipeline.py and return its module object."""
    spec = importlib.util.spec_from_file_location("invokePipeline", PIPELINE_INVOKER_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not create import spec for {PIPELINE_INVOKER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def invoke_pipeline_for_tickers(tickers: list, dry_run: bool = False) -> dict:
    """
    Invoke the pipeline for a list of tickers.

    invokePipeline.py is a library module (no CLI), so we import it and await
    its invoke_pipeline() coroutine directly rather than shelling out.

    Returns:
        {
            "status": "ok" | "locked" | "failed" | "dry_run",
            "result": invoker_output or None,
            "error": str or None,
        }
    """
    if not tickers:
        return {"status": "ok", "result": {"empty": True}, "error": None}

    if dry_run:
        logger.info(f"DRY-RUN: would invoke pipeline for tickers={tickers}")
        return {"status": "dry_run", "result": None, "error": None}

    logger.info(f"Invoking pipeline for {len(tickers)} ticker(s): {tickers}")

    try:
        invoker = _load_pipeline_invoker()
    except Exception as e:
        logger.exception("Could not load pipeline invoker:")
        return {"status": "failed", "result": None,
                "error": f"invoker import failed: {e}"}

    try:
        invoker_result = await invoker.invoke_pipeline(tickers)
    except Exception as e:
        logger.exception("Pipeline invocation raised:")
        return {"status": "failed", "result": None, "error": str(e)}

    # Map the invoker's status vocabulary onto the monitor's.
    invoker_status = invoker_result.get("status")
    flags = "; ".join(invoker_result.get("data_quality_flags") or [])

    if invoker_status == "success":
        logger.info("Pipeline invocation succeeded")
        return {"status": "ok", "result": invoker_result, "error": None}
    elif invoker_status == "rejected_lock_held":
        logger.warning("Pipeline invoker reports locked")
        return {"status": "locked", "result": invoker_result, "error": flags or None}
    else:
        # failed, rejected_invalid_input, unknown, etc.
        err = flags or f"invoker status={invoker_status}"
        logger.error(f"Pipeline invocation failed: {err}")
        return {"status": "failed", "result": invoker_result, "error": err}


# ============ PENDING RERUNS QUEUE ============

def load_pending_reruns() -> list:
    """Load the pending reruns queue. Returns empty list if missing."""
    if not os.path.exists(PENDING_RERUNS_PATH):
        return []
    try:
        with open(PENDING_RERUNS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("pending"), list):
            return data["pending"]
        return []
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not load pending reruns: {e}")
        return []


def save_pending_reruns(pending: list) -> None:
    """Save the pending reruns queue. Atomic."""
    _atomic_write_json(PENDING_RERUNS_PATH, {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "pending": pending,
    })


def queue_reruns(tickers: list, run_id: str) -> int:
    """Add tickers to the pending reruns queue. Soft-capped."""
    if not tickers:
        return 0
    pending = load_pending_reruns()
    existing_tickers = {p.get("ticker") for p in pending}
    added = 0
    for t in tickers:
        if t in existing_tickers:
            continue
        pending.append({
            "ticker": t,
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "queued_by_run": run_id,
        })
        added += 1
    if len(pending) > PENDING_RERUNS_SOFT_CAP:
        logger.warning(
            f"Pending reruns queue exceeds soft cap ({len(pending)} > {PENDING_RERUNS_SOFT_CAP}). "
            f"Investigate why reruns aren't draining."
        )
    save_pending_reruns(pending)
    logger.info(f"Queued {added} ticker(s) to pending reruns; total in queue: {len(pending)}")
    return added


async def drain_pending_reruns(dry_run: bool = False) -> dict:
    """
    Try to invoke the pipeline for any pending reruns. Returns a summary.
    On success, the queue is cleared. On lock or failure, queue is preserved.
    """
    pending = load_pending_reruns()
    if not pending:
        return {"drained": 0, "remaining": 0, "status": "empty"}

    tickers = [p["ticker"] for p in pending if "ticker" in p]
    logger.info(f"Attempting to drain {len(tickers)} pending reruns")

    invoke_result = await invoke_pipeline_for_tickers(tickers, dry_run=dry_run)
    status = invoke_result["status"]

    if status == "ok" or status == "dry_run":
        save_pending_reruns([])
        return {"drained": len(tickers), "remaining": 0, "status": status}
    else:
        # locked or failed — keep the queue
        return {"drained": 0, "remaining": len(tickers), "status": status,
                "error": invoke_result.get("error")}


# ============ OBSERVATION LOGGING ============

def log_observations(run_id: str, cadence_window: dict, tickers: list,
                      signal_results: dict, gate0_results: dict,
                      llm_results: dict) -> None:
    """
    Append per-ticker observations to the JSONL file. One line per ticker.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    per_ticker_signals = signal_results.get("per_ticker_signals", {})
    aggregation = signal_results.get("aggregation", {})
    decisions = aggregation.get("decisions", {})
    gate0_decisions = gate0_results.get("decisions", {})
    llm_per_ticker = llm_results.get("per_ticker_results", {})

    for ticker in tickers:
        obs = {
            "ticker": ticker,
            "run_id": run_id,
            "observed_at": now_iso,
            "cadence_window": cadence_window.get("name"),
            "cadence_actual_et": cadence_window.get("actual_time_et"),
            "signals": per_ticker_signals.get(ticker, {}),
            "aggregation_decision": decisions.get(ticker, {}),
            "gate_0_decision": gate0_decisions.get(ticker),
            "llm_result": llm_per_ticker.get(ticker),
        }
        _append_jsonl(OBSERVATIONS_PATH, obs)


# ============ MAIN ORCHESTRATOR ============

async def run_monitor(
    dry_run: bool = False,
    skip_llm: bool = False,
    cadence_override: Optional[str] = None,
    tickers_override: Optional[list] = None,
) -> dict:
    """
    Main per-run orchestration function.

    Args:
        dry_run: do everything except actually invoke the pipeline / queue reruns
        skip_llm: skip Call 1 + Call 2 (still does mechanical work)
        cadence_override: force a cadence window
        tickers_override: limit to specific tickers (for testing)

    Returns: a structured run result dict.
    """
    run_started_at = datetime.now(timezone.utc)
    run_id = run_started_at.strftime("%Y%m%dT%H%M%S")

    _setup_logging(run_id)

    logger.info("=" * 60)
    logger.info(f"Stage 5 Monitor run starting (run_id={run_id})")
    logger.info(f"  dry_run={dry_run}, skip_llm={skip_llm}")
    logger.info(f"  cadence_override={cadence_override}")
    logger.info("=" * 60)

    result = {
        "run_id": run_id,
        "started_at": run_started_at.isoformat(),
        "dry_run": dry_run,
        "skip_llm": skip_llm,
        "status": "unknown",
        "cadence_window": None,
        "tickers_in_scope": [],
        "summary": {},
        "errors": [],
    }

    try:
        # Determine cadence
        cadence = determine_cadence_window(now=run_started_at, override=cadence_override)
        result["cadence_window"] = cadence
        logger.info(
            f"Cadence: {cadence['name']} (scope={cadence['scope']}, "
            f"matched={cadence['matched']}, actual_et={cadence['actual_time_et']})"
        )

        # Import modules
        modules = _import_modules()

        # Load portfolio
        portfolio = load_portfolio_tickers(scope=cadence["scope"])
        tickers_in_scope = list(portfolio["all_in_scope"])
        if tickers_override:
            tickers_in_scope = [t for t in tickers_override if t in tickers_in_scope or True]
            # If user specifies tickers not in portfolio, still allow (for testing)
            tickers_in_scope = list(tickers_override)
        result["tickers_in_scope"] = tickers_in_scope
        logger.info(
            f"Universe: {len(tickers_in_scope)} ticker(s) "
            f"(held={len(portfolio['held'])}, unselected={len(portfolio['unselected'])})"
        )

        if not tickers_in_scope:
            result["status"] = "ok"
            result["summary"] = {"message": "No tickers in scope"}
            logger.info("No tickers in scope; exiting cleanly.")
            return result

        # Step 1: drain pending reruns FIRST
        drain_result = await drain_pending_reruns(dry_run=dry_run)
        logger.info(f"Pending reruns drain: {drain_result}")
        result["pending_drain"] = drain_result

        # Step 2: baseline refresh if needed
        if _baseline_needs_refresh():
            baseline_summary = await refresh_baselines(
                modules, tickers_in_scope,
                portfolio["sector_by_ticker"],
            )
            result["baseline_refresh"] = baseline_summary
        else:
            logger.info("Baselines already fresh for today; skipping refresh.")
            result["baseline_refresh"] = {"skipped": "already_fresh_today"}

        # Step 3: load baselines + anchors
        percentile_baselines = modules["percentileBaselines"].load_baselines(
            PERCENTILE_BASELINES_PATH
        ) or {"tickers": {}}
        beta_baselines = modules["betaBaselines"].load_beta_baselines(
            BETA_BASELINES_PATH
        ) or {"tickers": {}}
        anchors = modules["cumulativeSignalAnalyzer"].load_anchors(ANCHORS_PATH)
        logger.info(
            f"Loaded baselines: pct={len(percentile_baselines.get('tickers', {}))}, "
            f"beta={len(beta_baselines.get('tickers', {}))}, anchors={len(anchors)}"
        )

        # Seed cumulative-drift anchors for held positions from real Alpaca
        # cost basis. File (rerun) anchors take precedence; tickers absent from
        # Alpaca keep current behavior. An Alpaca failure degrades gracefully
        # to file-only anchors.
        alpaca = modules["alpacaPositions"].fetch_positions()
        effective_anchors = build_effective_anchors(
            anchors, portfolio["held"], alpaca.get("positions") or {}
        )
        logger.info(
            f"Alpaca positions: {len(alpaca.get('positions') or {})} fetched "
            f"(status={alpaca.get('status')}); seeded "
            f"{len(effective_anchors) - len(anchors)} held-position anchor(s) "
            f"not already covered by file anchors"
        )

        # Step 4: fetch live data
        live_data = await fetch_live_data(modules, tickers_in_scope)
        result["fetch_errors"] = (
            live_data.get("fetch_errors", []) + alpaca.get("errors", [])
        )

        # Step 5: signal analysis
        signal_results = await run_signal_analyzers(
            modules, tickers_in_scope, live_data, percentile_baselines,
            effective_anchors,
        )
        result["signal_results"] = signal_results
        for _t, _d in signal_results.get("aggregation", {}).get("decisions", {}).items():
            if _d.get("triggered"):
                logger.info(f"  Signal [{_t}] TRIGGERED: {json.dumps(_d, default=str)[:400]}")

        # Step 6: Gate 0 mechanical filter
        triggered_decisions = {
            t: d for t, d in signal_results["aggregation"]["decisions"].items()
            if d.get("triggered")
        }
        gate0_results = modules["gate0Filter"].apply_gate_0(
            triggered_decisions=triggered_decisions,
            beta_baselines=beta_baselines,
            macro_context=live_data["macro_context"] or {"indicators": {}},
        )
        logger.info(
            f"Gate 0: {gate0_results['summary']['tickers_skipped']} skipped, "
            f"{gate0_results['summary']['tickers_to_investigate']} to investigate "
            f"({gate0_results['summary']['tickers_bypassed_to_investigate']} bypassed)"
        )
        result["gate0_results"] = gate0_results
        for _t, _d in gate0_results.get("decisions", {}).items():
            logger.info(f"  Gate 0 [{_t}]: {json.dumps(_d, default=str)[:400]}")

        # Step 7: Call 1 + Call 2 — wire the signal decisions through module-level state
        global _SIGNAL_DECISIONS_BY_TICKER
        _SIGNAL_DECISIONS_BY_TICKER = triggered_decisions

        try:
            llm_results = await run_llm_gates(
                modules=modules,
                gate0_decisions=gate0_results["decisions"],
                portfolio=portfolio,
                anchors=effective_anchors,
                macro_context=live_data["macro_context"] or {},
                cadence_window=cadence,
                beta_baselines=beta_baselines,
                skip_llm=skip_llm,
            )
        finally:
            _SIGNAL_DECISIONS_BY_TICKER = {}

        result["llm_results"] = llm_results

        # Step 8: build rerun list
        rerun_tickers = [
            t for t, r in llm_results["per_ticker_results"].items()
            if r.get("rerun_decision")
        ]
        logger.info(f"Rerun list: {len(rerun_tickers)} ticker(s) {rerun_tickers}")

        # Step 9: invoke pipeline OR queue
        if rerun_tickers:
            invoke_result = await invoke_pipeline_for_tickers(rerun_tickers, dry_run=dry_run)
            result["pipeline_invocation"] = invoke_result

            if invoke_result["status"] == "locked":
                if not dry_run:
                    queue_reruns(rerun_tickers, run_id)
            elif invoke_result["status"] == "failed":
                logger.warning(
                    f"Pipeline invocation failed; "
                    f"{'NOT queueing (dry-run)' if dry_run else 'queueing for retry'}"
                )
                if not dry_run:
                    queue_reruns(rerun_tickers, run_id)
        else:
            result["pipeline_invocation"] = {"status": "no_reruns"}

        # Step 10: write observation log — always, even on a dry run. This is
        # pure record-keeping; inspecting it is exactly the point of a dry run.
        log_observations(run_id, cadence, tickers_in_scope,
                          signal_results, gate0_results, llm_results)

        # Build summary
        result["summary"] = {
            "tickers_in_scope": len(tickers_in_scope),
            "tickers_triggered_by_signals": signal_results["aggregation"]["summary"]["tickers_triggered"],
            "gate0_skipped": gate0_results["summary"]["tickers_skipped"],
            "gate0_to_investigate": gate0_results["summary"]["tickers_to_investigate"],
            "llm_call_one_succeeded": llm_results["summary"]["call_one_succeeded"],
            "llm_call_two_succeeded": llm_results["summary"]["call_two_succeeded"],
            "tickers_to_rerun": len(rerun_tickers),
            "rerun_tickers": rerun_tickers,
            "total_llm_input_tokens": llm_results["summary"]["total_input_tokens"],
            "total_llm_output_tokens": llm_results["summary"]["total_output_tokens"],
        }

        result["status"] = "ok"

    except Exception as e:
        logger.exception("Monitor run raised:")
        result["status"] = "failed"
        result["errors"].append(f"{type(e).__name__}: {e}")
        result["traceback"] = traceback.format_exc()

    finally:
        run_ended_at = datetime.now(timezone.utc)
        result["ended_at"] = run_ended_at.isoformat()
        result["duration_seconds"] = (run_ended_at - run_started_at).total_seconds()

        # Write run output file
        try:
            os.makedirs(RUN_OUTPUT_DIR, exist_ok=True)
            run_output_path = os.path.join(RUN_OUTPUT_DIR, f"monitor_run_{run_id}.json")
            _atomic_write_json(run_output_path, result)
            logger.info(f"Run output: {run_output_path}")
        except Exception as e:
            logger.warning(f"Could not write run output file: {e}")

        logger.info("=" * 60)
        logger.info(
            f"Monitor run done: status={result['status']}, "
            f"duration={result['duration_seconds']:.1f}s"
        )
        if "summary" in result and isinstance(result["summary"], dict):
            logger.info(f"  Summary: {result['summary']}")
        logger.info("=" * 60)

    return result


# ============ CLI ============

def _parse_cli_args():
    parser = argparse.ArgumentParser(description="Stage 5 Monitor — orchestrator")
    parser.add_argument("--dry-run", action="store_true",
                        help="Do everything except actually invoke the pipeline")
    parser.add_argument("--skip-llm", action="store_true",
                        help="Skip Call 1 + Call 2 LLM gates")
    parser.add_argument("--cadence", default=None,
                        help=f"Force cadence window: {list(CADENCE_WINDOWS.keys())}")
    parser.add_argument("--tickers", nargs="*", default=None,
                        help="Subset of tickers to monitor (for testing)")
    return parser.parse_args()


def main():
    args = _parse_cli_args()
    result = asyncio.run(run_monitor(
        dry_run=args.dry_run,
        skip_llm=args.skip_llm,
        cadence_override=args.cadence,
        tickers_override=args.tickers,
    ))
    print(f"\nMonitor run complete: status={result['status']}, "
          f"duration={result.get('duration_seconds', 0):.1f}s")
    if result.get("summary"):
        print(f"Summary: {json.dumps(result['summary'], indent=2)}")
    sys.exit(0 if result["status"] == "ok" else 1)


if __name__ == "__main__":
    main()