"""
Sector analysis for Stage 4 pre-optimization.

Reads sector/industry data from Stage 3 per-company JSONs and produces:
    - ticker -> {sector, industry} dict
    - sector breakdown with count, tickers, share of universe

Pure data lookup — no API calls, no computation beyond grouping.
"""

import logging
import os
import json
import re

logger = logging.getLogger(__name__)


def extract_ticker_from_company_name(company_name: str) -> str:
    """Extract ticker from a string like 'Mastercard (MA)' -> 'MA'."""
    if not company_name:
        return None
    match = re.search(r"\(([A-Z0-9\-\.]+)\)\s*$", company_name)
    return match.group(1) if match else None


def _get_sector_industry_from_stage3(data: dict) -> tuple:
    """
    Extract sector and industry from a Stage 3 per-company JSON.
    Looks in valuation_metrics block first (Stage 3a's output), falls back
    to top-level sector/industry fields if present.
    Returns (sector, industry) tuple; either may be None.
    """
    if not data:
        return None, None

    # Stage 3a's valuation_metrics block is the primary source
    vm = data.get("valuation_metrics") or {}
    if isinstance(vm, dict):
        sector = vm.get("sector")
        industry = vm.get("industry")
        if sector or industry:
            return sector, industry

    # Fall back to top-level fields
    sector = data.get("sector")
    industry = data.get("industry")
    return sector, industry


def compute_sector_analysis(stage3_output_dir: str) -> dict:
    """
    Read all per-company JSONs from Stage 3 output directory.
    Returns a dict matching the agreed output schema:
        {
            "analysis_date": ...,        ← set by caller
            "tickers_to_sector": {
                "MA": {"sector": "Financial Services", "industry": "Credit Services"},
                ...
            },
            "sector_breakdown": {
                "Technology": {"count": 12, "tickers": [...], "share_of_universe": 0.24},
                ...
            }
        }
    """
    flags = []

    if not os.path.isdir(stage3_output_dir):
        logger.error(f"Stage 3 output directory not found: {stage3_output_dir}")
        return {
            "tickers_to_sector": {},
            "sector_breakdown": {},
            "data_quality_flags": [f"Stage 3 output directory not found: {stage3_output_dir}"],
        }

    tickers_to_sector = {}

    for fname in sorted(os.listdir(stage3_output_dir)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(stage3_output_dir, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            flags.append(f"Could not read {fname}: {e}")
            continue

        # Extract ticker
        ticker = data.get("ticker")
        if not ticker:
            company_name = data.get("company_name", "")
            ticker = extract_ticker_from_company_name(company_name)
        if not ticker:
            flags.append(f"Could not extract ticker from {fname}")
            continue

        sector, industry = _get_sector_industry_from_stage3(data)
        if not sector and not industry:
            flags.append(f"{ticker}: no sector/industry data in Stage 3 JSON")

        tickers_to_sector[ticker] = {
            "sector": sector,
            "industry": industry,
        }

    # Build sector breakdown
    total = len(tickers_to_sector)
    sector_breakdown = {}

    for ticker, info in tickers_to_sector.items():
        sector = info.get("sector") or "Unknown"
        if sector not in sector_breakdown:
            sector_breakdown[sector] = {
                "count": 0,
                "tickers": [],
                "share_of_universe": 0.0,
            }
        sector_breakdown[sector]["count"] += 1
        sector_breakdown[sector]["tickers"].append(ticker)

    # Compute shares now that counts are final
    if total > 0:
        for sector_data in sector_breakdown.values():
            sector_data["share_of_universe"] = round(sector_data["count"] / total, 4)

    return {
        "tickers_to_sector": tickers_to_sector,
        "sector_breakdown": sector_breakdown,
        "data_quality_flags": flags,
    }