"""
Single source of truth for constructing the LLM client used across the
pipeline. Backed either by direct Moonshot or by OpenRouter, selected via
the BACKEND constant below.

Why a helper: the chat-completions call signature is identical across
backends because both speak the OpenAI-compatible schema; only the
base_url, api_key, and model id change. Centralising those three values
means every stage flips backends together with a one-line code edit, with
no risk of half-migrated state.

To switch providers: change BACKEND below, commit, redeploy. API keys
still come from env vars (so secrets are never committed); only the
provider selection and the per-provider model id live in this file.
"""

import os
from openai import AsyncOpenAI


# ============ BACKEND TOGGLE ============
# Flip this single line to swap providers across the entire pipeline.
# Valid values: "moonshot" (direct), "openrouter".
BACKEND = "openrouter"

# Model id used for each backend. The Moonshot slug is Moonshot's native
# id; the OpenRouter slug should be verified against their catalogue
# before flipping (likely `moonshotai/kimi-k2-thinking` or similar).
MODEL_BY_BACKEND = {
    "moonshot":   "kimi-k2.6",
    "openrouter": "moonshotai/kimi-k2.6",
}

# Base URL for each backend. Moonshot's base URL is overridable via env
# for parity with today's behaviour; OpenRouter's is fixed.
_BASE_URL_BY_BACKEND = {
    "moonshot":   lambda: os.getenv("MOONSHOT_BASE_URL") or "https://api.moonshot.ai/v1",
    "openrouter": lambda: "https://openrouter.ai/api/v1",
}

# Env var holding the API key for each backend.
_API_KEY_ENV_BY_BACKEND = {
    "moonshot":   "MOONSHOT_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


def get_llm_client(*, max_retries: int | None = None) -> tuple[AsyncOpenAI, str]:
    """Return (AsyncOpenAI client, model id) for the BACKEND set above.

    The returned tuple is unpacked at each call site:

        client, model = get_llm_client(max_retries=5)
        response = await client.chat.completions.create(
            model=model, messages=..., max_tokens=...
        )
    """
    if BACKEND not in MODEL_BY_BACKEND:
        raise ValueError(
            f"Unknown BACKEND={BACKEND!r} in llm_client.py; "
            f"must be one of {sorted(MODEL_BY_BACKEND)}"
        )

    api_key_env = _API_KEY_ENV_BY_BACKEND[BACKEND]
    api_key = os.getenv(api_key_env)
    if not api_key:
        raise EnvironmentError(
            f"BACKEND={BACKEND!r} but {api_key_env} is not set"
        )

    base_url = _BASE_URL_BY_BACKEND[BACKEND]()
    model = MODEL_BY_BACKEND[BACKEND]

    kwargs = {"base_url": base_url, "api_key": api_key}
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    return AsyncOpenAI(**kwargs), model
