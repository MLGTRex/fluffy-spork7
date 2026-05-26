"""
Single source of truth for the LLM client used by plain (non-tool-using)
calls across the pipeline. All such calls go through OpenRouter using
Kimi K2.6.

Tool-using calls (Stage 2 deep research, Stage 5 monitor Call 1) bypass
this helper and talk to Moonshot directly via the shared FormulaChatClient
(`moonshot_formula_client.py`), because OpenRouter does not proxy
Moonshot's /formulas/{uri}/tools or /fibers endpoints.
"""

import os
from openai import AsyncOpenAI

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MODEL = "moonshotai/kimi-k2.6"


def get_llm_client(*, max_retries: int | None = None) -> tuple[AsyncOpenAI, str]:
    """Return (AsyncOpenAI client, model id) for OpenRouter Kimi K2.6.

    The returned tuple is unpacked at each call site:

        client, model = get_llm_client(max_retries=5)
        response = await client.chat.completions.create(
            model=model, messages=..., max_tokens=...
        )
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise EnvironmentError("OPENROUTER_API_KEY is not set")

    kwargs = {"base_url": OPENROUTER_BASE_URL, "api_key": api_key}
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    return AsyncOpenAI(**kwargs), MODEL
