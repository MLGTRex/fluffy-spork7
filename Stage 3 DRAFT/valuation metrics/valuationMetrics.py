import os
import logging
import asyncio
import statistics
from datetime import datetime
from dotenv import load_dotenv
import yfinance as yf
import pandas as pd

load_dotenv()

log_filename = f"research_log_{datetime.now().strftime('%Y-%m-%d')}.log"

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

# Max peers to include in peer comparison
MAX_PEERS = 8

# Metrics to include in peer comparison aggregation
PEER_COMPARISON_METRICS = [
    "pe_trailing",
    "pe_forward",
    "ev_ebitda",
    "ps_ratio",
    "operating_margin",
    "roe",
    "revenue_growth_1yr",
]


def _safe_div(numerator, denominator):
    """Safe division — returns None if any operand is None or denominator is zero."""
    if numerator is None or denominator is None:
        return None
    try:
        if denominator == 0:
            return None
        return numerator / denominator
    except (TypeError, ZeroDivisionError):
        return None


def _safe_get(d: dict, key: str):
    """Safe dict access — returns None if key missing or value is NaN."""
    if d is None:
        return None
    val = d.get(key)
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    return val


def _compute_cagr(start_value, end_value, years: int):
    """Compute CAGR between two values across N years. Returns None on invalid input."""
    if start_value is None or end_value is None or years <= 0:
        return None
    try:
        if start_value <= 0 or end_value <= 0:
            return None
        return (end_value / start_value) ** (1 / years) - 1
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def extract_company_metrics(ticker_obj, ticker_symbol: str, flags: list) -> dict:
    """
    Extract the full metric set for a single company from a yfinance Ticker object.
    Mutates `flags` to record any data quality issues encountered.
    Returns a dict matching the JSON output schema (without peer_comparison).
    """
    info = {}
    try:
        info = ticker_obj.info or {}
    except Exception as e:
        flags.append(f"{ticker_symbol}: failed to fetch .info ({e})")
        info = {}

    # Classification
    sector = info.get("sector")
    industry = info.get("industry")
    industry_key = info.get("industryKey")
    if not industry:
        flags.append(f"{ticker_symbol}: industry classification unavailable")

    # ---- Profitability & margins ----
    gross_margin_ttm = _safe_get(info, "grossMargins")
    operating_margin_ttm = _safe_get(info, "operatingMargins")
    net_margin_ttm = _safe_get(info, "profitMargins")
    roe_ttm = _safe_get(info, "returnOnEquity")
    roa_ttm = _safe_get(info, "returnOnAssets")
    # yfinance does not expose ROIC directly; approximate later if needed
    roic_ttm = None

    if gross_margin_ttm is None:
        flags.append(f"{ticker_symbol}: gross margin unavailable")
    if operating_margin_ttm is None:
        flags.append(f"{ticker_symbol}: operating margin unavailable")
    if roe_ttm is None:
        flags.append(f"{ticker_symbol}: ROE unavailable")

    # ---- 3yr trend on margins (from financials statements) ----
    gross_margin_3yr_avg = None
    operating_margin_3yr_avg = None
    try:
        fin = ticker_obj.financials  # annual income statement, columns = years (most recent first)
        if isinstance(fin, pd.DataFrame) and not fin.empty:
            cols = fin.columns[:3]  # last 3 fiscal years
            revs = []
            gross_profits = []
            op_incomes = []
            for c in cols:
                rev = fin.get(c, {}).get("Total Revenue") if c in fin.columns else None
                gp = fin.get(c, {}).get("Gross Profit") if c in fin.columns else None
                op = fin.get(c, {}).get("Operating Income") if c in fin.columns else None
                if rev is not None and not pd.isna(rev):
                    revs.append(rev)
                if gp is not None and not pd.isna(gp):
                    gross_profits.append(gp)
                if op is not None and not pd.isna(op):
                    op_incomes.append(op)

            gm_values = [_safe_div(g, r) for g, r in zip(gross_profits, revs)]
            gm_values = [v for v in gm_values if v is not None]
            if gm_values:
                gross_margin_3yr_avg = sum(gm_values) / len(gm_values)

            om_values = [_safe_div(o, r) for o, r in zip(op_incomes, revs)]
            om_values = [v for v in om_values if v is not None]
            if om_values:
                operating_margin_3yr_avg = sum(om_values) / len(om_values)
    except Exception as e:
        flags.append(f"{ticker_symbol}: 3yr margin trend computation failed ({e})")

    # ---- Growth ----
    revenue_growth_1yr = _safe_get(info, "revenueGrowth")
    eps_growth_1yr = _safe_get(info, "earningsGrowth")

    # 3yr CAGRs from financials
    revenue_cagr_3yr = None
    eps_cagr_3yr = None
    fcf_growth_1yr = None
    fcf_cagr_3yr = None
    try:
        fin = ticker_obj.financials
        if isinstance(fin, pd.DataFrame) and not fin.empty:
            cols = list(fin.columns[:4])  # need 4 points for 3yr CAGR
            revs = []
            for c in cols:
                rev = fin.get(c, {}).get("Total Revenue") if c in fin.columns else None
                if rev is not None and not pd.isna(rev):
                    revs.append(rev)
            if len(revs) >= 4:
                # cols are most-recent-first, so revs[0]=latest, revs[3]=4yrs ago (3yr span)
                revenue_cagr_3yr = _compute_cagr(revs[3], revs[0], 3)

            # Net income / Diluted EPS — yfinance may have netIncome but not per-share EPS history
            net_incomes = []
            for c in cols:
                ni = fin.get(c, {}).get("Net Income") if c in fin.columns else None
                if ni is not None and not pd.isna(ni):
                    net_incomes.append(ni)
            if len(net_incomes) >= 4:
                eps_cagr_3yr = _compute_cagr(net_incomes[3], net_incomes[0], 3)
                # Note: this is net income CAGR, not strictly EPS CAGR.
                # Diluted shares can change. Flagging as approximation.
                flags.append(f"{ticker_symbol}: eps_cagr_3yr approximated by net income CAGR (share count changes not adjusted)")

        cf = ticker_obj.cashflow
        if isinstance(cf, pd.DataFrame) and not cf.empty:
            cols = list(cf.columns[:4])
            fcfs = []
            for c in cols:
                # FCF = Operating Cash Flow - Capital Expenditure (capex is reported as negative)
                ocf = cf.get(c, {}).get("Operating Cash Flow") if c in cf.columns else None
                capex = cf.get(c, {}).get("Capital Expenditure") if c in cf.columns else None
                if ocf is not None and capex is not None and not pd.isna(ocf) and not pd.isna(capex):
                    fcfs.append(ocf + capex)  # capex is typically negative, so + adds the negative
            if len(fcfs) >= 2:
                fcf_growth_1yr = _safe_div(fcfs[0] - fcfs[1], abs(fcfs[1]))
            if len(fcfs) >= 4:
                fcf_cagr_3yr = _compute_cagr(fcfs[3], fcfs[0], 3)
    except Exception as e:
        flags.append(f"{ticker_symbol}: growth metrics computation failed ({e})")

    # ---- Valuation multiples ----
    pe_trailing = _safe_get(info, "trailingPE")
    pe_forward = _safe_get(info, "forwardPE")
    ps_ratio = _safe_get(info, "priceToSalesTrailing12Months")
    pb_ratio = _safe_get(info, "priceToBook")
    ev_ebitda = _safe_get(info, "enterpriseToEbitda")
    peg_ratio = _safe_get(info, "trailingPegRatio") or _safe_get(info, "pegRatio")

    # FCF yield = FCF / Market Cap
    market_cap = _safe_get(info, "marketCap")
    free_cash_flow = _safe_get(info, "freeCashflow")
    fcf_yield = _safe_div(free_cash_flow, market_cap)

    if pe_trailing is None:
        flags.append(f"{ticker_symbol}: trailing P/E unavailable (likely loss-making or not reported)")
    if ev_ebitda is None:
        flags.append(f"{ticker_symbol}: EV/EBITDA unavailable")

    # ---- Balance sheet ----
    debt_to_equity = _safe_get(info, "debtToEquity")
    if debt_to_equity is not None:
        # yfinance reports this as a percentage (e.g., 284 means 2.84). Normalize to ratio.
        debt_to_equity = debt_to_equity / 100.0
    current_ratio = _safe_get(info, "currentRatio")
    total_debt = _safe_get(info, "totalDebt")
    total_cash = _safe_get(info, "totalCash")
    ebitda = _safe_get(info, "ebitda")
    net_debt = None
    net_debt_to_ebitda = None
    if total_debt is not None and total_cash is not None:
        net_debt = total_debt - total_cash
        net_debt_to_ebitda = _safe_div(net_debt, ebitda)

    # ---- Per share ----
    eps_ttm = _safe_get(info, "trailingEps")
    eps_forward = _safe_get(info, "forwardEps")
    revenue_per_share = _safe_get(info, "revenuePerShare")
    book_value_per_share = _safe_get(info, "bookValue")
    dividend_yield = _safe_get(info, "dividendYield")
    if dividend_yield is not None and dividend_yield > 1:
        # Some yfinance fields return as percent; if >1, assume percent and divide
        dividend_yield = dividend_yield / 100.0
    shares_outstanding = _safe_get(info, "sharesOutstanding")

    # ---- Assemble (without peer_comparison and history_5yr; those are added separately) ----
    return {
        "ticker": ticker_symbol,
        "sector": sector,
        "industry": industry,
        "industry_key": industry_key,
        "market_cap_usd": market_cap,
        "profitability": {
            "gross_margin_ttm": gross_margin_ttm,
            "operating_margin_ttm": operating_margin_ttm,
            "net_margin_ttm": net_margin_ttm,
            "gross_margin_3yr_avg": gross_margin_3yr_avg,
            "operating_margin_3yr_avg": operating_margin_3yr_avg,
            "roe_ttm": roe_ttm,
            "roa_ttm": roa_ttm,
            "roic_ttm": roic_ttm,
        },
        "growth": {
            "revenue_growth_1yr": revenue_growth_1yr,
            "revenue_cagr_3yr": revenue_cagr_3yr,
            "eps_growth_1yr": eps_growth_1yr,
            "eps_cagr_3yr": eps_cagr_3yr,
            "fcf_growth_1yr": fcf_growth_1yr,
            "fcf_cagr_3yr": fcf_cagr_3yr,
        },
        "valuation_multiples": {
            "pe_trailing": pe_trailing,
            "pe_forward": pe_forward,
            "ps_ratio": ps_ratio,
            "pb_ratio": pb_ratio,
            "ev_ebitda": ev_ebitda,
            "fcf_yield": fcf_yield,
            "peg_ratio": peg_ratio,
        },
        "balance_sheet": {
            "debt_to_equity": debt_to_equity,
            "current_ratio": current_ratio,
            "net_debt_to_ebitda": net_debt_to_ebitda,
            "total_debt_usd": total_debt,
            "total_cash_usd": total_cash,
        },
        "per_share": {
            "eps_ttm": eps_ttm,
            "eps_forward": eps_forward,
            "revenue_per_share": revenue_per_share,
            "book_value_per_share": book_value_per_share,
            "dividend_yield": dividend_yield,
            "shares_outstanding": shares_outstanding,
        },
    }


