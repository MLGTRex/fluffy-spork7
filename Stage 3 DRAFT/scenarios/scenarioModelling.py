import os
import sys
import logging
from datetime import datetime
from openai import AsyncOpenAI
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pipeline tools"))
from moonshot_cache import (
    is_cache_enabled,
    build_cache_friendly_messages,
    build_legacy_messages,
    extract_cache_stats,
    log_cache_stats,
)

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "prompts")

# Maps logical agent keys to prompt filenames
PROMPT_FILES = {
    "bull": "scenario_bull.md",
    "bear": "scenario_bear.md",
    "base_initial": "scenario_base_initial.md",
    "bull_rebuttal": "scenario_bull_rebuttal.md",
    "bear_rebuttal": "scenario_bear_rebuttal.md",
    "base_arbitration": "scenario_base_arbitration.md",
}


def _load_prompt(name: str) -> str:
    with open(os.path.join(PROMPTS_DIR, name), encoding="utf-8") as f:
        return f.read()


load_dotenv()

log_filename = f"research_log_{datetime.now().strftime('%Y-%m-%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(log_filename, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def _build_user_message(
    agent_key: str,
    company_name: str,
    research_dump: str,
    bull_initial: str = "",
    bear_initial: str = "",
    base_initial: str = "",
    bull_rebuttal: str = "",
    bear_rebuttal: str = "",
) -> str:
    """Assemble the user message for a given agent based on what it needs to see."""

    if agent_key in ("bull", "bear", "base_initial"):
        # Initial scenario agents see only the research dump
        return f"""Construct your scenario for {company_name} based on the following research and debate inputs.

# RESEARCH DUMP

{research_dump}
"""

    if agent_key == "bull_rebuttal":
        return f"""Construct your bull scenario rebuttal for {company_name} based on the following inputs.

# YOUR INITIAL BULL SCENARIO

{bull_initial}

---

# THE OPPOSING BEAR SCENARIO TO REBUT

{bear_initial}

---

# RESEARCH DUMP (REFERENCE)

{research_dump}
"""

    if agent_key == "bear_rebuttal":
        return f"""Construct your bear scenario rebuttal for {company_name} based on the following inputs.

# YOUR INITIAL BEAR SCENARIO

{bear_initial}

---

# THE OPPOSING BULL SCENARIO TO REBUT

{bull_initial}

---

# RESEARCH DUMP (REFERENCE)

{research_dump}
"""

    if agent_key == "base_arbitration":
        return f"""Produce the final base scenario for {company_name} by arbitrating across the following inputs.

# YOUR INITIAL BASE SCENARIO

{base_initial}

---

# BULL SCENARIO

{bull_initial}

---

# BEAR SCENARIO

{bear_initial}

---

# BULL REBUTTAL

{bull_rebuttal}

---

# BEAR REBUTTAL

{bear_rebuttal}

---

# RESEARCH DUMP (REFERENCE)

{research_dump}
"""

    raise ValueError(f"Unknown agent_key: {agent_key}")


