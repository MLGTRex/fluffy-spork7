import os
import asyncio
import logging
from datetime import datetime
from openai import AsyncOpenAI
from dotenv import load_dotenv

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")


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
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

MODEL = "moonshotai/kimi-k2.6"
MAX_TOKENS = 32768
MAX_SEARCH_RESULTS = 10


async def run_deep_research(question: str, system_prompt_type: str = "FINANCE") -> str:
    logger.info("Initializing OpenRouter Deep Research demo...")

    prompt_map = {
        "FINANCE": _load_prompt("finance.md"),
        "NEWS": _load_prompt("news.md"),
        "ENVIRONMENT": _load_prompt("environment.md"),
    }
    system_prompt = prompt_map.get(system_prompt_type, _load_prompt("finance.md"))

    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    client = AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
        max_retries=5,
    )

    tools = [{
        "type": "openrouter:web_search",
        "parameters": {
            "engine": "exa",
            "max_results": MAX_SEARCH_RESULTS,
            "max_total_results": MAX_SEARCH_RESULTS,
        },
    }]

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]

    logger.info(f"Sending query to OpenRouter (model={MODEL})...")
    logger.info(f"User Question: '{question}'")

    response = await client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=tools,
        max_tokens=MAX_TOKENS,
    )

    msg = response.choices[0].message
    text = msg.content or ""

    tool_calls = getattr(msg, "tool_calls", None) or []
    if tool_calls:
        logger.info(f"⚙ Server-tool searches performed: {len(tool_calls)}")
        for call in tool_calls:
            raw_args = getattr(getattr(call, "function", None), "arguments", "") or ""
            short = raw_args[:80] + "..." if len(raw_args) > 80 else raw_args
            logger.info(f"  → search args: {short}")
            logger.info(f"  ← search completed")

    annotations = getattr(msg, "annotations", None) or []
    if annotations:
        logger.info(f"✓ Annotations received: {len(annotations)}")
        extras = []
        for a in annotations:
            if getattr(a, "type", None) != "url_citation":
                continue
            cit = getattr(a, "url_citation", None)
            if cit is None:
                continue
            url = getattr(cit, "url", "") or ""
            title = getattr(cit, "title", "") or url
            extras.append(f"[Source: {url} / Title: {title} / Date: ]")
        if extras:
            text = text + "\n\n---\nAnnotation citations:\n" + "\n".join(extras)

    logger.info("✓ Final text response generated.")
    return text