def compute_5yr_multiple_history(ticker_obj, ticker_symbol: str, flags: list) -> dict:
    """
    Compute 5-year min/median/max for key valuation multiples.
    Approach: pull 5 years of daily prices and quarterly fundamentals, compute
    rolling P/E, P/S, EV/EBITDA, FCF yield using TTM fundamentals at each date.

    Returns a dict {metric: {current, min, median, max}} or None values if not computable.
    """
    result = {
        "pe_trailing": {"current": None, "min": None, "median": None, "max": None},
        "ps_ratio": {"current": None, "min": None, "median": None, "max": None},
        "ev_ebitda": {"current": None, "min": None, "median": None, "max": None},
        "fcf_yield": {"current": None, "min": None, "median": None, "max": None},
    }

    try:
        # Pull 5yr of monthly prices (daily is too granular and slows things down)
        hist = ticker_obj.history(period="5y", interval="1mo")
        if hist.empty:
            flags.append(f"{ticker_symbol}: 5yr price history unavailable")
            return result

        # Quarterly fundamentals
        q_fin = ticker_obj.quarterly_financials
        q_cf = ticker_obj.quarterly_cashflow
        q_bs = ticker_obj.quarterly_balance_sheet

        if not isinstance(q_fin, pd.DataFrame) or q_fin.empty:
            flags.append(f"{ticker_symbol}: quarterly financials unavailable for 5yr history")
            return result

        # For each month-end, compute TTM (sum of last 4 quarters available before that date)
        # then divide market cap / EV by the relevant TTM metric.
        def ttm_at_date(df: pd.DataFrame, row_label: str, target_date) -> float:
            """Sum of last 4 quarters of `row_label` from df with quarter-end columns, prior to target_date."""
            if not isinstance(df, pd.DataFrame) or df.empty or row_label not in df.index:
                return None
            try:
                # Columns are quarter-end dates (datetime). Filter to those <= target_date and take last 4.
                date_cols = [c for c in df.columns if pd.notna(c) and c <= target_date]
                if len(date_cols) < 4:
                    return None
                date_cols = sorted(date_cols, reverse=True)[:4]
                vals = [df.loc[row_label, c] for c in date_cols]
                vals = [v for v in vals if pd.notna(v)]
                if len(vals) < 4:
                    return None
                return sum(vals)
            except Exception:
                return None

        def latest_value(df: pd.DataFrame, row_label: str, target_date) -> float:
            """Most recent value of `row_label` from df at or before target_date."""
            if not isinstance(df, pd.DataFrame) or df.empty or row_label not in df.index:
                return None
            try:
                date_cols = [c for c in df.columns if pd.notna(c) and c <= target_date]
                if not date_cols:
                    return None
                latest = max(date_cols)
                v = df.loc[row_label, latest]
                if pd.isna(v):
                    return None
                return v
            except Exception:
                return None

        pe_series, ps_series, ev_ebitda_series, fcf_yield_series = [], [], [], []

        # We need shares outstanding to compute market cap from price. Use latest known.
        try:
            shares = ticker_obj.info.get("sharesOutstanding")
        except Exception:
            shares = None

        for ts, row in hist.iterrows():
            close = row.get("Close")
            if close is None or pd.isna(close) or shares is None:
                continue
            mcap = close * shares

            # TTM net income (for P/E)
            ttm_ni = ttm_at_date(q_fin, "Net Income", ts)
            if ttm_ni is not None and ttm_ni > 0:
                pe_series.append(mcap / ttm_ni)

            # TTM revenue (for P/S)
            ttm_rev = ttm_at_date(q_fin, "Total Revenue", ts)
            if ttm_rev is not None and ttm_rev > 0:
                ps_series.append(mcap / ttm_rev)

            # EV / TTM EBITDA — approximate EV = market_cap + total_debt - cash (using latest balance sheet at that date)
            total_debt_at = latest_value(q_bs, "Total Debt", ts)
            cash_at = latest_value(q_bs, "Cash And Cash Equivalents", ts)
            ttm_ebitda = ttm_at_date(q_fin, "EBITDA", ts)
            if ttm_ebitda is not None and ttm_ebitda > 0 and total_debt_at is not None:
                ev = mcap + (total_debt_at or 0) - (cash_at or 0)
                ev_ebitda_series.append(ev / ttm_ebitda)

            # FCF yield = TTM FCF / market cap
            ttm_ocf = ttm_at_date(q_cf, "Operating Cash Flow", ts)
            ttm_capex = ttm_at_date(q_cf, "Capital Expenditure", ts)
            if ttm_ocf is not None and ttm_capex is not None and mcap > 0:
                ttm_fcf = ttm_ocf + ttm_capex
                fcf_yield_series.append(ttm_fcf / mcap)

        def summarize(series: list) -> dict:
            if not series:
                return {"current": None, "min": None, "median": None, "max": None}
            return {
                "current": series[-1],
                "min": min(series),
                "median": statistics.median(series),
                "max": max(series),
            }

        result["pe_trailing"] = summarize(pe_series)
        result["ps_ratio"] = summarize(ps_series)
        result["ev_ebitda"] = summarize(ev_ebitda_series)
        result["fcf_yield"] = summarize(fcf_yield_series)

        if not pe_series:
            flags.append(f"{ticker_symbol}: 5yr P/E history could not be computed")

    except Exception as e:
        flags.append(f"{ticker_symbol}: 5yr multiple history computation failed ({e})")

    return result


