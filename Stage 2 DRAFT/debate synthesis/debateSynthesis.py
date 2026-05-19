import os
import sys
import re
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


def extract_structured_fields(content: str) -> dict:
    """
    Extract the trailing JSON block from a synthesis output.
    
    Returns a dict with score, categorical, score_confidence.
    Returns None for fields if extraction fails.
    """
    result = {
        "score": None,
        "categorical": "",
        "score_confidence": ""
    }
    
    # Find the last ```json ... ``` block in the content
    pattern = r'```json\s*(\{.*?\})\s*```'
    matches = re.findall(pattern, content, re.DOTALL)
    
    if not matches:
        # Fallback: try to find a bare JSON object near the end
        bare_pattern = r'(\{[^{}]*"score"[^{}]*\})'
        bare_matches = re.findall(bare_pattern, content, re.DOTALL)
        if not bare_matches:
            logger.warning("No JSON block found in synthesis output.")
            return result
        json_str = bare_matches[-1]
    else:
        json_str = matches[-1]
    
    try:
        parsed = json.loads(json_str)
        result["score"] = parsed.get("score")
        result["categorical"] = parsed.get("categorical", "")
        result["score_confidence"] = parsed.get("score_confidence", "")
        logger.info(f"Structured fields parsed: score={result['score']}, categorical={result['categorical']}, confidence={result['score_confidence']}")
    except json.JSONDecodeError as e:
        logger.warning(f"JSON parse failed: {e}. Content: {json_str[:200]}")
    
    return result


async def run_synthesis(
    bull_case: str,
    bear_case: str,
    bull_rebuttal: str,
    bear_rebuttal: str,
    company_name: str
) -> dict:
    """
    Generate a debate synthesis for a company.
    
    Args:
        bull_case: The initial bull case
        bear_case: The initial bear case
        bull_rebuttal: The bull rebuttal of the bear case
        bear_rebuttal: The bear rebuttal of the bull case
        company_name: Name of the target company (for logging and the user message)
    
    Returns:
        A dict with:
            content: The full synthesis as markdown
            score: Numeric sentiment score (-100 to +100), or None if parsing failed
            categorical: Categorical bucket, or "" if parsing failed
            score_confidence: Confidence level on the score, or "" if parsing failed
    """
    logger.info(f"Initialising Synthesis Function for {company_name}...")
    
    api_key = os.getenv("MOONSHOT_API_KEY")
    base_url = os.getenv("MOONSHOT_BASE_URL") or "https://api.moonshot.ai/v1"
    
    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    model = "kimi-k2.6"
    max_tokens = 32768
    
    user_message = f"""Synthesize the debate for {company_name} based on the following four documents.

# BULL CASE

{bull_case}

---

# BEAR CASE

{bear_case}

---

# BULL REBUTTAL

{bull_rebuttal}

---

# BEAR REBUTTAL

{bear_rebuttal}

---

After your full markdown synthesis output, append a JSON block in the following format containing your final structured ratings:

```json
{{"score": <integer from -100 to +100>, "categorical": "<Strong Bear | Bear | Neutral | Bull | Strong Bull>", "score_confidence": "<High | Medium | Low>"}}
```
"""
    
    messages = [
        {"role": "system", "content": _load_prompt("synthesis.md")},
        {"role": "user", "content": user_message}
    ]
    
    try:
        logger.info(f"Sending synthesis request to model for {company_name}...")
        
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens
        )
        
        content = response.choices[0].message.content
        logger.info(f"Synthesis generated for {company_name}.")
        
        structured = extract_structured_fields(content)
        
        return {
            "content": content,
            "score": structured["score"],
            "categorical": structured["categorical"],
            "score_confidence": structured["score_confidence"]
        }
    
    finally:
        await client.close()
        logger.info(f"Synthesis Function completed for {company_name}.\n")