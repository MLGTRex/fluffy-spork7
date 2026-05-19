"""
Alpaca news fetcher — recent news headlines per ticker for the Stage 5 monitor.

Fetches Alpaca's news feed for a given ticker, exposing each article's stable
Alpaca-assigned ID (used downstream for novelty comparison):
  - id              : Alpaca news article id, coerced to str
  - published_at    : ISO timestamp
  - headline        : article headline
  - url             : canonical article URL (if provided)

Used for:
  - The novelty filter (Stage 5 monitor/gates/noveltyFilter.py): compares the
    headline ID set across cadences on the same ET trading day so we don't
    re-spend LLM budget on identical situations. The fact that Alpaca IDs are
    stable per article is what makes the dedup robust (URL/headline-text would
    be brittle).

Designed to be:
  - Independent: standalone via CLI
  - Reusable: importable by the monitor orchestrator
  - Per-ticker: small fetches (the filter only runs for tickers Gate 0 said
    investigate, typically 0-5 per cadence), bounded by the caller's semaphore
  - Best-effort: never raises; failures surface in the "errors" list and the
    caller fails-open (proceeds to LLM gates).

Usage:
    from alpacaNews import fetch_news_for_ticker
    from datetime import datetime, timezone, timedelta
    result = fetch_news_for_ticker(
        "NOW",
        since_utc=datetime.now(timezone.utc) - timedelta(hours=24),
    )
    if result["status"] == "ok":
        ids = result["ids"]

CLI:
    python3 alpacaNews.py --ticker NOW --hours 24
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv


# ============ CONFIGURATION ============

# News API does not distinguish paper vs live. The constant is retained for
# parity with the sibling alpacaPositions.py and to make a future credential
# split (separate news-only key) a one-line change.
PAPER = True

DEFAULT_LOOKBACK_HOURS = 24
DEFAULT_LIMIT = 50

# Load credentials from the project-root .env regardless of cwd.
# This file: Stage 5 DRAFT/monitor/data sources/alpacaNews.py
_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
load_dotenv()  # also honor a .env discoverable from cwd


# ============ LOGGER ============

_logger = logging.getLogger("monitor.alpacaNews")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)


# ============ HELPERS ============

def _normalize_symbol(symbol: str) -> str:
    """
    Mirror alpacaPositions._normalize_symbol: Alpaca uses '.' for class
    shares (BRK.B); the repo uses '-' everywhere downstream (BRK-B).
    """
    return (symbol or "").strip().upper().replace(".", "-")


def _to_iso(value) -> str:
    """Coerce a datetime/str to an ISO string. None on failure."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, str):
        return value
    return str(value)


def _empty_result(ticker: str, since_utc=None, until_utc=None) -> dict:
    return {
        "data_source": "alpacaNews",
        "ticker": _normalize_symbol(ticker),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "since_utc": _to_iso(since_utc),
        "until_utc": _to_iso(until_utc),
        "status": "unknown",
        "items": [],
        "ids": [],
        "errors": [],
    }


def _extract_news_list(response):
    """
    Pull the list of news objects from an alpaca-py NewsClient response.

    The response shape has drifted across alpaca-py minor versions: some
    expose a plain `.news` attribute, others wrap it as `.data["news"]`
    keyed by the symbol. Try the known shapes; fall back to anything that
    looks list-like.
    """
    # Modern alpaca-py (>=0.30): direct .news attribute holding a list
    news = getattr(response, "news", None)
    if isinstance(news, list):
        return news

    # Older shape: .data is a dict (sometimes keyed by symbol, sometimes
    # a flat "news" key)
    data = getattr(response, "data", None)
    if isinstance(data, dict):
        if "news" in data and isinstance(data["news"], list):
            return data["news"]
        # Symbol-keyed: collapse all lists
        merged = []
        for v in data.values():
            if isinstance(v, list):
                merged.extend(v)
        if merged:
            return merged

    # Last resort: response itself iterable as a list
    if isinstance(response, list):
        return response
    return []


def _coerce_article(article) -> dict:
    """
    Flatten one Alpaca news object to a plain dict. Handles both pydantic
    model attributes and dict-like access.
    """
    def _get(obj, name):
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    raw_id = _get(article, "id")
    headline = _get(article, "headline") or ""
    url = _get(article, "url")
    created_at = _get(article, "created_at") or _get(article, "updated_at")

    return {
        "id": str(raw_id) if raw_id is not None else None,
        "published_at": _to_iso(created_at),
        "headline": str(headline),
        "url": url,
    }


