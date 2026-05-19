"""
Candidate Summary — Stage 4 sub-stage that produces a decision-ready summary
of each Stage 3 output for downstream consumption by Track B (and any other
LLM-based Stage 4 sub-stage).

Per-company task. One LLM call per candidate, run with concurrency control.
"""

import os
import json
import logging
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ============ LLM CONFIG ============

MODEL = "kimi-k2.6"
MAX_TOKENS = 32768


# ============ CLIENT ============

def get_client() -> AsyncOpenAI:
    """Initialize the Kimi-compatible AsyncOpenAI client (matches Stage 2/3 pattern)."""
    api_key = os.getenv("MOONSHOT_API_KEY")
    base_url = os.getenv("MOONSHOT_BASE_URL") or "https://api.moonshot.ai/v1"
    if not api_key:
        raise EnvironmentError("Missing MOONSHOT_API_KEY environment variable")
    return AsyncOpenAI(base_url=base_url, api_key=api_key)


# ============ PROMPT ASSEMBLY ============

def _format_structured_fields(candidate: dict) -> str:
    """Compact structured-fields block for the user message."""
    fields = {
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
    }
    return json.dumps(fields, indent=2, ensure_ascii=False)


def build_user_message(candidate: dict) -> str:
    """Build the user message that includes all four source documents for one company."""
    company_label = f"{candidate.get('company_name', '')} ({candidate.get('ticker', '')})"

    msg = f"""Produce a candidate summary for {company_label}.

## Structured Fields

```json
{_format_structured_fields(candidate)}
```

## Bull Scenario

{candidate.get('scenario_bull') or '(not available)'}

---

## Bear Scenario

{candidate.get('scenario_bear') or '(not available)'}

---

## Base Scenario (Final)

{candidate.get('scenario_base_final') or '(not available)'}

---

## Consolidation

{candidate.get('consolidation') or '(not available)'}
"""
    return msg


# ============ CORE FUNCTION ============

async def summarize_candidate(candidate: dict, prompt_template: str) -> dict:
    """
    Generate a candidate summary for one company.

    Returns:
        {
            "ticker": str,
            "summary": str,
            "model": str,
            "error": str | None
        }
    """
    ticker = candidate.get("ticker", "?")
    logger.info(f"Initialising Candidate Summary for {ticker}...")

    client = get_client()
    user_message = build_user_message(candidate)

    messages = [
        {"role": "system", "content": prompt_template},
        {"role": "user", "content": user_message},
    ]

    try:
        logger.info(f"Sending summary request to model for {ticker}...")
        response = await client.chat.completions.create(
            model=MODEL,
            messages=messages,
            max_tokens=MAX_TOKENS,
        )
        content = response.choices[0].message.content or ""
        logger.info(f"Summary generated for {ticker} ({len(content)} chars).")

        return {
            "ticker": ticker,
            "summary": content.strip(),
            "model": MODEL,
            "error": None,
        }

    except Exception as e:
        logger.error(f"Summary failed for {ticker}: {e}")
        return {
            "ticker": ticker,
            "summary": "",
            "model": MODEL,
            "error": str(e),
        }

    finally:
        await client.close()
        logger.info(f"Candidate Summary completed for {ticker}.\n")