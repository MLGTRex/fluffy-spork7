import os
import sys
import json
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


async def run_debate_case(case_type: str, research_dump: str, company_name: str) -> str:
    """
    Generate a bull or bear case for a company based on the research dump.
    
    Args:
        case_type: "BULL" or "BEAR"
        research_dump: Combined financial, news, and competitive research as a single string
        company_name: Name of the target company (for logging and the user message)
    
    Returns:
        The generated case as a markdown string.
    """
    logger.info(f"Initialising Debate Case Function ({case_type}) for {company_name}...")
    
    prompt_map = {
        "BULL": _load_prompt("bull_case.md"),
        "BEAR": _load_prompt("bear_case.md"),
    }
    
    system_prompt = prompt_map.get(case_type)
    if not system_prompt:
        raise ValueError(f"Unknown case_type: {case_type}. Must be 'BULL' or 'BEAR'.")
    
    api_key = os.getenv("MOONSHOT_API_KEY")
    base_url = os.getenv("MOONSHOT_BASE_URL") or "https://api.moonshot.ai/v1"
    
    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    model = "kimi-k2.6"
    max_tokens = 32768
    
    user_message = f"Construct your case for {company_name} based on the following research dump.\n\n{research_dump}"
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message}
    ]
    
    try:
        logger.info(f"Sending {case_type} case request to model for {company_name}...")
        
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens
        )
        
        content = response.choices[0].message.content
        logger.info(f"{case_type} case generated for {company_name}.")
        
        return content
    
    finally:
        await client.close()
        logger.info(f"Debate Case Function ({case_type}) completed for {company_name}.\n")