def get_peer_tickers(industry_key: str, target_ticker: str, flags: list) -> list:
    """
    Get up to MAX_PEERS peer tickers in the same industry, ranked by market cap (descending).
    Excludes the target ticker itself.
    """
    if not industry_key:
        flags.append(f"{target_ticker}: cannot find peers (no industry_key)")
        return []
    try:
        ind = yf.Industry(industry_key)
        top = ind.top_companies  # DataFrame
        if top is None or (isinstance(top, pd.DataFrame) and top.empty):
            flags.append(f"{target_ticker}: yf.Industry({industry_key}).top_companies returned no data")
            return []

        # The DataFrame index is typically the ticker symbol; columns include market cap.
        # Filter out the target itself, take top N.
        tickers = []
        for sym in top.index:
            if str(sym).upper() == target_ticker.upper():
                continue
            tickers.append(str(sym))
            if len(tickers) >= MAX_PEERS:
                break
        return tickers
    except Exception as e:
        flags.append(f"{target_ticker}: peer ticker lookup failed ({e})")
        return []


def extract_peer_metric_subset(peer_metrics: dict) -> dict:
    """Extract only the subset of metrics used in peer comparison from a full metrics dict."""
    p = peer_metrics.get("profitability") or {}
    g = peer_metrics.get("growth") or {}
    v = peer_metrics.get("valuation_multiples") or {}
    return {
        "pe_trailing": v.get("pe_trailing"),
        "pe_forward": v.get("pe_forward"),
        "ev_ebitda": v.get("ev_ebitda"),
        "ps_ratio": v.get("ps_ratio"),
        "operating_margin": p.get("operating_margin_ttm"),
        "roe": p.get("roe_ttm"),
        "revenue_growth_1yr": g.get("revenue_growth_1yr"),
    }


