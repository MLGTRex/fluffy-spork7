"""
Track B — Pure LLM Portfolio Construction (functional).

Uses candidate_summaries.json as the per-company context (NOT the raw Stage 3
narratives — those are too large for the model's context window). Adds
pre-optimization data (correlation, sector, macro) for portfolio-level reasoning.

Constraint enforcement is via the prompt + a Python validator that retries the
LLM call once if the first output violates any constraints.
"""

import os
import re
import sys
import json
import logging
from openai import AsyncOpenAI
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pipeline tools"))
from llm_client import get_llm_client

load_dotenv()

logger = logging.getLogger(__name__)

# ============ HARD CONSTRAINTS ============

PORTFOLIO_SIZE = 15
MIN_POSITION_WEIGHT = 3.0
MAX_POSITION_WEIGHT = 20.0
SECTOR_CAP = 35.0
WEIGHT_SUM_TOLERANCE = 0.5  # allocations must sum to 100 ± 0.5

# ============ LLM CONFIG ============

MAX_TOKENS = 32768


# ============ PROMPT BUILDING ============

def format_candidate_for_prompt(candidate: dict, summary_text: str, idx: int) -> str:
    """Build the per-candidate context block: structured fields + the pre-computed summary."""
    structured = {
        "ticker": candidate["ticker"],
        "company_name": candidate["company_name"],
        "sector": candidate["sector"],
        "industry": candidate["industry"],
        "conviction": candidate["conviction"],
        "expected_return_12m": candidate["expected_return_12m"],
        "base_return_12m": candidate["base_return_12m"],
        "upside_return_12m": candidate["upside_return_12m"],
        "downside_return_12m": candidate["downside_return_12m"],
        "scenario_probability_bull": candidate["scenario_probability_bull"],
        "scenario_probability_base": candidate["scenario_probability_base"],
        "scenario_probability_bear": candidate["scenario_probability_bear"],
        "key_invalidation_triggers": candidate["key_invalidation_triggers"],
    }

    block = f"\n### Candidate {idx}: {candidate['ticker']} ({candidate['company_name']})\n\n"
    block += "**Structured fields:**\n```json\n"
    block += json.dumps(structured, indent=2, ensure_ascii=False)
    block += "\n```\n\n"
    block += "**Summary:**\n\n" + (summary_text or "(summary unavailable)") + "\n\n"

    return block


def build_user_message(candidates: list, summaries_by_ticker: dict, pre_opt: dict) -> str:
    """Build the user message: candidate blocks + pre-optimization JSON."""
    parts = [
        f"Construct a 15-position portfolio from the following {len(candidates)} candidates.\n"
    ]

    parts.append("\n## Candidate Universe\n")
    for i, c in enumerate(candidates, start=1):
        summary_text = summaries_by_ticker.get(c["ticker"], "")
        parts.append(format_candidate_for_prompt(c, summary_text, i))

    parts.append("\n## Pre-Optimization Data\n\n```json\n")
    parts.append(json.dumps(pre_opt, indent=2, ensure_ascii=False))
    parts.append("\n```\n")

    return "".join(parts)


def build_retry_user_message(initial_user_message: str, previous_response: str, violations: list) -> str:
    """Append previous output and violation feedback to the initial user message."""
    feedback = initial_user_message + "\n\n## Your Previous Output\n\n"
    feedback += "```\n" + previous_response + "\n```\n\n"
    feedback += "## Validation Errors\n\nYour previous output violated the following constraints:\n\n"
    for v in violations:
        feedback += f"- {v}\n"
    feedback += (
        "\nPlease produce a corrected JSON output that satisfies ALL hard constraints. "
        "Respond with only the corrected JSON in a ```json fenced code block."
    )
    return feedback


# ============ PARSING ============

def parse_json_from_llm_response(text: str) -> dict:
    """
    Extract the JSON object from the LLM response.
    Expects a ```json fenced block; falls back to bare JSON detection.
    Returns the parsed dict or raises ValueError.
    """
    if not text:
        raise ValueError("Empty response from LLM")

    fence_match = re.search(r"```json\s*\n(.*?)\n```", text, re.DOTALL | re.IGNORECASE)
    if fence_match:
        json_str = fence_match.group(1).strip()
    else:
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start == -1 or brace_end == -1 or brace_end <= brace_start:
            raise ValueError("No JSON object found in response")
        json_str = text[brace_start:brace_end + 1]

    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON parse error: {e}")


# ============ VALIDATION ============

