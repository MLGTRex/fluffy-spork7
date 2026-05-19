import logging
import asyncio
import time
import random
from datetime import datetime
import yfinance as yf
import pandas as pd

logger = logging.getLogger(__name__)

# ============ RATE-LIMIT HANDLING CONFIG ============

# Constant politeness delay added to every extraction call (seconds).
# A small jitter is added on top to avoid synchronized waves.
POLITENESS_DELAY_SECONDS = 0.5

# Retry strategy: list of delays (in seconds) before each retry attempt.
# 5 retries means up to 6 total attempts per company.
RETRY_DELAYS = [2, 5, 15, 30, 60]


# ============ HELPERS ============

def _safe_get(d: dict, key: str):
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


def _safe_div(numerator, denominator):
    if numerator is None or denominator is None:
        return None
    try:
        if denominator == 0:
            return None
        return numerator / denominator
    except (TypeError, ZeroDivisionError):
        return None


def _compute_cagr(start_value, end_value, years: int):
    if start_value is None or end_value is None or years <= 0:
        return None
    try:
        if start_value <= 0 or end_value <= 0:
            return None
        return (end_value / start_value) ** (1 / years) - 1
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _df_value(df, row_label: str, col_index: int = 0):
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    if row_label not in df.index:
        return None
    if col_index >= len(df.columns):
        return None
    try:
        val = df.loc[row_label, df.columns[col_index]]
        if pd.isna(val):
            return None
        return val
    except Exception:
        return None


def _df_values(df, row_label: str, n: int = 4):
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []
    if row_label not in df.index:
        return []
    out = []
    for i in range(min(n, len(df.columns))):
        try:
            val = df.loc[row_label, df.columns[i]]
            if not pd.isna(val):
                out.append(val)
        except Exception:
            continue
    return out


# ============ RATE-LIMIT DETECTION ============

def _looks_rate_limited(raw_metrics: dict) -> bool:
    """
    Heuristic for detecting rate-limit-induced empty data.

    Rate-limited responses typically have:
        - sector and industry both None (Yahoo's .info call returned mostly empty)
        - market_cap_usd None
        - All major metric blocks (profitability, balance_sheet, etc.) entirely null

    A genuinely loss-making or new company will usually still have:
        - sector/industry populated
        - market_cap_usd populated
        - At least some metrics populated (e.g., balance_sheet for any going concern)

    Returns True if we suspect rate limiting (caller should retry).
    """
    if not raw_metrics:
        return True

    # If we have sector AND market cap, the .info call clearly succeeded.
    if raw_metrics.get("sector") and raw_metrics.get("market_cap_usd"):
        return False

    # If both sector and market cap are missing, treat as suspicious.
    # Additionally, check if all major blocks are entirely null (very strong signal).
    blocks_to_check = ["profitability", "returns_on_capital", "cash_flow", "balance_sheet", "growth", "valuation"]
    populated_metrics = 0
    total_metrics = 0
    for block_name in blocks_to_check:
        block = raw_metrics.get(block_name) or {}
        for v in block.values():
            total_metrics += 1
            if v is not None:
                populated_metrics += 1

    # If less than 10% of all metrics are populated and sector is missing, likely rate-limited.
    if total_metrics > 0 and (populated_metrics / total_metrics) < 0.10:
        return True

    return False


# ============ CORE EXTRACTION (NO RETRY) ============