def aggregate_peer_comparison(company_metrics: dict, peer_metric_dicts: list) -> dict:
    """
    Build the peer_comparison block: for each metric, compute company / peer_median / peer_min / peer_max.
    """
    company_subset = extract_peer_metric_subset(company_metrics)
    peer_subsets = [extract_peer_metric_subset(pm) for pm in peer_metric_dicts]

    peer_metrics_block = {}
    for metric in PEER_COMPARISON_METRICS:
        peer_values = [s.get(metric) for s in peer_subsets if s.get(metric) is not None]
        company_val = company_subset.get(metric)
        if peer_values:
            peer_metrics_block[metric] = {
                "company": company_val,
                "peer_median": statistics.median(peer_values),
                "peer_min": min(peer_values),
                "peer_max": max(peer_values),
                "peer_count": len(peer_values),
            }
        else:
            peer_metrics_block[metric] = {
                "company": company_val,
                "peer_median": None,
                "peer_min": None,
                "peer_max": None,
                "peer_count": 0,
            }

    return peer_metrics_block


async def fetch_one_ticker_metrics(ticker_symbol: str, flags: list) -> dict:
    """Async wrapper around extract_company_metrics for a single ticker (yfinance is sync)."""
    def _do():
        try:
            t = yf.Ticker(ticker_symbol)
            return extract_company_metrics(t, ticker_symbol, flags)
        except Exception as e:
            flags.append(f"{ticker_symbol}: full metrics fetch failed ({e})")
            return None

    return await asyncio.to_thread(_do)


