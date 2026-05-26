import os
import sys
import asyncio
import logging
from datetime import datetime
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pipeline tools"))
from moonshot_formula_client import FormulaChatClient, normalise_formula_uri

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "prompts")

def _load_prompt(name: str) -> str:
    with open(os.path.join(PROMPTS_DIR, name), encoding="utf-8") as f:
        return f.read()

load_dotenv()

# --- LOGGING SETUP ---
# This creates a log file with today's date, e.g., "research_log_2026-04-30.log"
log_filename = f"research_log_{datetime.now().strftime('%Y-%m-%d')}.log"

# Configure the logger to write to BOTH the file and the terminal
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(log_filename, encoding='utf-8'),
        logging.StreamHandler() # This keeps the terminal output active
    ]
)
logger = logging.getLogger(__name__)
# ---------------------

async def run_deep_research(question: str, system_prompt_type: str = "FINANCE", formulas: list = None):
    logger.info("Initializing Deep Research module...")
    
    if formulas is None:
        formulas = ["moonshot/web-search:latest", "moonshot/rethink:latest"]
    
    prompt_map = {
        "FINANCE": _load_prompt("finance.md"),
        "NEWS": _load_prompt("news.md"),
        "ENVIRONMENT": _load_prompt("environment.md"),
    }
    
    api_key = os.getenv("MOONSHOT_API_KEY")
    base_url = os.getenv("MOONSHOT_BASE_URL") or "https://api.moonshot.ai/v1"
    
    client = FormulaChatClient(
        base_url=base_url, api_key=api_key,
        model="kimi-k2.6", max_tokens=32768,
        httpx_timeout=30.0, openai_max_retries=5,
        logger=logger,
    )
    
    logger.info(f"Loading tools from {len(formulas)} formulas...")
    all_tools = []
    tool_to_uri = {}
    
    for uri in [normalise_formula_uri(u) for u in formulas]:
        tools = await client.get_tools(uri)
        for tool in tools:
            func = tool.get("function")
            if not func:
                continue
                
            func_name = func.get("name")
            if func_name and func_name not in tool_to_uri:
                all_tools.append(tool)
                tool_to_uri[func_name] = uri
                
    logger.info(f"✓ Loaded {len(all_tools)} unique tools.")

    try:
        messages = [{"role": "system", "content": prompt_map.get(system_prompt_type, _load_prompt("finance.md"))}]
        messages.append({"role": "user", "content": question})
        
        logger.info(f"Sending initial query to Moonshot API...")
        logger.info(f"User Question: '{question}'")
        
        response = await client.openai.chat.completions.create(
            model=client.model, messages=messages, tools=all_tools, max_tokens=client.max_tokens
        )
        
        final_answer = await client.handle_response(response, messages, all_tools, tool_to_uri)
        return final_answer
        
    finally:
        await client.close()
        logger.info("Deep Research module closed.\n")