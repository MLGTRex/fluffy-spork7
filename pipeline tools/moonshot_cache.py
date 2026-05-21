"""
Moonshot/Kimi K2.6 prefix-cache helpers.

Kimi K2.6 supports automatic prefix caching: any time consecutive requests share
an identical message prefix, the cached portion is billed at ~75% off ($0.15/M
vs $0.60/M input). There is no API to create or delete a cache — the provider
handles it transparently. The only thing the caller controls is whether the
prefix actually matches across calls.

For this pipeline, that means restructuring messages so the large, static
research dump for a given company sits in a position that's byte-identical
across every agent call for that company:

    [system: <generic preamble> + <research_dump>]    ← identical across calls
    [user:   <per-agent role + per-call task + dynamic context>]    ← varies

The per-agent prompt content (bull_case.md, scenario_bull.md, etc.) moves from
the `system` role into the `user` message — byte-identical wording, only the
role label changes. This is the minimal structural change required for the
cache key to match across all agents within a single company's chain.

When USE_MOONSHOT_CACHE is set to "false", every helper here falls back to the
legacy behaviour (per-agent system message, research dump inline in user
message) so the change can be toggled off without redeploying.
"""

import os
import logging

CACHE_FLAG_ENV = "USE_MOONSHOT_CACHE"


def is_cache_enabled() -> bool:
    """True if the cache-friendly message structure should be used."""
    raw = os.getenv(CACHE_FLAG_ENV, "true").strip().lower()
    return raw not in ("false", "0", "no", "off", "")


def _company_preamble(company_name: str) -> str:
    """
    Generic, company-stable preamble that opens every cached system message.

    Identical wording across all agents for the same company so the cache key
    matches. The company name is the only variable.
    """
    return (
        f"You are a senior investment analyst working on {company_name}. "
        "The complete research dossier for this company is provided below. "
        "Treat it as your authoritative source of information for any task you "
        "are asked to perform in the messages that follow."
    )


def build_cached_system_content(company_name: str, research_dump: str) -> str:
    """
    Build the system message that contains the cacheable prefix.

    Keep the format byte-stable: any change to whitespace, headers, or order
    will silently invalidate cache hits.
    """
    preamble = _company_preamble(company_name)
    return (
        f"{preamble}\n\n"
        "# RESEARCH DOSSIER\n\n"
        f"{research_dump}"
    )


def build_cache_friendly_messages(
    company_name: str,
    research_dump: str,
    role_content: str,
    task_content: str,
) -> list[dict]:
    """
    Canonical 2-message structure that maximises cache hits.

    Args:
        company_name: target company; goes into the company-stable preamble.
        research_dump: large static content that should be cached. Sits in
            the system message so it's at the prefix.
        role_content: the per-agent prompt file content (bull_case.md,
            scenario_bull.md, etc.) — byte-identical to today, just delivered
            under the `user` role instead of `system`.
        task_content: per-call dynamic content. For cases this is the task
            instruction; for rebuttals this includes own/opposing case;
            for arbitration this includes all prior scenario outputs.

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    system_content = build_cached_system_content(company_name, research_dump)
    user_content = (
        "Your specific role and instructions for this task are described "
        "below. Treat these as your binding instructions and follow them "
        "rigorously.\n\n"
        "---\n\n"
        f"{role_content}\n\n"
        "---\n\n"
        f"{task_content}"
    )
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


def build_legacy_messages(system_prompt: str, user_message: str) -> list[dict]:
    """Today's structure — used when caching is disabled via the flag."""
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]


def extract_cache_stats(response) -> dict:
    """
    Parse cache-hit statistics from a Moonshot/Kimi chat completion response.

    Kimi surfaces cached token counts via either `usage.cached_tokens` or
    `usage.prompt_tokens_details.cached_tokens` (depending on API version).
    Both are checked.
    """
    stats = {
        "prompt_tokens": 0,
        "cached_tokens": 0,
        "completion_tokens": 0,
        "cache_hit_rate": 0.0,
    }

    usage = getattr(response, "usage", None)
    if usage is None:
        return stats

    prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
    completion_tokens = getattr(usage, "completion_tokens", 0) or 0

    cached_tokens = getattr(usage, "cached_tokens", 0) or 0
    if not cached_tokens:
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cached_tokens = getattr(details, "cached_tokens", 0) or 0
        elif isinstance(usage, dict):
            details_dict = usage.get("prompt_tokens_details") or {}
            cached_tokens = details_dict.get("cached_tokens", 0) or 0

    stats["prompt_tokens"] = prompt_tokens
    stats["completion_tokens"] = completion_tokens
    stats["cached_tokens"] = cached_tokens
    if prompt_tokens > 0:
        stats["cache_hit_rate"] = cached_tokens / prompt_tokens
    return stats


def log_cache_stats(logger: logging.Logger, agent_label: str, company_name: str, stats: dict) -> None:
    """Emit a one-line cache-stats summary so cache hits are visible per call."""
    logger.info(
        "[cache] %s | %s | prompt=%d cached=%d (%.0f%%) completion=%d",
        agent_label,
        company_name,
        stats["prompt_tokens"],
        stats["cached_tokens"],
        stats["cache_hit_rate"] * 100,
        stats["completion_tokens"],
    )
