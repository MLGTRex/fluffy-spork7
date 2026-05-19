import io
import logging
from datetime import datetime
import requests
import pandas as pd

logger = logging.getLogger(__name__)

# iShares IWB ETF holdings CSV endpoint
IWB_CSV_URL = (
    "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/"
    "1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund"
)


# Wikipedia Russell 1000 page
WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/Russell_1000_Index"

# Some headers iShares may require to serve the CSV
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/csv,text/plain,*/*",
}


def normalize_ticker(ticker: str) -> str:
    """
    Convert IWB/Wikipedia ticker conventions to yfinance conventions.
    e.g., 'BRK.B' -> 'BRK-B', 'BF.B' -> 'BF-B'.
    """
    if not isinstance(ticker, str):
        return ""
    t = ticker.strip().upper()
    # yfinance uses '-' for share class delimiter where many sources use '.'
    t = t.replace(".", "-")
    return t


def fetch_from_iwb() -> list:
    """
    Fetch Russell 1000 constituents from the iShares IWB holdings CSV.

    Returns: list of dicts: [{"ticker": "AAPL", "company_name": "Apple Inc"}, ...]
    Raises RuntimeError on failure.
    """
    logger.info("Fetching Russell 1000 from iShares IWB holdings CSV...")
    try:
        resp = requests.get(IWB_CSV_URL, headers=HTTP_HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"IWB CSV fetch failed: {e}")

    text = resp.text

    # The CSV has metadata header rows before the actual data table.
    # The header row of the data table typically starts with "Ticker"
    # We find the line containing "Ticker," and parse from there.
    lines = text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if line.strip().startswith("Ticker") or line.lower().strip().startswith("ticker,"):
            header_idx = i
            break

    if header_idx is None:
        raise RuntimeError("IWB CSV: could not locate data header row (no 'Ticker' line found)")

    csv_body = "\n".join(lines[header_idx:])
    try:
        df = pd.read_csv(io.StringIO(csv_body))
    except Exception as e:
        raise RuntimeError(f"IWB CSV parse failed: {e}")

    # Column names vary slightly between iShares CSV versions. Normalize.
    df.columns = [c.strip() for c in df.columns]

    # Identify the right columns (defensive)
    ticker_col = None
    name_col = None
    asset_class_col = None
    for c in df.columns:
        cl = c.lower()
        if cl == "ticker":
            ticker_col = c
        elif cl == "name":
            name_col = c
        elif cl == "asset class":
            asset_class_col = c

    if ticker_col is None or name_col is None:
        raise RuntimeError(
            f"IWB CSV: missing expected columns (got: {list(df.columns)})"
        )

    # Filter to equity rows only (drop cash, futures, etc.)
    if asset_class_col:
        df = df[df[asset_class_col].astype(str).str.strip().str.lower() == "equity"]

    # Drop rows with missing tickers or names
    df = df[df[ticker_col].notna() & df[name_col].notna()]
    df = df[df[ticker_col].astype(str).str.strip() != "-"]

    companies = []
    seen = set()
    for _, row in df.iterrows():
        raw_ticker = str(row[ticker_col]).strip()
        raw_name = str(row[name_col]).strip()
        if not raw_ticker or not raw_name:
            continue
        ticker = normalize_ticker(raw_ticker)
        if ticker in seen:
            continue
        seen.add(ticker)
        companies.append({"ticker": ticker, "company_name": raw_name})

    if not companies:
        raise RuntimeError("IWB CSV: parse succeeded but no equity rows extracted")

    logger.info(f"IWB fetch successful: {len(companies)} companies extracted.")
    return companies


def fetch_from_wikipedia() -> list:
    """
    Fetch Russell 1000 constituents from Wikipedia as a fallback.

    Returns: list of dicts: [{"ticker": ..., "company_name": ...}, ...]
    Raises RuntimeError on failure.
    """
    logger.info("Fetching Russell 1000 from Wikipedia...")
    try:
        # pd.read_html(url) fetches the page itself via urllib with a default
        # Python user-agent, which Wikipedia now rejects with HTTP 403. Fetch
        # the page ourselves with a browser-like User-Agent, then parse the
        # returned HTML from a buffer.
        resp = requests.get(WIKIPEDIA_URL, headers=HTTP_HEADERS, timeout=30)
        resp.raise_for_status()
        tables = pd.read_html(io.StringIO(resp.text))
    except Exception as e:
        raise RuntimeError(f"Wikipedia fetch failed: {e}")

    # Find the table that has both a ticker-like column and a name-like column.
    # Wikipedia's structure can shift; we scan all tables and pick the first match.
    candidate = None
    for df in tables:
        cols_lower = [str(c).lower() for c in df.columns]
        has_ticker = any("ticker" in c or "symbol" in c for c in cols_lower)
        has_name = any("company" in c or "name" in c for c in cols_lower)
        if has_ticker and has_name and len(df) > 100:  # Russell 1000 should be ~1000 rows
            candidate = df
            break

    if candidate is None:
        raise RuntimeError(
            "Wikipedia: could not locate a constituents table with ticker + name columns"
        )

    # Identify the right columns
    ticker_col = None
    name_col = None
    for c in candidate.columns:
        cl = str(c).lower()
        if ticker_col is None and ("ticker" in cl or "symbol" in cl):
            ticker_col = c
        elif name_col is None and ("company" in cl or "name" in cl):
            name_col = c

    if ticker_col is None or name_col is None:
        raise RuntimeError(
            f"Wikipedia: could not identify ticker/name columns (got: {list(candidate.columns)})"
        )

    companies = []
    seen = set()
    for _, row in candidate.iterrows():
        raw_ticker = str(row[ticker_col]).strip()
        raw_name = str(row[name_col]).strip()
        if not raw_ticker or raw_ticker.lower() == "nan":
            continue
        if not raw_name or raw_name.lower() == "nan":
            continue
        ticker = normalize_ticker(raw_ticker)
        if ticker in seen:
            continue
        seen.add(ticker)
        companies.append({"ticker": ticker, "company_name": raw_name})

    if not companies:
        raise RuntimeError("Wikipedia: table parsed but no rows extracted")

    logger.info(f"Wikipedia fetch successful: {len(companies)} companies extracted.")
    return companies


def fetch_universe() -> dict:
    """
    Try IWB first, fall back to Wikipedia if IWB fails.

    Returns a dict ready to write to universe.json:
    {
        "fetched_date": "2026-05-07",
        "source": "IWB" | "Wikipedia",
        "company_count": N,
        "companies": [...]
    }
    """
    today_str = datetime.now().date().isoformat()

    try:
        companies = fetch_from_iwb()
        return {
            "fetched_date": today_str,
            "source": "IWB",
            "company_count": len(companies),
            "companies": companies,
        }
    except Exception as e:
        logger.warning(f"IWB fetch failed: {e}. Falling back to Wikipedia.")

    companies = fetch_from_wikipedia()
    return {
        "fetched_date": today_str,
        "source": "Wikipedia",
        "company_count": len(companies),
        "companies": companies,
    }