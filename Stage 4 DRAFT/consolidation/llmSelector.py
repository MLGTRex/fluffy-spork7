"""
Consolidation LLM Selector (functional).

Calls the LLM to choose 15 final names from the union of Track A and Track B picks.
The LLM does not assign allocations — that's the allocator's job. It just picks names.

If the allocator reports infeasibility, this module supports a retry call that
includes the previous selection + violation feedback so the LLM can try again.
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

# ============ LLM CONFIG ============

MAX_TOKENS = 32768

PORTFOLIO_SIZE = 15


# ============ LLM CALL (STREAMING) ============

async def _call_llm(client: AsyncOpenAI, model: str, system_prompt: str, user_message: str) -> str:
    """Single LLM call with streaming, returns concatenated response text."""
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


# ============ PROMPT BUILDING ============

def _format_candidate_for_prompt(candidate: dict, summary_text: str) -> str:
    """One candidate's context block."""
    structured = {
        "ticker": candidate.get("ticker"),
        "company_name": candidate.get("company_name"),
        "sector": candidate.get("sector"),
        "industry": candidate.get("industry"),
        "conviction": candidate.get("conviction"),
        "expected_return_12m": candidate.get("expected_return_12m"),
        "base_return_12m": candidate.get("base_return_12m"),
        "upside_return_12m": candidate.get("upside_return_12m"),
        "downside_return_12m": candidate.get("downside_return_12m"),
        "scenario_probability_bull": candidate.get("scenario_probability_bull"),
        "scenario_probability_base": candidate.get("scenario_probability_base"),
        "scenario_probability_bear": candidate.get("scenario_probability_bear"),
        "key_invalidation_triggers": candidate.get("key_invalidation_triggers"),
    }
    block = f"\n### {candidate['ticker']} ({candidate.get('company_name', '')})\n\n"
    block += "**Structured fields:**\n```json\n"
    block += json.dumps(structured, indent=2, ensure_ascii=False)
    block += "\n```\n\n"
    block += "**Summary:**\n\n" + (summary_text or "(summary unavailable)") + "\n\n"
    return block


def _format_track_portfolio(track_label: str, track_data: dict) -> str:
    """Render one track's portfolio (Track A or B) as a readable block."""
    block = f"\n## Track {track_label} Portfolio\n\n"
    method = track_data.get("method", "")
    if method:
        block += f"_Method: {method}_\n\n"

    # Try both shapes:
    #   Track A: positions at top level
    #   Track B: positions nested under 'portfolio'
    portfolio_section = track_data.get("portfolio") or {}
    positions = portfolio_section.get("positions") or track_data.get("positions", [])

    block += "**Positions:**\n\n"
    for p in positions:
        ticker = p.get("ticker", "?")
        weight = p.get("allocation_pct", 0)
        rationale = p.get("rationale", "")
        if rationale:
            block += f"- **{ticker}** ({weight:.2f}%): {rationale}\n"
        else:
            block += f"- **{ticker}** ({weight:.2f}%)\n"

    rejections = portfolio_section.get("notable_rejections", [])
    if rejections:
        block += "\n**Notable rejections:**\n\n"
        for r in rejections:
            block += f"- **{r.get('ticker', '?')}**: {r.get('rationale', '')}\n"

    thesis = portfolio_section.get("portfolio_thesis")
    if thesis:
        block += f"\n**Portfolio thesis:** {thesis}\n"

    key_risks = portfolio_section.get("key_risks", [])
    if key_risks:
        block += "\n**Key risks:**\n"
        for k in key_risks:
            block += f"- {k}\n"

    return block