def _extract_raw_metrics_once(ticker_symbol: str) -> dict:
    """
    Single-attempt extraction of raw metrics. Internal use only — wrapped by
    extract_raw_metrics() with retry logic.
    """
    flags = []
    try:
        t = yf.Ticker(ticker_symbol)
    except Exception as e:
        flags.append(f"{ticker_symbol}: failed to initialize yfinance Ticker ({e})")
        return _empty_raw_metrics(flags)

    info = {}
    try:
        info = t.info or {}
    except Exception as e:
        flags.append(f"{ticker_symbol}: failed to fetch .info ({e})")
        info = {}

    sector = info.get("sector")
    industry = info.get("industry")
    if not sector:
        flags.append(f"{ticker_symbol}: sector classification unavailable")
    if not industry:
        flags.append(f"{ticker_symbol}: industry classification unavailable")

    try:
        fin = t.financials
    except Exception as e:
        flags.append(f"{ticker_symbol}: failed to fetch financials ({e})")
        fin = None

    try:
        cf = t.cashflow
    except Exception as e:
        flags.append(f"{ticker_symbol}: failed to fetch cashflow ({e})")
        cf = None

    try:
        bs = t.balance_sheet
    except Exception as e:
        flags.append(f"{ticker_symbol}: failed to fetch balance_sheet ({e})")
        bs = None

    # ============ PROFITABILITY ============
    gross_margin_ttm = _safe_get(info, "grossMargins")
    operating_margin_ttm = _safe_get(info, "operatingMargins")
    net_margin_ttm = _safe_get(info, "profitMargins")

    operating_margin_trend_3yr = None
    revenues_3yr = _df_values(fin, "Total Revenue", n=4)
    op_incomes_3yr = _df_values(fin, "Operating Income", n=4)
    if len(revenues_3yr) >= 4 and len(op_incomes_3yr) >= 4:
        latest_om = _safe_div(op_incomes_3yr[0], revenues_3yr[0])
        earlier_om = _safe_div(op_incomes_3yr[3], revenues_3yr[3])
        if latest_om is not None and earlier_om is not None:
            operating_margin_trend_3yr = latest_om - earlier_om

    # ============ RETURNS ON CAPITAL ============
    roe_ttm = _safe_get(info, "returnOnEquity")
    roa_ttm = _safe_get(info, "returnOnAssets")
    roic_ttm = None

    # ============ CASH FLOW ============
    free_cash_flow = _safe_get(info, "freeCashflow")
    total_revenue = _safe_get(info, "totalRevenue")
    net_income = _safe_get(info, "netIncomeToCommon") or _safe_get(info, "netIncome")

    fcf_margin = _safe_div(free_cash_flow, total_revenue)
    fcf_to_net_income = _safe_div(free_cash_flow, net_income)

    ocfs_3yr = _df_values(cf, "Operating Cash Flow", n=4)
    ocf_cagr_3yr = None
    if len(ocfs_3yr) >= 4:
        ocf_cagr_3yr = _compute_cagr(ocfs_3yr[3], ocfs_3yr[0], 3)

    # ============ BALANCE SHEET ============
    debt_to_equity = _safe_get(info, "debtToEquity")
    if debt_to_equity is not None:
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

    interest_coverage = None
    ebit = _df_value(fin, "EBIT") or _df_value(fin, "Operating Income")
    interest_expense = _df_value(fin, "Interest Expense")
    if ebit is not None and interest_expense is not None:
        try:
            ie = abs(float(interest_expense))
            if ie > 0:
                interest_coverage = ebit / ie
        except (TypeError, ValueError):
            pass

    # ============ GROWTH ============
    revenue_growth_1yr = _safe_get(info, "revenueGrowth")
    eps_growth_1yr = _safe_get(info, "earningsGrowth")

    revenue_cagr_3yr = None
    if len(revenues_3yr) >= 4:
        revenue_cagr_3yr = _compute_cagr(revenues_3yr[3], revenues_3yr[0], 3)

    net_incomes_3yr = _df_values(fin, "Net Income", n=4)
    eps_cagr_3yr = None
    if len(net_incomes_3yr) >= 4:
        eps_cagr_3yr = _compute_cagr(net_incomes_3yr[3], net_incomes_3yr[0], 3)
        flags.append(
            f"{ticker_symbol}: eps_cagr_3yr approximated by net income CAGR "
            f"(share count changes not adjusted)"
        )

    capex_3yr = _df_values(cf, "Capital Expenditure", n=4)
    fcfs_3yr = []
    for i in range(min(len(ocfs_3yr), len(capex_3yr))):
        try:
            fcfs_3yr.append(ocfs_3yr[i] + capex_3yr[i])
        except (TypeError, ValueError):
            continue
    fcf_cagr_3yr = None
    if len(fcfs_3yr) >= 4:
        fcf_cagr_3yr = _compute_cagr(fcfs_3yr[3], fcfs_3yr[0], 3)

    # ============ VALUATION ============
    pe_trailing = _safe_get(info, "trailingPE")
    pe_forward = _safe_get(info, "forwardPE")
    ps_ratio = _safe_get(info, "priceToSalesTrailing12Months")
    pb_ratio = _safe_get(info, "priceToBook")
    ev_ebitda = _safe_get(info, "enterpriseToEbitda")
    peg_ratio = _safe_get(info, "trailingPegRatio") or _safe_get(info, "pegRatio")

    market_cap = _safe_get(info, "marketCap")
    fcf_yield = _safe_div(free_cash_flow, market_cap)

    return {
        "sector": sector,
        "industry": industry,
        "market_cap_usd": market_cap,
        "profitability": {
            "gross_margin_ttm": gross_margin_ttm,
            "operating_margin_ttm": operating_margin_ttm,
            "net_margin_ttm": net_margin_ttm,
            "operating_margin_trend_3yr": operating_margin_trend_3yr,
        },
        "returns_on_capital": {
            "roe_ttm": roe_ttm,
            "roa_ttm": roa_ttm,
            "roic_ttm": roic_ttm,
        },
        "cash_flow": {
            "fcf_margin_ttm": fcf_margin,
            "fcf_to_net_income_ttm": fcf_to_net_income,
            "ocf_cagr_3yr": ocf_cagr_3yr,
        },
        "balance_sheet": {
            "debt_to_equity": debt_to_equity,
            "net_debt_to_ebitda": net_debt_to_ebitda,
            "current_ratio": current_ratio,
            "interest_coverage": interest_coverage,
            "total_debt_usd": total_debt,
            "total_cash_usd": total_cash,
        },
        "growth": {
            "revenue_growth_1yr": revenue_growth_1yr,
            "revenue_cagr_3yr": revenue_cagr_3yr,
            "eps_growth_1yr": eps_growth_1yr,
            "eps_cagr_3yr": eps_cagr_3yr,
            "fcf_cagr_3yr": fcf_cagr_3yr,
        },
        "valuation": {
            "pe_trailing": pe_trailing,
            "pe_forward": pe_forward,
            "ps_ratio": ps_ratio,
            "pb_ratio": pb_ratio,
            "ev_ebitda": ev_ebitda,
            "peg_ratio": peg_ratio,
            "fcf_yield": fcf_yield,
        },
        "data_quality_flags": flags,
    }


