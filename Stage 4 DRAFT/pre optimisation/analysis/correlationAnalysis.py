"""
Correlation analysis for Stage 4 pre-optimization.

Reads cached daily prices for the candidate tickers, computes the full pairwise
Pearson correlation matrix from daily returns. No thresholds, no flagged pairs —
just surface the raw matrix for downstream LLMs to interpret.
"""

import logging
import pandas as pd
import numpy as np

from priceCache import load_cached_returns

logger = logging.getLogger(__name__)

LOOKBACK_YEARS = 3
INTERVAL = "daily"


def compute_correlation_matrix(tickers: list, cache_dir: str) -> dict:
    """
    Compute the pairwise Pearson correlation matrix for a list of tickers.

    Returns a dict matching the agreed output schema:
        {
            "analysis_date": ...,                  ← set by caller
            "lookback_years": 3,
            "interval": "daily",
            "n_observations": int,
            "tickers": [...],
            "correlation_matrix": {
                "MA": {"MA": 1.0, "V": 0.85, ...},
                ...
            },
            "data_quality_flags": [...]
        }
    """
    flags = []

    # Load returns for each ticker
    returns_by_ticker = {}
    for ticker in tickers:
        returns = load_cached_returns(ticker, cache_dir)
        if returns.empty:
            flags.append(f"{ticker}: no cached returns available; excluded from correlation")
            continue
        returns_by_ticker[ticker] = returns

    if not returns_by_ticker:
        return {
            "lookback_years": LOOKBACK_YEARS,
            "interval": INTERVAL,
            "n_observations": 0,
            "tickers": [],
            "correlation_matrix": {},
            "data_quality_flags": flags + ["No tickers had cached returns; matrix is empty"],
        }

    # Build a DataFrame with one column per ticker, indexed by date
    # pandas will align on the common dates via outer join then we drop incomplete rows
    df = pd.DataFrame(returns_by_ticker)
    df.index = pd.to_datetime(df.index)

    # Use only dates where ALL tickers have a return (inner join behavior)
    # This ensures the correlation is computed on a consistent observation window
    df_aligned = df.dropna(how="any")
    n_observations = len(df_aligned)

    if n_observations < 30:
        flags.append(
            f"Only {n_observations} dates where all tickers have returns; "
            f"correlations may be unreliable"
        )

    # Compute pairwise Pearson correlation
    corr_df = df_aligned.corr(method="pearson")

    # Note tickers with low individual coverage
    for ticker in tickers:
        if ticker not in returns_by_ticker:
            continue
        ticker_obs = len(returns_by_ticker[ticker])
        if ticker_obs < n_observations * 0.5 and ticker_obs > 0:
            flags.append(
                f"{ticker}: only {ticker_obs} returns available "
                f"(vs aligned window of {n_observations})"
            )

    # Convert correlation DataFrame to nested dict
    correlation_matrix = {}
    for ticker_a in corr_df.index:
        correlation_matrix[ticker_a] = {}
        for ticker_b in corr_df.columns:
            val = corr_df.at[ticker_a, ticker_b]
            if pd.isna(val):
                correlation_matrix[ticker_a][ticker_b] = None
            else:
                correlation_matrix[ticker_a][ticker_b] = round(float(val), 4)

    return {
        "lookback_years": LOOKBACK_YEARS,
        "interval": INTERVAL,
        "n_observations": n_observations,
        "tickers": list(corr_df.index),
        "correlation_matrix": correlation_matrix,
        "data_quality_flags": flags,
    }