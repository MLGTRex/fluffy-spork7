import os
import sys
import logging
from datetime import datetime
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pipeline tools"))
from moonshot_cache import (
    is_cache_enabled,
    build_cache_friendly_messages,
    build_legacy_messages,
    extract_cache_stats,
    log_cache_stats,
)
from llm_client import get_llm_client

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "prompts")

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


async def run_debate_rebuttal(
    rebuttal_type: str,
    own_case: str,
    opposing_case: str,
    research_dump: str,
    company_name: str
) -> str:
    """
    Generate a bull or bear rebuttal for a company.
    
    Args:
        rebuttal_type: "BULL" or "BEAR" — which side is rebutting
        own_case: The agent's own initial case (bull rebutter gets bull case, bear rebutter gets bear case)
        opposing_case: The case to rebut
        research_dump: Combined research as reference for verifying claims
        company_name: Name of the target company (for logging and the user message)
    
    Returns:
        The generated rebuttal as a markdown string.
    """
    logger.info(f"Initialising Debate Rebuttal Function ({rebuttal_type}) for {company_name}...")
    
    prompt_map = {
        "BULL": _load_prompt("bull_rebuttal.md"),
        "BEAR": _load_prompt("bear_rebuttal.md"),
    }

    role_prompt = prompt_map.get(rebuttal_type)
    if not role_prompt:
        raise ValueError(f"Unknown rebuttal_type: {rebuttal_type}. Must be 'BULL' or 'BEAR'.")

    own_label = "BULL CASE" if rebuttal_type == "BULL" else "BEAR CASE"
    opposing_label = "BEAR CASE" if rebuttal_type == "BULL" else "BULL CASE"

    client, model = get_llm_client(max_retries=5)
    max_tokens = 32768

    if is_cache_enabled():
        task_content = f"""Construct your rebuttal for {company_name} based on the inputs below. Use the research dossier provided above as your reference for verifying claims.

# YOUR INITIAL {own_label}

{own_case}

---

# THE OPPOSING {opposing_label} TO REBUT

{opposing_case}
"""
        messages = build_cache_friendly_messages(
            company_name=company_name,
            research_dump=research_dump,
            role_content=role_prompt,
            task_content=task_content,
        )
    else:
        user_message = f"""Construct your rebuttal for {company_name} based on the following inputs.

# YOUR INITIAL {own_label}

{own_case}

---

# THE OPPOSING {opposing_label} TO REBUT

{opposing_case}

---

# RESEARCH DUMP (REFERENCE)

{research_dump}
"""
        messages = build_legacy_messages(role_prompt, user_message)

    try:
        logger.info(f"Sending {rebuttal_type} rebuttal request to model for {company_name}...")

        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens
        )

        content = response.choices[0].message.content
        logger.info(f"{rebuttal_type} rebuttal generated for {company_name}.")

        log_cache_stats(logger, f"debate_rebuttal_{rebuttal_type.lower()}", company_name, extract_cache_stats(response))

        return content

    finally:
        await client.close()
        logger.info(f"Debate Rebuttal Function ({rebuttal_type}) completed for {company_name}.\n")