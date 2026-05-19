import logging
import asyncio
import time
import random
from datetime import datetime, timedelta
import requests

logger = logging.getLogger(__name__)

# ============ SEC API CONFIG ============

# SEC requires a meaningful User-Agent for all API requests.
# Replace with your contact info or company name in production.
SEC_USER_AGENT = "InvestmentPipeline contact@example.com"

SEC_TICKER_CIK_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL_TEMPLATE = "https://data.sec.gov/submissions/CIK{cik_padded}.json"

# Politeness delay between SEC requests (SEC limit: 10/sec)
SEC_REQUEST_DELAY_SECONDS = 0.2
RETRY_DELAYS = [2, 5, 15]  # 4 attempts total


# ============ HELPERS ============

def _pad_cik(cik) -> str:
    """SEC requires CIK as a 10-digit zero-padded string."""
    return str(cik).zfill(10)


def _http_request_with_retry(url: str, headers: dict = None) -> dict:
    """
    Make an HTTP GET request to SEC with retry-on-failure logic.
    Returns parsed JSON response, or raises RuntimeError on final failure.
    """
    if headers is None:
        headers = {}
    headers.setdefault("User-Agent", SEC_USER_AGENT)
    headers.setdefault("Accept", "application/json")

    attempts = len(RETRY_DELAYS) + 1

    for attempt_num in range(attempts):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 404:
                # Resource doesn't exist (e.g., company has no filings)
                raise RuntimeError(f"SEC 404: {url}")
            elif resp.status_code == 429:
                # Rate limited
                logger.warning(f"SEC rate limit hit on {url}; backing off")
            else:
                logger.warning(f"SEC returned {resp.status_code} on {url}")
        except requests.RequestException as e:
            logger.warning(f"SEC request error on {url}: {e}")

        if attempt_num < len(RETRY_DELAYS):
            wait = RETRY_DELAYS[attempt_num]
            time.sleep(wait + random.uniform(0, 0.5))

    raise RuntimeError(f"SEC request failed after {attempts} attempts: {url}")


# ============ TICKER-TO-CIK MAPPING ============

def fetch_ticker_cik_map() -> dict:
    """
    Fetch SEC's official ticker-to-CIK mapping.
    Returns dict like: {"AAPL": "0000320193", "MSFT": "0000789019", ...}
    """
    logger.info("Fetching SEC ticker-to-CIK mapping...")
    data = _http_request_with_retry(SEC_TICKER_CIK_URL)

    # The file is structured as {"0": {"cik_str": ..., "ticker": ..., "title": ...}, ...}
    out = {}
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        ticker = entry.get("ticker", "").strip().upper()
        cik = entry.get("cik_str")
        if ticker and cik is not None:
            out[ticker] = _pad_cik(cik)

    logger.info(f"Loaded {len(out)} ticker-to-CIK mappings.")
    return out


def normalize_ticker_for_sec(ticker: str) -> str:
    """
    SEC uses '.' for class shares while yfinance uses '-' (we used '-' in our universe).
    Normalize back to SEC convention for the lookup.
    e.g., 'BRK-B' -> 'BRK.B', 'BF-B' -> 'BF.B'
    Note: SEC's company_tickers.json actually uses no separator for some classes
    ('BRKB' instead of 'BRK.B'), so we may need to try both.
    """
    if not isinstance(ticker, str):
        return ""
    return ticker.strip().upper()


def lookup_cik(ticker: str, mapping: dict) -> str:
    """
    Look up a ticker's CIK with fallback variations (some tickers are stored
    differently in SEC's mapping vs IWB's CSV).
    Returns CIK string or None if not found.
    """
    t = normalize_ticker_for_sec(ticker)

    # Try direct lookup
    if t in mapping:
        return mapping[t]

    # Try with dot instead of dash (BRK-B -> BRK.B style)
    if "-" in t:
        alt = t.replace("-", ".")
        if alt in mapping:
            return mapping[alt]
        # Try with no separator (BRK-B -> BRKB)
        alt2 = t.replace("-", "")
        if alt2 in mapping:
            return mapping[alt2]

    return None


# ============ FILINGS FETCH ============

def fetch_filings_for_cik(cik: str) -> dict:
    """
    Fetch SEC submissions metadata for a CIK.
    Returns the full JSON response, or None if not found.
    """
    cik_padded = _pad_cik(cik)
    url = SEC_SUBMISSIONS_URL_TEMPLATE.format(cik_padded=cik_padded)
    try:
        return _http_request_with_retry(url)
    except RuntimeError as e:
        logger.warning(f"Could not fetch filings for CIK {cik}: {e}")
        return None


# ============ DISQUALIFIER CHECKS ============

