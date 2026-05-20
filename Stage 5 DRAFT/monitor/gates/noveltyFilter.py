"""
Novelty filter — suppress repeat LLM spend on persistent signals.

Without this filter, a ticker whose underlying move/news persists across
cadences (pre_open, post_open, pre_close, post_close) will re-trigger the
full Gate 0 + Call 1 + Call 2 chain at each cadence and burn LLM budget
on essentially the same situation, four times per day. Gate 0 itself is
stateless and the LLM gates have no memory of prior verdicts.

The filter sits between Gate 0 and the LLM gates in runMonitor.py. For each
ticker Gate 0 said 'investigate':

  1. Fetch fresh Alpaca news headlines for the ticker.
  2. If a prior 'no rerun' Call 2 verdict exists for the same ticker on the
     current ET trading day, compare current news IDs against the IDs
     captured at the time of that prior verdict.
  3. If no IDs are new (current - prior == empty), suppress: the ticker's
     Gate 0 decision is flipped to 'skip' and the LLM gates are skipped.
  4. Otherwise (or if no prior verdict), proceed to the LLM gates.
  5. After Call 2 completes, if it returned 'no rerun' AND status == ok,
     persist the verdict together with the news snapshot for use by
     subsequent cadences.

Cache file: monitor/state/llm_verdict_cache.json. On load, if its stored
trading_day_et doesn't match today's ET date, the verdicts dict is wiped.

Fail-open: any Alpaca fetch failure causes that ticker to fall through to
the LLM gates (do not silently suppress on infra failure).

Only 'no rerun' verdicts are cached — a 'rerun' verdict triggers the
pipeline invoker, which updates anchors and refreshes the entire thesis;
nothing for the novelty filter to do with it.
"""

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


# ============ PATHS ============

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MONITOR_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
STATE_DIR = os.path.join(MONITOR_ROOT, "state")
CACHE_PATH = os.path.join(STATE_DIR, "llm_verdict_cache.json")


# ============ CONFIG ============

FETCH_LOOKBACK_HOURS = 24
NEWS_FETCH_CONCURRENCY = 3


# ============ HELPERS ============

def _current_et_trading_day() -> str:
    """Return 'YYYY-MM-DD' for now() in America/New_York. Mirrors the
    cadence-window logic in runMonitor.determine_cadence_window."""
    now_utc = datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        et_now = now_utc.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        et_now = now_utc.astimezone(timezone(timedelta(hours=-5)))
    return et_now.strftime("%Y-%m-%d")


def _empty_cache() -> dict:
    return {
        "trading_day_et": _current_et_trading_day(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "verdicts": {},
    }


def _parse_iso(value):
    """Best-effort ISO parser. Returns tz-aware UTC datetime or None."""
    if not value:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if not isinstance(value, str):
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


# ============ CACHE LOAD / SAVE ============

def load_verdict_cache() -> dict:
    """
    Read CACHE_PATH. If the file is missing, unreadable, or its
    trading_day_et doesn't match today's ET trading day, return a fresh
    empty cache dict. Does NOT persist the reset to disk — that happens
    lazily when something is actually written.
    """
    today_et = _current_et_trading_day()

    if not os.path.exists(CACHE_PATH):
        return _empty_cache()

    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"Could not read {CACHE_PATH}: {e}; treating as empty")
        return _empty_cache()

    if not isinstance(data, dict):
        return _empty_cache()

    if data.get("trading_day_et") != today_et:
        # ET trading day has rolled over — wipe prior verdicts
        return _empty_cache()

    if not isinstance(data.get("verdicts"), dict):
        data["verdicts"] = {}

    return data