def _empty_raw_metrics(flags: list) -> dict:
    return {
        "sector": None,
        "industry": None,
        "market_cap_usd": None,
        "profitability": {
            "gross_margin_ttm": None,
            "operating_margin_ttm": None,
            "net_margin_ttm": None,
            "operating_margin_trend_3yr": None,
        },
        "returns_on_capital": {
            "roe_ttm": None,
            "roa_ttm": None,
            "roic_ttm": None,
        },
        "cash_flow": {
            "fcf_margin_ttm": None,
            "fcf_to_net_income_ttm": None,
            "ocf_cagr_3yr": None,
        },
        "balance_sheet": {
            "debt_to_equity": None,
            "net_debt_to_ebitda": None,
            "current_ratio": None,
            "interest_coverage": None,
            "total_debt_usd": None,
            "total_cash_usd": None,
        },
        "growth": {
            "revenue_growth_1yr": None,
            "revenue_cagr_3yr": None,
            "eps_growth_1yr": None,
            "eps_cagr_3yr": None,
            "fcf_cagr_3yr": None,
        },
        "valuation": {
            "pe_trailing": None,
            "pe_forward": None,
            "ps_ratio": None,
            "pb_ratio": None,
            "ev_ebitda": None,
            "peg_ratio": None,
            "fcf_yield": None,
        },
        "data_quality_flags": flags,
    }


# ============ PUBLIC: SYNCHRONOUS WITH RETRY ============

def extract_raw_metrics(ticker_symbol: str) -> dict:
    """
    Extract raw metrics for one company with rate-limit-aware retry.

    On each attempt:
        1. Wait politeness delay + small jitter
        2. Call _extract_raw_metrics_once
        3. Check if result looks rate-limited
        4. If yes and retries remain: wait the next backoff delay, retry
        5. If yes and retries exhausted: return what we have, with a flag
        6. If no: return the result

    The politeness delay (POLITENESS_DELAY_SECONDS) is applied before EVERY attempt
    including the first one, to throttle the overall rate of yfinance calls.
    """
    last_result = None
    attempts = len(RETRY_DELAYS) + 1  # initial + retries

    for attempt_num in range(attempts):
        # Politeness delay before every call (with small jitter to desynchronize parallel callers)
        jitter = random.uniform(0, POLITENESS_DELAY_SECONDS * 0.5)
        time.sleep(POLITENESS_DELAY_SECONDS + jitter)

        result = _extract_raw_metrics_once(ticker_symbol)
        last_result = result

        if not _looks_rate_limited(result):
            # Got real data — done
            if attempt_num > 0:
                logger.info(f"{ticker_symbol}: succeeded on retry attempt {attempt_num}")
            return result

        # Looks rate-limited
        if attempt_num < len(RETRY_DELAYS):
            wait_seconds = RETRY_DELAYS[attempt_num]
            logger.warning(
                f"{ticker_symbol}: looks rate-limited (attempt {attempt_num + 1}/{attempts}); "
                f"waiting {wait_seconds}s before retry"
            )
            time.sleep(wait_seconds)
        else:
            logger.error(
                f"{ticker_symbol}: still rate-limited after {attempts} attempts; giving up"
            )
            # Add flag so downstream can see this
            if "data_quality_flags" not in last_result:
                last_result["data_quality_flags"] = []
            last_result["data_quality_flags"].append(
                f"{ticker_symbol}: extraction gave up after {attempts} rate-limited attempts"
            )

    return last_result


async def extract_raw_metrics_async(ticker_symbol: str) -> dict:
    """Async wrapper around extract_raw_metrics. Runs the sync work (including all sleeps) in a thread."""
    return await asyncio.to_thread(extract_raw_metrics, ticker_symbol)