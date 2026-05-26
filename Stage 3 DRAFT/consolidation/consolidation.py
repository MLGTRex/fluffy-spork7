import os
import re
import sys
import json
import logging
import asyncio
from datetime import datetime
from dotenv import load_dotenv
import yfinance as yf

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pipeline tools"))
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


def extract_ticker(company_name: str) -> str:
    """
    Extract ticker symbol from a company name formatted as 'Company Name (TICKER)'.
    Returns the ticker string, or raises ValueError if not found.
    """
    match = re.search(r'\(([A-Z][A-Z0-9.\-]*)\)', company_name)
    if not match:
        raise ValueError(f"Could not extract ticker from company name: {company_name}")
    return match.group(1)


def fetch_current_price(ticker: str) -> float:
    """
    Fetch the current (most recent close) price for a ticker via yfinance.
    Returns price as float. Raises RuntimeError on failure.
    """
    try:
        t = yf.Ticker(ticker)
        price = None
        try:
            fi = t.fast_info
            price = fi.get("last_price") if hasattr(fi, "get") else getattr(fi, "last_price", None)
        except Exception:
            price = None

        if price is None or price == 0:
            hist = t.history(period="5d")
            if hist.empty:
                raise RuntimeError(f"No price history returned for {ticker}.")
            price = float(hist["Close"].iloc[-1])

        if price is None or price <= 0:
            raise RuntimeError(f"Invalid price returned for {ticker}: {price}")

        return float(price)
    except Exception as e:
        raise RuntimeError(f"yfinance lookup failed for {ticker}: {e}")


def strip_empties(obj):
    """
    Recursively remove keys/items with empty/null values from a dict or list.
    Specifically removes:
        - None
        - "" (empty string)
        - [] (empty list)
        - {} (empty dict, after recursive cleaning)
        - "N/A", "n/a", "NA", "null" (string sentinel values)
        - NaN (any float that fails == itself)

    Returns the cleaned object. May return None if the entire structure becomes empty.
    """
    NA_STRINGS = {"n/a", "na", "null", "none", "-"}

    def is_empty(v) -> bool:
        if v is None:
            return True
        if isinstance(v, str):
            if v.strip() == "":
                return True
            if v.strip().lower() in NA_STRINGS:
                return True
            return False
        if isinstance(v, float):
            try:
                if v != v:  # NaN check
                    return True
            except Exception:
                pass
            return False
        if isinstance(v, (list, tuple)):
            return len(v) == 0
        if isinstance(v, dict):
            return len(v) == 0
        return False

    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            cleaned_v = strip_empties(v)
            if not is_empty(cleaned_v):
                cleaned[k] = cleaned_v
        return cleaned
    elif isinstance(obj, list):
        cleaned_list = []
        for item in obj:
            cleaned_item = strip_empties(item)
            if not is_empty(cleaned_item):
                cleaned_list.append(cleaned_item)
        return cleaned_list
    else:
        return obj


def extract_structured_fields(content: str) -> dict:
    """
    Extract the trailing JSON block from a consolidation output.

    Returns a dict containing the raw fields parsed from the LLM's JSON block.
    Fields default to None / empty if extraction fails.
    """
    result = {
        "price_target_bull_1m": None,
        "price_target_bull_3m": None,
        "price_target_bull_6m": None,
        "price_target_bull_12m": None,
        "price_target_base_1m": None,
        "price_target_base_3m": None,
        "price_target_base_6m": None,
        "price_target_base_12m": None,
        "price_target_bear_1m": None,
        "price_target_bear_3m": None,
        "price_target_bear_6m": None,
        "price_target_bear_12m": None,
        "scenario_probability_bull": None,
        "scenario_probability_base": None,
        "scenario_probability_bear": None,
        "conviction": "",
        "thesis_summary": "",
        "key_invalidation_triggers": [],
    }

    pattern = r'```json\s*(\{.*?\})\s*```'
    matches = re.findall(pattern, content, re.DOTALL)

    if not matches:
        bare_pattern = r'(\{[^{}]*"scenario_probability_bull"[^{}]*\})'
        bare_matches = re.findall(bare_pattern, content, re.DOTALL)
        if not bare_matches:
            logger.warning("No JSON block found in consolidation output.")
            return result
        json_str = bare_matches[-1]
    else:
        json_str = matches[-1]

    try:
        parsed = json.loads(json_str)
        for key in result.keys():
            if key in parsed:
                result[key] = parsed[key]
        logger.info(
            f"Structured fields parsed: conviction={result['conviction']}, "
            f"probs=(bull={result['scenario_probability_bull']}, "
            f"base={result['scenario_probability_base']}, "
            f"bear={result['scenario_probability_bear']})"
        )
    except json.JSONDecodeError as e:
        logger.warning(f"JSON parse failed: {e}. Content: {json_str[:200]}")

    return result


