"""
Macro factor analysis for Stage 4 pre-optimization.

For each candidate ticker, regress its daily returns against the daily returns of each
factor proxy to estimate beta exposures. Then compute pairwise similarity (cosine +
euclidean) between companies' beta vectors.

Pure local compute — reads cached returns from priceCache.py.
No LLM. No thresholds. Just raw evidence for downstream LLMs to interpret.
"""

import logging
import numpy as np
import pandas as pd

from priceCache import load_cached_returns

logger = logging.getLogger(__name__)

LOOKBACK_YEARS = 3
INTERVAL = "daily"

# Macro factor name -> proxy ticker.
# Must match the proxies actually cached by priceCache.py / runPriceCache.py.
FACTOR_PROXIES = {
    "interest_rates": "^TNX",
    "oil": "CL=F",
    "usd": "DX-Y.NYB",
    "housing": "XHB",
    "china": "FXI",
    "credit": "HYG",
    "geopolitical": "^VIX",
}

# Minimum observations required to compute a beta for a (company, factor) pair
MIN_OBSERVATIONS_FOR_BETA = 100


# ============ HELPERS ============

def _load_aligned_returns(tickers: list, cache_dir: str) -> pd.DataFrame:
    """
    Load returns for each ticker and align them on common dates.
    Returns a DataFrame with one column per ticker, indexed by date.
    Tickers with no cached data are silently excluded; check the returned columns
    against the input list to detect missing tickers.
    """
    returns_by_ticker = {}
    for ticker in tickers:
        r = load_cached_returns(ticker, cache_dir)
        if r.empty:
            continue
        returns_by_ticker[ticker] = r

    if not returns_by_ticker:
        return pd.DataFrame()

    df = pd.DataFrame(returns_by_ticker)
    df.index = pd.to_datetime(df.index)
    return df


def _ols_beta(y: np.ndarray, x: np.ndarray) -> tuple:
    """
    Simple OLS regression y = alpha + beta * x.
    Returns (beta, r_squared) or (None, None) on failure.
    """
    if len(y) != len(x) or len(y) < 2:
        return None, None

    # Drop NaNs in either series
    mask = ~(np.isnan(y) | np.isnan(x))
    y_clean = y[mask]
    x_clean = x[mask]

    if len(y_clean) < MIN_OBSERVATIONS_FOR_BETA:
        return None, None

    x_mean = float(np.mean(x_clean))
    y_mean = float(np.mean(y_clean))

    x_var = float(np.sum((x_clean - x_mean) ** 2))
    if x_var == 0:
        return None, None

    beta = float(np.sum((x_clean - x_mean) * (y_clean - y_mean)) / x_var)

    # R-squared
    y_pred = y_mean + beta * (x_clean - x_mean)
    ss_res = float(np.sum((y_clean - y_pred) ** 2))
    ss_tot = float(np.sum((y_clean - y_mean) ** 2))
    r_squared = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    return beta, r_squared


def _pair_key(ticker_a: str, ticker_b: str) -> str:
    """Return a canonical key for a pair of tickers (sorted, joined with _)."""
    a, b = sorted([ticker_a, ticker_b])
    return f"{a}_{b}"


def _cosine_similarity(v1: np.ndarray, v2: np.ndarray) -> float:
    """Cosine similarity between two vectors. Returns None if either is zero-magnitude."""
    n1 = float(np.linalg.norm(v1))
    n2 = float(np.linalg.norm(v2))
    if n1 == 0 or n2 == 0:
        return None
    return float(np.dot(v1, v2) / (n1 * n2))


def _euclidean_distance(v1: np.ndarray, v2: np.ndarray) -> float:
    """Euclidean (L2) distance between two vectors."""
    return float(np.linalg.norm(v1 - v2))


# ============ MAIN ENTRYPOINT ============

