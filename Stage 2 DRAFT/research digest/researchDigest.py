import os
import re
import sys
import logging
from datetime import datetime
from openai import AsyncOpenAI
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pipeline tools"))
from moonshot_cache import extract_cache_stats, log_cache_stats

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "prompts")

# Inline citations in the deep-research output look like
#   [Source: <publisher> / Title: <title> / Date: <YYYY-MM-DD>]
# They exist to keep deep research honest to itself during fact-finding.
# Downstream stages treat the dossier as accurate, so they're pure overhead
# from the digest onward.
_CITATION_RE = re.compile(r'\s*\[Source:[^\]]*\]')


def _strip_citations(report: str) -> str:
    return _CITATION_RE.sub('', report)


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


async def run_research_digest(
    finance_report: str,
    news_report: str,
    environment_report: str,
    company_name: str,
) -> str:
    """
    Produce a deduplicated research dossier from the three Stage 2 deep-research reports.

    The output preserves every fact, number, date, name, quote and material claim
    from the originals, with cross-report repetition and boilerplate removed.
    Downstream Stage 3 scenario agents consume this in place of the raw concatenation.
    """
    logger.info(f"Initialising Research Digest for {company_name}...")

    api_key = os.getenv("MOONSHOT_API_KEY")
    base_url = os.getenv("MOONSHOT_BASE_URL") or "https://api.moonshot.ai/v1"

    client = AsyncOpenAI(base_url=base_url, api_key=api_key, max_retries=5)
    model = "kimi-k2.6"
    max_tokens = 32768

    finance_clean = _strip_citations(finance_report)
    news_clean = _strip_citations(news_report)
    environment_clean = _strip_citations(environment_report)

    user_message = f"""Produce a deduplicated research dossier for {company_name} from the three reports below.

# FINANCIAL RESEARCH

{finance_clean}

---

# NEWS & NARRATIVE RESEARCH

{news_clean}

---

# COMPETITIVE & MACRO RESEARCH

{environment_clean}
"""

    messages = [
        {"role": "system", "content": _load_prompt("research_digest.md")},
        {"role": "user", "content": user_message},
    ]

    try:
        logger.info(f"Sending research-digest request to model for {company_name}...")

        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
        )

        content = response.choices[0].message.content
        logger.info(f"Research digest generated for {company_name}.")

        log_cache_stats(logger, "research_digest", company_name, extract_cache_stats(response))

        return content

    finally:
        await client.close()
        logger.info(f"Research Digest completed for {company_name}.\n")
