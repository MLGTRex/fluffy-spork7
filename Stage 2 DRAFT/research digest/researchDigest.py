import asyncio
import os
import sys
import logging
from datetime import datetime
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pipeline tools"))
from moonshot_cache import extract_cache_stats, log_cache_stats
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

# Per-section output budget. Smaller than the previous single-call 32768 so each
# section call finishes well within the SDK's response-timeout window, removing
# the timeout-driven retry loop that re-bills full input on every attempt.
_SECTION_MAX_TOKENS = 16384


async def _digest_section(
    section_label: str,
    section_content: str,
    company_name: str,
) -> str:
    """Run the dedup pass on a single research section. Returns the cleaned text."""
    client, model = get_llm_client(max_retries=5)

    user_message = (
        f"Below is the {section_label} research report for {company_name}. "
        f"Produce the cleaned version per your instructions.\n\n{section_content}"
    )

    messages = [
        {"role": "system", "content": _load_prompt("research_digest_section.md")},
        {"role": "user", "content": user_message},
    ]

    try:
        logger.info(f"Sending {section_label} digest request to model for {company_name}...")

        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=_SECTION_MAX_TOKENS,
        )

        content = response.choices[0].message.content
        logger.info(f"{section_label} digest generated for {company_name}.")

        cache_label = f"research_digest_{section_label.lower().replace(' & ', '_').replace(' ', '_')}"
        log_cache_stats(logger, cache_label, company_name, extract_cache_stats(response))

        return content

    finally:
        await client.close()


async def run_research_digest(
    finance_report: str,
    news_report: str,
    environment_report: str,
    company_name: str,
) -> str:
    """
    Produce a deduplicated research dossier from the three Stage 2 deep-research reports.

    Each report is cleaned independently in parallel — smaller, faster, no
    cross-section dedup. The three cleaned sections are reassembled into one
    dossier with the same labelled headers Stage 3's raw-reports fallback uses,
    so downstream consumers don't care which path produced the input.
    """
    logger.info(f"Initialising Research Digest for {company_name}...")

    sections = [
        ("FINANCIAL", finance_report),
        ("NEWS & NARRATIVE", news_report),
        ("COMPETITIVE & MACRO", environment_report),
    ]

    try:
        results = await asyncio.gather(
            *(_digest_section(label, content, company_name) for label, content in sections),
        )

        finance_clean, news_clean, environment_clean = results

        return f"""# FINANCIAL RESEARCH

{finance_clean}

---

# NEWS & NARRATIVE RESEARCH

{news_clean}

---

# COMPETITIVE & MACRO RESEARCH

{environment_clean}
"""

    finally:
        logger.info(f"Research Digest completed for {company_name}.\n")