def compute_macro_analysis(tickers: list, cache_dir: str) -> dict:
    """
    Run macro factor analysis for the given candidate tickers.

    Returns a dict matching the agreed output schema:
        {
            "lookback_years": 3,
            "interval": "daily",
            "factor_proxies": {"interest_rates": "^TNX", ...},
            "per_company_betas": {
                "MA": {"interest_rates": -0.32, "oil": 0.05, ...},
                ...
            },
            "pairwise_similarity": {
                "cosine": {"MA_V": 0.92, ...},
                "euclidean": {"MA_V": 0.18, ...}
            },
            "data_quality_flags": [...]
        }
    """
    flags = []

    # Load returns for all tickers (companies + factor proxies)
    factor_tickers = list(FACTOR_PROXIES.values())
    all_returns_tickers = tickers + factor_tickers
    df = _load_aligned_returns(all_returns_tickers, cache_dir)

    if df.empty:
        flags.append("No returns data available for any ticker; aborting")
        return {
            "lookback_years": LOOKBACK_YEARS,
            "interval": INTERVAL,
            "factor_proxies": FACTOR_PROXIES,
            "per_company_betas": {},
            "pairwise_similarity": {"cosine": {}, "euclidean": {}},
            "data_quality_flags": flags,
        }

    # Note missing tickers
    missing_companies = [t for t in tickers if t not in df.columns]
    missing_factors = [t for t in factor_tickers if t not in df.columns]
    for t in missing_companies:
        flags.append(f"{t}: no cached returns; excluded from regression")
    for t in missing_factors:
        # Find factor name for nicer message
        factor_name = next((fn for fn, fp in FACTOR_PROXIES.items() if fp == t), t)
        flags.append(
            f"Factor '{factor_name}' (proxy={t}): no cached returns; "
            f"all per-company betas for this factor will be None"
        )

    # For each company present, compute a beta against each factor proxy present
    per_company_betas = {}

    for ticker in tickers:
        if ticker not in df.columns:
            # Already flagged; skip with empty beta vector
            per_company_betas[ticker] = {name: None for name in FACTOR_PROXIES.keys()}
            continue

        company_returns = df[ticker].values

        betas = {}
        for factor_name, factor_ticker in FACTOR_PROXIES.items():
            if factor_ticker not in df.columns:
                betas[factor_name] = None
                continue

            factor_returns = df[factor_ticker].values
            beta, r_squared = _ols_beta(company_returns, factor_returns)

            if beta is None:
                betas[factor_name] = None
                flags.append(
                    f"{ticker} vs {factor_name}: regression failed "
                    f"(insufficient data after NaN alignment)"
                )
            else:
                betas[factor_name] = round(beta, 4)

        per_company_betas[ticker] = betas

    # Compute pairwise similarity from beta vectors
    # Only between companies that have all factor betas populated (no Nones)
    company_vectors = {}
    factor_order = list(FACTOR_PROXIES.keys())

    for ticker, betas in per_company_betas.items():
        vector_values = [betas.get(fn) for fn in factor_order]
        if any(v is None for v in vector_values):
            continue
        company_vectors[ticker] = np.array(vector_values, dtype=float)

    cosine_pairs = {}
    euclidean_pairs = {}

    company_tickers_with_vectors = sorted(company_vectors.keys())
    n = len(company_tickers_with_vectors)
    for i in range(n):
        for j in range(i + 1, n):
            ticker_a = company_tickers_with_vectors[i]
            ticker_b = company_tickers_with_vectors[j]
            v1 = company_vectors[ticker_a]
            v2 = company_vectors[ticker_b]

            key = _pair_key(ticker_a, ticker_b)

            cos = _cosine_similarity(v1, v2)
            cosine_pairs[key] = round(cos, 4) if cos is not None else None

            euc = _euclidean_distance(v1, v2)
            euclidean_pairs[key] = round(euc, 4)

    if n < len(tickers):
        flags.append(
            f"Pairwise similarity computed on {n} of {len(tickers)} companies "
            f"(others had missing factor betas)"
        )

    return {
        "lookback_years": LOOKBACK_YEARS,
        "interval": INTERVAL,
        "factor_proxies": FACTOR_PROXIES,
        "per_company_betas": per_company_betas,
        "pairwise_similarity": {
            "cosine": cosine_pairs,
            "euclidean": euclidean_pairs,
        },
        "data_quality_flags": flags,
    }