def build_user_message(
    union_candidates: list,
    summaries_by_ticker: dict,
    track_a: dict,
    track_b: dict,
    pre_opt: dict,
) -> str:
    """Initial user message for the first selection attempt."""
    parts = [
        f"Compare the two portfolios below and produce a final consolidated 15-name selection.\n",
        f"\n## Candidate Universe (union of Track A + Track B picks)\n",
        f"\n{len(union_candidates)} unique tickers across both tracks.\n",
    ]
    for c in union_candidates:
        summary_text = summaries_by_ticker.get(c["ticker"], "")
        parts.append(_format_candidate_for_prompt(c, summary_text))

    parts.append(_format_track_portfolio("A", track_a))
    parts.append(_format_track_portfolio("B", track_b))

    parts.append("\n## Pre-Optimization Data\n\n```json\n")
    parts.append(json.dumps(pre_opt, indent=2, ensure_ascii=False))
    parts.append("\n```\n")

    return "".join(parts)


def build_retry_user_message(
    initial_user_message: str,
    previous_response: str,
    violation_reason: str,
) -> str:
    """Retry message: previous attempt + specific allocator failure."""
    feedback = initial_user_message + "\n\n## Your Previous Selection\n\n"
    feedback += "```\n" + previous_response + "\n```\n\n"
    feedback += "## Allocator Feedback\n\nYour previous selection caused the allocator to fail:\n\n"
    feedback += f"> {violation_reason}\n\n"
    feedback += (
        "Please revise your selection to fix this problem. "
        "Respond with the corrected JSON in a ```json fenced code block."
    )
    return feedback


# ============ PARSING ============

def parse_json_from_llm_response(text: str) -> dict:
    """Extract JSON object from response. Raises ValueError on failure."""
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

def validate_selection(parsed: dict, union_tickers: set) -> list:
    """
    Basic validation: exactly 15 unique tickers, all from the union pool.
    Returns list of violation messages (empty if valid).
    Does NOT check sector cap — that's the allocator's job.
    """
    violations = []

    if not isinstance(parsed, dict):
        return ["Output is not a JSON object"]

    tickers = parsed.get("selected_tickers")
    if not isinstance(tickers, list):
        return ["'selected_tickers' is missing or not a list"]

    if len(tickers) != PORTFOLIO_SIZE:
        violations.append(
            f"Number of selected tickers = {len(tickers)}; must be exactly {PORTFOLIO_SIZE}"
        )

    seen = set()
    for i, t in enumerate(tickers):
        if not isinstance(t, str):
            violations.append(f"Selected ticker {i+1} is not a string: {t}")
            continue
        if t in seen:
            violations.append(f"Duplicate ticker in selection: {t}")
            continue
        seen.add(t)
        if t not in union_tickers:
            violations.append(
                f"Ticker {t} is not in the union pool of Track A + Track B picks"
            )

    return violations


# ============ TOP-LEVEL ENTRY ============

async def select_consolidation_portfolio(
    union_candidates: list,
    summaries_by_ticker: dict,
    track_a: dict,
    track_b: dict,
    pre_opt: dict,
    system_prompt: str,
    previous_response: str = None,
    violation_reason: str = None,
) -> dict:
    """
    Make a single selection call. The caller handles the feedback loop.

    Returns:
        {
            "raw_response": str,
            "parsed": dict (may be None on parse failure),
            "selection_violations": list of strings (empty if structurally valid),
            "model": str,
        }
    """
    client, model = get_llm_client()
    try:
        initial = build_user_message(
            union_candidates, summaries_by_ticker, track_a, track_b, pre_opt
        )

        if previous_response is not None and violation_reason is not None:
            user_message = build_retry_user_message(
                initial, previous_response, violation_reason
            )
            logger.info(f"Sending consolidation retry request ({len(user_message)} chars)...")
        else:
            user_message = initial
            logger.info(f"Sending consolidation initial request ({len(user_message)} chars)...")

        response = await _call_llm(client, model, system_prompt, user_message)
        logger.info(f"Consolidation response received ({len(response)} chars).")

        parsed = None
        violations = []
        try:
            parsed = parse_json_from_llm_response(response)
            union_tickers = {c["ticker"] for c in union_candidates}
            violations = validate_selection(parsed, union_tickers)
        except ValueError as e:
            violations = [f"JSON parse error: {e}"]

        return {
            "raw_response": response,
            "parsed": parsed,
            "selection_violations": violations,
            "model": model,
        }
    finally:
        await client.close()