def _build_task_content(
    agent_key: str,
    company_name: str,
    bull_initial: str = "",
    bear_initial: str = "",
    base_initial: str = "",
    bull_rebuttal: str = "",
    bear_rebuttal: str = "",
) -> str:
    """
    Cache-friendly counterpart to _build_user_message.

    Omits the research dump (which lives in the cached system message under
    the cache-friendly structure). Returns only the per-agent task instruction
    plus any prior agent outputs the current agent needs to see.
    """
    if agent_key in ("bull", "bear", "base_initial"):
        label = {
            "bull": "bull",
            "bear": "bear",
            "base_initial": "initial base",
        }[agent_key]
        return (
            f"Construct your {label} scenario for {company_name} using the "
            "research dossier provided above."
        )

    if agent_key == "bull_rebuttal":
        return f"""Construct your bull scenario rebuttal for {company_name} using the research dossier provided above. The inputs you must respond to are below.

# YOUR INITIAL BULL SCENARIO

{bull_initial}

---

# THE OPPOSING BEAR SCENARIO TO REBUT

{bear_initial}
"""

    if agent_key == "bear_rebuttal":
        return f"""Construct your bear scenario rebuttal for {company_name} using the research dossier provided above. The inputs you must respond to are below.

# YOUR INITIAL BEAR SCENARIO

{bear_initial}

---

# THE OPPOSING BULL SCENARIO TO REBUT

{bull_initial}
"""

    if agent_key == "base_arbitration":
        return f"""Produce the final base scenario for {company_name} by arbitrating across the following inputs. Use the research dossier provided above as your reference for verifying claims.

# YOUR INITIAL BASE SCENARIO

{base_initial}

---

# BULL SCENARIO

{bull_initial}

---

# BEAR SCENARIO

{bear_initial}

---

# BULL REBUTTAL

{bull_rebuttal}

---

# BEAR REBUTTAL

{bear_rebuttal}
"""

    raise ValueError(f"Unknown agent_key: {agent_key}")


async def run_scenario_agent(
    agent_key: str,
    company_name: str,
    research_dump: str,
    bull_initial: str = "",
    bear_initial: str = "",
    base_initial: str = "",
    bull_rebuttal: str = "",
    bear_rebuttal: str = "",
) -> str:
    """
    Generate output for one of the six Stage 3b scenario agents.

    Args:
        agent_key: One of "bull", "bear", "base_initial",
                   "bull_rebuttal", "bear_rebuttal", "base_arbitration"
        company_name: Name of the target company (for logging and the user message)
        research_dump: Combined Stage 2 research + debate output
        bull_initial: Initial bull scenario (required for rebuttals + arbitration)
        bear_initial: Initial bear scenario (required for rebuttals + arbitration)
        base_initial: Initial base scenario (required for arbitration)
        bull_rebuttal: Bull rebuttal (required for arbitration)
        bear_rebuttal: Bear rebuttal (required for arbitration)

    Returns:
        The generated scenario document as a markdown string.
    """
    logger.info(f"Initialising Scenario Agent ({agent_key}) for {company_name}...")

    prompt_filename = PROMPT_FILES.get(agent_key)
    if not prompt_filename:
        raise ValueError(
            f"Unknown agent_key: {agent_key}. Must be one of {list(PROMPT_FILES.keys())}."
        )

    role_prompt = _load_prompt(prompt_filename)

    api_key = os.getenv("MOONSHOT_API_KEY")
    base_url = os.getenv("MOONSHOT_BASE_URL") or "https://api.moonshot.ai/v1"

    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    model = "kimi-k2.6"
    max_tokens = 32768

    if is_cache_enabled():
        task_content = _build_task_content(
            agent_key=agent_key,
            company_name=company_name,
            bull_initial=bull_initial,
            bear_initial=bear_initial,
            base_initial=base_initial,
            bull_rebuttal=bull_rebuttal,
            bear_rebuttal=bear_rebuttal,
        )
        messages = build_cache_friendly_messages(
            company_name=company_name,
            research_dump=research_dump,
            role_content=role_prompt,
            task_content=task_content,
        )
    else:
        user_message = _build_user_message(
            agent_key=agent_key,
            company_name=company_name,
            research_dump=research_dump,
            bull_initial=bull_initial,
            bear_initial=bear_initial,
            base_initial=base_initial,
            bull_rebuttal=bull_rebuttal,
            bear_rebuttal=bear_rebuttal,
        )
        messages = build_legacy_messages(role_prompt, user_message)

    try:
        logger.info(f"Sending {agent_key} scenario request to model for {company_name}...")

        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens
        )

        content = response.choices[0].message.content
        logger.info(f"{agent_key} scenario generated for {company_name}.")

        log_cache_stats(logger, f"scenario_{agent_key}", company_name, extract_cache_stats(response))

        return content

    finally:
        await client.close()
        logger.info(f"Scenario Agent ({agent_key}) completed for {company_name}.\n")