def save_verdict_cache(cache: dict) -> None:
    """Atomic temp+rename, mirroring runMonitor._atomic_write_json."""
    os.makedirs(STATE_DIR, exist_ok=True)
    cache["updated_at"] = datetime.now(timezone.utc).isoformat()
    fd, tmp = tempfile.mkstemp(prefix=".llm_verdict_cache.", suffix=".tmp",
                               dir=STATE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp, CACHE_PATH)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def store_no_rerun_verdict(
    *,
    ticker: str,
    call_two_result: dict,
    news_snapshot: dict,
    cadence_window: str,
    run_id: str,
    signal_decision: dict,
) -> None:
    """
    Persist a per-ticker no-rerun verdict together with the Alpaca news
    snapshot captured at novelty-check time. Guards on status/decision so
    we never cache pseudo-no-rerun verdicts (e.g., from an LLM failure) —
    those would suppress LLM calls for the rest of the day on a transient
    blip.
    """
    if not isinstance(call_two_result, dict):
        return
    if call_two_result.get("status") != "ok":
        return
    decision = call_two_result.get("decision") or {}
    if decision.get("rerun_decision") is not False:
        return
    if not isinstance(news_snapshot, dict):
        return
    if news_snapshot.get("status") != "ok":
        # A verdict resting on a failed news fetch would suppress forever
        # under the novelty filter — don't cache it.
        return

    cache = load_verdict_cache()  # already day-rolled

    items = news_snapshot.get("items") or []
    newest_published = None
    if items:
        newest_published = items[0].get("published_at")

    fired_signal_types = []
    actual_move_pct = None
    try:
        fired = (signal_decision or {}).get("fired_signals") or []
        fired_signal_types = sorted({
            (s.get("signal_type") or s.get("type") or "unknown")
            for s in fired if isinstance(s, dict)
        })
        summary = (signal_decision or {}).get("signal_summary") or {}
        actual_move_pct = (
            summary.get("actual_move_pct")
            or summary.get("daily_move_pct")
        )
    except Exception:
        pass

    cache["verdicts"][ticker] = {
        "ticker": ticker,
        "stored_at": datetime.now(timezone.utc).isoformat(),
        "cadence_window": cadence_window,
        "run_id": run_id,
        "rerun_decision": False,
        "evidence_strength": decision.get("evidence_strength"),
        "alpaca_news": {
            "fetched_at": news_snapshot.get("fetched_at"),
            "since_utc": news_snapshot.get("since_utc"),
            "ids": list(news_snapshot.get("ids") or []),
            "newest_published_at": newest_published,
            "status": news_snapshot.get("status"),
        },
        "signal_summary": {
            "fired_signal_types": fired_signal_types,
            "actual_move_pct": actual_move_pct,
        },
    }
    save_verdict_cache(cache)


# ============ NOVELTY FILTER ============

def _per_ticker_record(decision, **fields) -> dict:
    base = {
        "decision": decision,
        "prior_verdict_run_id": None,
        "prior_verdict_cadence": None,
        "prior_news_ids": [],
        "current_news_ids": [],
        "new_ids": [],
        "alpaca_fetch_status": None,
        "alpaca_fetch_error": None,
        "since_utc": None,
        "news_snapshot": None,
    }
    base.update(fields)
    return base