def validate_portfolio(parsed: dict, candidates: list) -> list:
    """
    Check the parsed LLM output against hard constraints.
    Returns a list of violation messages (empty if valid).
    """
    violations = []

    if not isinstance(parsed, dict):
        return ["Output is not a JSON object"]

    positions = parsed.get("positions")
    if not isinstance(positions, list):
        return ["'positions' is missing or not a list"]

    if len(positions) != PORTFOLIO_SIZE:
        violations.append(
            f"Number of positions = {len(positions)}; must be exactly {PORTFOLIO_SIZE}"
        )

    eligible_tickers = set()
    ineligible_with_reason = {}
    for c in candidates:
        ticker = c.get("ticker")
        base_ret = c.get("base_return_12m")
        if base_ret is None:
            ineligible_with_reason[ticker] = "missing base_return_12m"
        elif base_ret <= 0:
            ineligible_with_reason[ticker] = f"base_return_12m={base_ret:.4f} <= 0"
        else:
            eligible_tickers.add(ticker)

    ticker_to_sector = {c["ticker"]: c.get("sector") for c in candidates}

    seen_tickers = set()
    total_weight = 0.0
    sector_totals = {}

    for i, p in enumerate(positions):
        if not isinstance(p, dict):
            violations.append(f"Position {i+1} is not a dict")
            continue

        ticker = p.get("ticker")
        weight = p.get("allocation_pct")

        if not ticker:
            violations.append(f"Position {i+1}: missing 'ticker'")
            continue

        if ticker in seen_tickers:
            violations.append(f"Position {i+1}: duplicate ticker {ticker}")
            continue
        seen_tickers.add(ticker)

        if ticker in ineligible_with_reason:
            violations.append(
                f"Position {i+1} ({ticker}): not eligible — {ineligible_with_reason[ticker]}"
            )
            continue

        if ticker not in eligible_tickers:
            violations.append(
                f"Position {i+1} ({ticker}): not in the candidate universe"
            )
            continue

        if weight is None:
            violations.append(f"Position {i+1} ({ticker}): missing 'allocation_pct'")
            continue
        try:
            weight = float(weight)
        except (TypeError, ValueError):
            violations.append(
                f"Position {i+1} ({ticker}): allocation_pct is not a number ({weight})"
            )
            continue

        if weight < MIN_POSITION_WEIGHT - 0.01 or weight > MAX_POSITION_WEIGHT + 0.01:
            violations.append(
                f"Position {i+1} ({ticker}): allocation_pct={weight:.2f} "
                f"outside allowed range [{MIN_POSITION_WEIGHT}, {MAX_POSITION_WEIGHT}]"
            )

        total_weight += weight

        sector = ticker_to_sector.get(ticker)
        if sector:
            sector_totals[sector] = sector_totals.get(sector, 0.0) + weight

    if abs(total_weight - 100.0) > WEIGHT_SUM_TOLERANCE:
        violations.append(
            f"Total allocation_pct sums to {total_weight:.2f}; must be 100.00 "
            f"(tolerance ±{WEIGHT_SUM_TOLERANCE})"
        )

    for sector, total in sector_totals.items():
        if total > SECTOR_CAP + 0.01:
            violations.append(
                f"Sector '{sector}' total allocation = {total:.2f}%; "
                f"exceeds cap of {SECTOR_CAP}%"
            )

    return violations


# ============ LLM CALL ============

async def _call_llm(client: AsyncOpenAI, model: str, system_prompt: str, user_message: str) -> str:
    """
    Single LLM call returning the response text.

    Uses streaming (stream=True) to avoid the read-timeout wall that hits on
    long-running calls with large prompts. With streaming, the server starts
    sending chunks as soon as generation begins; the client doesn't have to
    wait for the entire response before receiving any data. This eliminates
    the 'waited too long for response headers' connection failures.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    stream = await client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=MAX_TOKENS,
        stream=True,
    )

    content_parts = []
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta and delta.content:
            content_parts.append(delta.content)

    return "".join(content_parts)


# ============ TOP-LEVEL ENTRY ============

async def construct_track_b_portfolio(
    candidates: list,
    summaries_by_ticker: dict,
    pre_opt: dict,
    system_prompt: str,
) -> dict:
    """
    Run the full Track B pipeline: build prompt -> call LLM -> validate -> retry if needed.

    Returns:
        {
            "constraint_violations": list (empty if valid),
            "portfolio": parsed dict (may be partial if violations remain),
            "model": str,
            "raw_response_attempt_1": str,
            "raw_response_attempt_2": str | None,
        }
    """
    logger.info("Initialising Track B portfolio construction...")

    client, model = get_llm_client()

    try:
        user_message = build_user_message(candidates, summaries_by_ticker, pre_opt)
        logger.info(f"Built initial user message ({len(user_message)} chars).")

        # First attempt
        logger.info("Sending initial Track B request to model...")
        response_1 = await _call_llm(client, model, system_prompt, user_message)
        logger.info(f"Initial response received ({len(response_1)} chars).")

        parsed_1 = None
        violations_1 = []
        try:
            parsed_1 = parse_json_from_llm_response(response_1)
            violations_1 = validate_portfolio(parsed_1, candidates)
        except ValueError as e:
            violations_1 = [f"JSON parse error: {e}"]

        if not violations_1:
            logger.info("First attempt is valid — no constraint violations.")
            return {
                "constraint_violations": [],
                "portfolio": parsed_1,
                "model": model,
                "raw_response_attempt_1": response_1,
                "raw_response_attempt_2": None,
            }

        logger.warning(f"First attempt has {len(violations_1)} violation(s); retrying with feedback.")
        for v in violations_1:
            logger.warning(f"  - {v}")

        # Retry
        retry_user_message = build_retry_user_message(user_message, response_1, violations_1)
        logger.info("Sending retry Track B request to model...")
        response_2 = await _call_llm(client, model, system_prompt, retry_user_message)
        logger.info(f"Retry response received ({len(response_2)} chars).")

        parsed_2 = None
        violations_2 = []
        try:
            parsed_2 = parse_json_from_llm_response(response_2)
            violations_2 = validate_portfolio(parsed_2, candidates)
        except ValueError as e:
            violations_2 = [f"JSON parse error on retry: {e}"]

        if not violations_2:
            logger.info("Retry attempt is valid.")
            return {
                "constraint_violations": [],
                "portfolio": parsed_2,
                "model": model,
                "raw_response_attempt_1": response_1,
                "raw_response_attempt_2": response_2,
            }

        logger.error(f"Retry attempt still has {len(violations_2)} violation(s); saving with flags.")
        for v in violations_2:
            logger.error(f"  - {v}")

        return {
            "constraint_violations": violations_2,
            "portfolio": parsed_2 if parsed_2 is not None else parsed_1,
            "model": model,
            "raw_response_attempt_1": response_1,
            "raw_response_attempt_2": response_2,
        }

    finally:
        await client.close()
        logger.info("Track B portfolio construction completed.\n")