def compute_weighted_returns(structured: dict, current_price: float) -> dict:
    """
    Compute probability-weighted returns from raw price targets + probabilities + current price.
    """
    out = {
        "expected_return_1m": None,
        "expected_return_3m": None,
        "expected_return_6m": None,
        "expected_return_12m": None,
        "upside_return_12m": None,
        "base_return_12m": None,
        "downside_return_12m": None,
    }

    if not current_price or current_price <= 0:
        logger.warning("Cannot compute returns: invalid current price.")
        return out

    p_bull = structured.get("scenario_probability_bull")
    p_base = structured.get("scenario_probability_base")
    p_bear = structured.get("scenario_probability_bear")

    if None in (p_bull, p_base, p_bear):
        logger.warning("Cannot compute weighted returns: missing scenario probabilities.")
        weighted_ok = False
    else:
        prob_sum = p_bull + p_base + p_bear
        if abs(prob_sum - 1.0) > 0.01:
            logger.warning(f"Scenario probabilities do not sum to 1.0 (sum={prob_sum}). Computing returns anyway.")
        weighted_ok = True

    def pct_return(target):
        if target is None:
            return None
        return (target - current_price) / current_price

    for horizon in ("1m", "3m", "6m", "12m"):
        bull_t = structured.get(f"price_target_bull_{horizon}")
        base_t = structured.get(f"price_target_base_{horizon}")
        bear_t = structured.get(f"price_target_bear_{horizon}")

        if weighted_ok and None not in (bull_t, base_t, bear_t):
            r_bull = pct_return(bull_t)
            r_base = pct_return(base_t)
            r_bear = pct_return(bear_t)
            out[f"expected_return_{horizon}"] = (
                p_bull * r_bull + p_base * r_base + p_bear * r_bear
            )
        else:
            logger.warning(f"Cannot compute expected return for {horizon}: missing target(s).")

    bull_12 = structured.get("price_target_bull_12m")
    base_12 = structured.get("price_target_base_12m")
    bear_12 = structured.get("price_target_bear_12m")

    if bull_12 is not None:
        out["upside_return_12m"] = pct_return(bull_12)
    if base_12 is not None:
        out["base_return_12m"] = pct_return(base_12)
    if bear_12 is not None:
        out["downside_return_12m"] = pct_return(bear_12)

    return out


async def run_consolidation(
    scenario_bull: str,
    scenario_bear: str,
    scenario_base_final: str,
    synthesis: str,
    valuation_metrics: dict,
    company_name: str,
) -> dict:
    """
    Generate a consolidation output for a company.
    See module-level documentation for full input/output details.
    """
    logger.info(f"Initialising Consolidation Function for {company_name}...")

    ticker = extract_ticker(company_name)
    logger.info(f"Extracted ticker {ticker} for {company_name}.")

    current_price = await asyncio.to_thread(fetch_current_price, ticker)
    logger.info(f"Current price for {ticker}: ${current_price:.2f}")

    client, model = get_llm_client()
    max_tokens = 32768

    # Strip null/empty/NA values from valuation metrics so the LLM never sees them.
    # The original block is preserved in the JSON file; this only affects what's sent to the model.
    if valuation_metrics:
        cleaned_metrics = strip_empties(valuation_metrics)
        if cleaned_metrics:
            valuation_metrics_section = f"""# VALUATION METRICS

The following structured block contains the company's quantitative profile, including profitability, growth, valuation multiples (current and 5-year history), balance sheet, per-share data, and peer comparison. Only metrics with available data are included; any metric not shown was unavailable and should not be considered. Engage with the numbers shown specifically and quantitatively in your narrative.

```json
{json.dumps(cleaned_metrics, indent=2, ensure_ascii=False)}
```

---

"""
        else:
            # Everything was null/empty after cleaning — omit the section entirely
            valuation_metrics_section = ""
            logger.warning(f"{company_name}: valuation_metrics had no usable data after cleaning; omitting from prompt.")
    else:
        valuation_metrics_section = ""
        logger.warning(f"{company_name}: no valuation_metrics provided; omitting from prompt.")

    user_message = f"""Produce the consolidated thesis for {company_name} based on the following inputs.

The current market price for {ticker} is ${current_price:.2f}. Use this as the anchor for any return-related reasoning in your narrative; the JSON block must report price targets in absolute USD as instructed.

# BULL SCENARIO

{scenario_bull}

---

# BEAR SCENARIO

{scenario_bear}

---

# BASE SCENARIO (POST-ARBITRATION)

{scenario_base_final}

---

# DEBATE SYNTHESIS (STAGE 2)

{synthesis}

---

{valuation_metrics_section}After your full markdown consolidation output, append a JSON block in exactly the format specified in your system prompt.
"""

    messages = [
        {"role": "system", "content": _load_prompt("consolidation.md")},
        {"role": "user", "content": user_message}
    ]

    try:
        logger.info(f"Sending consolidation request to model for {company_name}...")

        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens
        )

        content = response.choices[0].message.content
        logger.info(f"Consolidation generated for {company_name}.")

        structured = extract_structured_fields(content)
        computed = compute_weighted_returns(structured, current_price)

        result = {
            "content": content,
            "ticker": ticker,
            "current_price": current_price,
        }
        result.update(structured)
        result.update(computed)

        return result

    finally:
        await client.close()
        logger.info(f"Consolidation Function completed for {company_name}.\n")