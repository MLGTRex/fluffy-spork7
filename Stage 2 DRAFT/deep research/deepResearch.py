import os
import json
import asyncio
import httpx
import logging
from datetime import datetime
from openai import AsyncOpenAI
from dotenv import load_dotenv
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

class FormulaChatClient:
    def __init__(self, moonshot_base_url: str, api_key: str, model: str, max_tokens: int):
        self.openai = AsyncOpenAI(base_url=moonshot_base_url, api_key=api_key)
        self.httpx = httpx.AsyncClient(base_url=moonshot_base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0)
        self.model = model
        self.max_tokens = max_tokens

    async def get_tools(self, formula_uri: str):
        response = await self.httpx.get(f"/formulas/{formula_uri}/tools")
        return response.json().get("tools", [])
    
    async def call_tool(self, formula_uri: str, function: str, args: dict):
        response = await self.httpx.post(
            f"/formulas/{formula_uri}/fibers",
            json={"name": function, "arguments": json.dumps(args)},
        )
        fiber = response.json()
        if fiber.get("status") == "succeeded":
            return fiber["context"].get("output") or fiber["context"].get("encrypted_output")
            
        error_msg = fiber.get('error', 'Unknown error')
        logger.error(f"Tool Error in {function}: {error_msg}")
        return f"Error: {error_msg}"

    async def handle_response(self, response, messages, all_tools, tool_to_uri):
        message = response.choices[0].message
        messages.append(message)

        # Base case: The AI is done and has a text answer
        if not message.tool_calls:
            logger.info("✓ [AI] Final text response generated.")
            return message.content

        # Recursive case: The AI wants to use tools
        logger.info(f"⚙ [AI] Requested {len(message.tool_calls)} tool call(s).")
        
        for call in message.tool_calls:
            func_name = call.function.name
            raw_args = call.function.arguments
            
            short_args = raw_args[:80] + "..." if len(raw_args) > 80 else raw_args
            logger.info(f"→ Calling tool: '{func_name}' with args: {short_args}")
            
            uri = tool_to_uri.get(func_name)
            if not uri:
                logger.error(f"URI not found for {func_name}")
                continue

            # Execute the tool
            result = await self.call_tool(uri, func_name, json.loads(raw_args))
            logger.info(f"← Tool '{func_name}' completed.")
            
            # Append the result to the conversation history
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

        # Send the tool results back to the AI for the next step
        logger.info("↻ [AI] Sending tool results back to the model for analysis...")
        next_res = await self.openai.chat.completions.create(
            model=self.model, messages=messages, tools=all_tools, max_tokens=self.max_tokens
        )
        return await self.handle_response(next_res, messages, all_tools, tool_to_uri)

    async def close(self):
        await self.httpx.aclose()


def normalise_formula_uri(uri: str) -> str:
    if "/" not in uri: uri = f"moonshot/{uri}"
    if ":" not in uri: uri = f"{uri}:latest"
    return uri


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
    
    client = FormulaChatClient(base_url, api_key, model="kimi-k2.6", max_tokens=32768)
    
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