import os
import sys
import logging
from datetime import datetime
from openai import AsyncOpenAI
from dotenv import load_dotenv

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
    
    system_prompt = prompt_map.get(rebuttal_type)
    if not system_prompt:
        raise ValueError(f"Unknown rebuttal_type: {rebuttal_type}. Must be 'BULL' or 'BEAR'.")
    
    own_label = "BULL CASE" if rebuttal_type == "BULL" else "BEAR CASE"
    opposing_label = "BEAR CASE" if rebuttal_type == "BULL" else "BULL CASE"
    
    api_key = os.getenv("MOONSHOT_API_KEY")
    base_url = os.getenv("MOONSHOT_BASE_URL") or "https://api.moonshot.ai/v1"
    
    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    model = "kimi-k2.6"
    max_tokens = 32768
    
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
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message}
    ]
    
    try:
        logger.info(f"Sending {rebuttal_type} rebuttal request to model for {company_name}...")
        
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens
        )
        
        content = response.choices[0].message.content
        logger.info(f"{rebuttal_type} rebuttal generated for {company_name}.")
        
        return content
    
    finally:
        await client.close()
        logger.info(f"Debate Rebuttal Function ({rebuttal_type}) completed for {company_name}.\n")