async def run_valuation_metrics(company_name: str, ticker_symbol: str) -> dict:
    """
    Generate the valuation metrics block for a company.

    Args:
        company_name: e.g., 'Mastercard (MA)' (used for logging only)
        ticker_symbol: extracted ticker, e.g., 'MA'

    Returns:
        A dict matching the JSON output schema, plus a 'data_quality_flags' list.
    """
    logger.info(f"Initialising Valuation Metrics Function for {company_name}...")
    flags = []

    # 1. Fetch company metrics + 5yr history (sync, run in thread)
    def _company_extract():
        t = yf.Ticker(ticker_symbol)
        company_block = extract_company_metrics(t, ticker_symbol, flags)
        history_block = compute_5yr_multiple_history(t, ticker_symbol, flags)
        return company_block, history_block

    company_block, history_block = await asyncio.to_thread(_company_extract)

    industry_key = company_block.get("industry_key")
    logger.info(f"{ticker_symbol}: industry_key='{industry_key}', sector='{company_block.get('sector')}'")

    # 2. Find peers (sync, in thread)
    peer_tickers = await asyncio.to_thread(get_peer_tickers, industry_key, ticker_symbol, flags)
    logger.info(f"{ticker_symbol}: found {len(peer_tickers)} peers: {peer_tickers}")

    # 3. Fetch peer metrics in parallel
    peer_results = []
    if peer_tickers:
        peer_results = await asyncio.gather(
            *(fetch_one_ticker_metrics(p, flags) for p in peer_tickers),
            return_exceptions=True,
        )
        # Filter out failures and exceptions
        peer_results = [r for r in peer_results if isinstance(r, dict)]

    # 4. Build peer comparison block
    peer_comparison = {
        "peer_tickers": [r.get("ticker") for r in peer_results],
        "peer_metrics": aggregate_peer_comparison(company_block, peer_results),
    }

    # 5. Assemble final output
    output = {
        "valuation_metrics_date": datetime.now().date().isoformat(),
        "ticker": ticker_symbol,
        "sector": company_block.get("sector"),
        "industry": company_block.get("industry"),
        "market_cap_usd": company_block.get("market_cap_usd"),
        "profitability": company_block.get("profitability"),
        "growth": company_block.get("growth"),
        "valuation_multiples": company_block.get("valuation_multiples"),
        "valuation_multiples_history_5yr": history_block,
        "balance_sheet": company_block.get("balance_sheet"),
        "per_share": company_block.get("per_share"),
        "peer_comparison": peer_comparison,
        "data_quality_flags": flags,
    }

    logger.info(f"Valuation Metrics Function completed for {company_name}.\n")
    return output