# ============ MAIN FETCH ============

def fetch_news_for_ticker(
    ticker: str,
    since_utc: datetime = None,
    until_utc: datetime = None,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    """
    Fetch news articles for a single ticker from Alpaca, newest first.

    Args:
        ticker: ticker symbol (in repo form — '-' for class shares)
        since_utc: earliest published_at to include; defaults to now - 24h
        until_utc: latest published_at to include; defaults to now
        limit: maximum number of articles to fetch

    Returns: dict with shape described in the module docstring. Never raises:
    any failure leaves items/ids empty and records a message in "errors",
    with status "fetch_failed".
    """
    now = datetime.now(timezone.utc)
    if since_utc is None:
        since_utc = now - timedelta(hours=DEFAULT_LOOKBACK_HOURS)
    if until_utc is None:
        until_utc = now

    result = _empty_result(ticker, since_utc=since_utc, until_utc=until_utc)
    norm_ticker = result["ticker"]

    if not norm_ticker:
        result["status"] = "fetch_failed"
        result["errors"].append("Empty ticker passed to fetch_news_for_ticker")
        return result

    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        result["status"] = "fetch_failed"
        result["errors"].append(
            "ALPACA_API_KEY and/or ALPACA_SECRET_KEY not set in environment"
        )
        _logger.warning(result["errors"][-1])
        return result

    try:
        from alpaca.data.historical import NewsClient
        from alpaca.data.requests import NewsRequest
    except ImportError as e:
        result["status"] = "fetch_failed"
        result["errors"].append(
            f"alpaca-py is required for alpacaNews but is not installed: {e}"
        )
        _logger.warning(result["errors"][-1])
        return result

    # Sort enum: prefer Sort.DESC but tolerate SDK versions without it
    sort_arg = "desc"
    try:
        from alpaca.data.enums import Sort
        sort_arg = Sort.DESC
    except Exception:
        pass

    try:
        client = NewsClient(api_key, secret_key)
    except Exception as e:
        result["status"] = "fetch_failed"
        result["errors"].append(f"Failed to construct Alpaca NewsClient: {e}")
        _logger.warning(result["errors"][-1])
        return result

    try:
        request = NewsRequest(
            symbols=[norm_ticker],
            start=since_utc,
            end=until_utc,
            limit=int(limit),
            sort=sort_arg,
        )
        response = client.get_news(request)
    except Exception as e:
        result["status"] = "fetch_failed"
        result["errors"].append(f"Failed to fetch Alpaca news: {e}")
        _logger.warning(result["errors"][-1])
        return result

    try:
        articles_raw = _extract_news_list(response)
    except Exception as e:
        result["status"] = "fetch_failed"
        result["errors"].append(f"Could not interpret Alpaca news response: {e}")
        _logger.warning(result["errors"][-1])
        return result

    for a in articles_raw:
        try:
            flat = _coerce_article(a)
            if flat["id"] is None:
                # Articles without an ID can't participate in dedup
                continue
            result["items"].append(flat)
        except Exception as e:
            result["errors"].append(f"Could not parse a news article: {e}")
            _logger.warning(result["errors"][-1])

    # Order newest-first deterministically (response is usually already sorted
    # but be defensive — comparison logic only cares about set membership)
    try:
        result["items"].sort(
            key=lambda x: x.get("published_at") or "",
            reverse=True,
        )
    except Exception:
        pass

    result["ids"] = [it["id"] for it in result["items"]]
    result["status"] = "ok"
    _logger.info(
        f"Fetched {len(result['items'])} Alpaca news item(s) for {norm_ticker} "
        f"since {since_utc.isoformat()}"
    )
    return result


# ============ CLI ============

def _cli():
    parser = argparse.ArgumentParser(description="Fetch Alpaca news for a ticker")
    parser.add_argument("--ticker", required=True, help="Ticker symbol")
    parser.add_argument("--hours", type=int, default=DEFAULT_LOOKBACK_HOURS,
                        help=f"Lookback hours (default {DEFAULT_LOOKBACK_HOURS})")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"Max articles (default {DEFAULT_LIMIT})")
    args = parser.parse_args()

    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    result = fetch_news_for_ticker(args.ticker, since_utc=since, limit=args.limit)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(_cli())