async def apply_novelty_filter(
    *,
    investigate_tickers: list,
    alpaca_news_module,
    now_utc: datetime,
) -> dict:
    """
    Evaluate the novelty filter for each ticker in investigate_tickers.

    Returns a dict with per-ticker decisions and a summary:

        {
          "evaluated_at": ISO,
          "trading_day_et": "YYYY-MM-DD",
          "per_ticker": {
              ticker: {
                  "decision": "novel"|"suppressed"|"no_prior_verdict"|"fetch_failed",
                  "prior_verdict_run_id": str|None,
                  "prior_verdict_cadence": str|None,
                  "prior_news_ids": [str],
                  "current_news_ids": [str],
                  "new_ids": [str],
                  "alpaca_fetch_status": "ok"|"fetch_failed"|None,
                  "alpaca_fetch_error": str|None,
                  "since_utc": ISO|None,
                  "news_snapshot": <alpacaNews result> | None,
              }, ...
          },
          "summary": {
              "tickers_evaluated": int,
              "suppressed": int,
              "proceeded_novel": int,
              "proceeded_no_prior": int,
              "proceeded_fetch_failed": int,
          }
        }
    """
    cache = load_verdict_cache()
    today_et = cache["trading_day_et"]
    sem = asyncio.Semaphore(NEWS_FETCH_CONCURRENCY)

    async def _eval_one(ticker: str) -> tuple:
        prior = cache["verdicts"].get(ticker)
        # If the prior verdict was stored with a failed news fetch, treat it
        # as no_prior_verdict so we don't suppress on a stale empty snapshot.
        if prior and (prior.get("alpaca_news") or {}).get("status") != "ok":
            prior = None

        since = now_utc - timedelta(hours=FETCH_LOOKBACK_HOURS)
        if prior:
            prior_fetched = _parse_iso((prior.get("alpaca_news") or {}).get("fetched_at"))
            if prior_fetched and prior_fetched > since:
                since = prior_fetched

        async with sem:
            try:
                snapshot = await asyncio.to_thread(
                    alpaca_news_module.fetch_news_for_ticker,
                    ticker,
                    since,
                    now_utc,
                )
            except Exception as e:
                logger.warning(f"[{ticker}] novelty filter: news fetch raised: {e}")
                return ticker, _per_ticker_record(
                    "fetch_failed",
                    alpaca_fetch_status="fetch_failed",
                    alpaca_fetch_error=str(e),
                    since_utc=since.isoformat(),
                )

        if not isinstance(snapshot, dict) or snapshot.get("status") != "ok":
            err = None
            if isinstance(snapshot, dict):
                errs = snapshot.get("errors") or []
                err = errs[0] if errs else None
            return ticker, _per_ticker_record(
                "fetch_failed",
                alpaca_fetch_status=(snapshot or {}).get("status", "fetch_failed"),
                alpaca_fetch_error=err,
                since_utc=since.isoformat(),
                news_snapshot=snapshot if isinstance(snapshot, dict) else None,
            )

        current_ids = list(snapshot.get("ids") or [])

        if prior is None:
            return ticker, _per_ticker_record(
                "no_prior_verdict",
                current_news_ids=current_ids,
                alpaca_fetch_status="ok",
                since_utc=since.isoformat(),
                news_snapshot=snapshot,
            )

        prior_ids_list = list((prior.get("alpaca_news") or {}).get("ids") or [])
        prior_ids = set(prior_ids_list)
        new_ids = [i for i in current_ids if i not in prior_ids]

        if not new_ids:
            return ticker, _per_ticker_record(
                "suppressed",
                prior_verdict_run_id=prior.get("run_id"),
                prior_verdict_cadence=prior.get("cadence_window"),
                prior_news_ids=prior_ids_list,
                current_news_ids=current_ids,
                new_ids=[],
                alpaca_fetch_status="ok",
                since_utc=since.isoformat(),
                news_snapshot=snapshot,
            )

        return ticker, _per_ticker_record(
            "novel",
            prior_verdict_run_id=prior.get("run_id"),
            prior_verdict_cadence=prior.get("cadence_window"),
            prior_news_ids=prior_ids_list,
            current_news_ids=current_ids,
            new_ids=new_ids,
            alpaca_fetch_status="ok",
            since_utc=since.isoformat(),
            news_snapshot=snapshot,
        )

    tasks = [_eval_one(t) for t in investigate_tickers]
    pairs = await asyncio.gather(*tasks, return_exceptions=False)

    per_ticker = {t: rec for t, rec in pairs}

    summary = {
        "tickers_evaluated": len(per_ticker),
        "suppressed": 0,
        "proceeded_novel": 0,
        "proceeded_no_prior": 0,
        "proceeded_fetch_failed": 0,
    }
    for rec in per_ticker.values():
        d = rec["decision"]
        if d == "suppressed":
            summary["suppressed"] += 1
        elif d == "novel":
            summary["proceeded_novel"] += 1
        elif d == "no_prior_verdict":
            summary["proceeded_no_prior"] += 1
        elif d == "fetch_failed":
            summary["proceeded_fetch_failed"] += 1

    return {
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "trading_day_et": today_et,
        "per_ticker": per_ticker,
        "summary": summary,
    }


# ============ CLI ============

def _cli():
    import argparse
    parser = argparse.ArgumentParser(description="Inspect the LLM verdict cache.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--show", action="store_true",
                       help="Print the current cache contents.")
    group.add_argument("--clear", action="store_true",
                       help="Wipe the cache (next monitor run starts fresh).")
    args = parser.parse_args()

    if args.show:
        cache = load_verdict_cache()
        print(json.dumps(cache, indent=2, default=str))
    elif args.clear:
        save_verdict_cache(_empty_cache())
        print(f"Cleared {CACHE_PATH}")


if __name__ == "__main__":
    _cli()