def _parse_recent_filings(submissions_data: dict) -> list:
    """
    Parse the 'recent' filings array into a list of dicts.
    Each dict has form, filingDate, items (where available), accessionNumber.
    """
    out = []
    if not submissions_data:
        return out

    recent = submissions_data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    items_list = recent.get("items", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    n = len(forms)
    for i in range(n):
        out.append({
            "form": forms[i] if i < len(forms) else "",
            "filing_date": dates[i] if i < len(dates) else "",
            "items": items_list[i] if i < len(items_list) else "",
            "accession_number": accessions[i] if i < len(accessions) else "",
            "primary_document": primary_docs[i] if i < len(primary_docs) else "",
        })
    return out


def _is_within_lookback(filing_date: str, cutoff_date: datetime) -> bool:
    """Check if a filing date string (YYYY-MM-DD) is within the lookback window."""
    if not filing_date:
        return False
    try:
        d = datetime.strptime(filing_date, "%Y-%m-%d")
        return d >= cutoff_date
    except ValueError:
        return False


def _check_for_bankruptcy_8k(filings: list, cutoff_date: datetime) -> dict:
    """
    Check filings for an 8-K with item 1.03 (bankruptcy/receivership) within lookback window.

    Note: We rely on the 'items' field from the SEC submissions API where available.
    If the field is empty, we don't fetch the 8-K's index page (per design decision).
    This may miss bankruptcy 8-Ks where the items field wasn't populated.
    """
    matches = []
    for filing in filings:
        if filing.get("form") != "8-K":
            continue
        if not _is_within_lookback(filing.get("filing_date", ""), cutoff_date):
            continue
        items = filing.get("items", "")
        if not items:
            # Items field empty — skip per design decision
            continue
        # Item 1.03 is "Bankruptcy or Receivership"
        # The items field is a comma-separated string like "1.03,9.01"
        if "1.03" in items:
            matches.append({
                "filing_date": filing.get("filing_date"),
                "accession_number": filing.get("accession_number"),
                "items": items,
            })

    if matches:
        return {
            "triggered": True,
            "matches": matches,
            "most_recent_date": max(m["filing_date"] for m in matches),
        }
    return {"triggered": False}


def _check_for_restatement_10ka(filings: list, cutoff_date: datetime) -> dict:
    """
    Check filings for a 10-K/A (amended annual report — usually restatement) within lookback window.
    """
    matches = []
    for filing in filings:
        if filing.get("form") != "10-K/A":
            continue
        if not _is_within_lookback(filing.get("filing_date", ""), cutoff_date):
            continue
        matches.append({
            "filing_date": filing.get("filing_date"),
            "accession_number": filing.get("accession_number"),
        })

    if matches:
        return {
            "triggered": True,
            "matches": matches,
            "most_recent_date": max(m["filing_date"] for m in matches),
        }
    return {"triggered": False}


# ============ PUBLIC API ============

def check_company_filings(ticker: str, cik: str, lookback_months: int = 12) -> dict:
    """
    Check a company's recent SEC filings for disqualifier signals.

    Returns:
        {
            "check_status": "ok" | "error",
            "error_message": "..." (if status=error),
            "bankruptcy_8k": {triggered, matches, most_recent_date} OR {triggered: False},
            "material_restatement_10ka": {...},
            "qualified": bool | None  # null if check_status=error
        }
    """
    if not cik:
        return {
            "check_status": "error",
            "error_message": f"No CIK found for ticker {ticker}",
            "bankruptcy_8k": {"triggered": False},
            "material_restatement_10ka": {"triggered": False},
            "qualified": None,
        }

    submissions = fetch_filings_for_cik(cik)
    if submissions is None:
        return {
            "check_status": "error",
            "error_message": f"Could not fetch filings from SEC for CIK {cik}",
            "bankruptcy_8k": {"triggered": False},
            "material_restatement_10ka": {"triggered": False},
            "qualified": None,
        }

    filings = _parse_recent_filings(submissions)
    cutoff_date = datetime.now() - timedelta(days=lookback_months * 30)

    bk_result = _check_for_bankruptcy_8k(filings, cutoff_date)
    rs_result = _check_for_restatement_10ka(filings, cutoff_date)

    triggered_any = bk_result.get("triggered") or rs_result.get("triggered")

    return {
        "check_status": "ok",
        "bankruptcy_8k": bk_result,
        "material_restatement_10ka": rs_result,
        "qualified": not triggered_any,
    }


async def check_company_filings_async(ticker: str, cik: str, lookback_months: int = 12) -> dict:
    """Async wrapper around check_company_filings, with politeness delay."""
    # Politeness delay before each call
    await asyncio.sleep(SEC_REQUEST_DELAY_SECONDS + random.uniform(0, 0.1))
    return await asyncio.to_thread(check_company_filings, ticker, cik, lookback_months)