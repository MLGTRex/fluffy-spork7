"""
Shared Moonshot formula-tool client for tool-using calls (Stage 2 deep
research, Stage 5 monitor Call 1). Wraps AsyncOpenAI (chat completions) +
httpx (Moonshot's /formulas/{uri}/tools and /fibers endpoints).

Stage-5-specific features (cap on tool-call rounds, force-answer message,
text-tool-call retry) are gated on optional constructor args. Passing only
the Stage 2 args yields byte-for-byte parity with Stage 2's original
recursive handler.
"""

import json
import logging

import httpx
from openai import AsyncOpenAI


def normalise_formula_uri(uri: str) -> str:
    if "/" not in uri:
        uri = f"moonshot/{uri}"
    if ":" not in uri:
        uri = f"{uri}:latest"
    return uri


def looks_like_text_tool_call(content: str) -> bool:
    """Detect a Moonshot tool-call directive emitted as plain text rather
    than via the structured tool_calls API field. Happens when the model
    still wants a tool after the tool list has been withdrawn."""
    if not content:
        return False
    return content.lstrip().startswith("<tool>") or "<tool_input>" in content


class FormulaChatClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        max_tokens: int,
        max_tool_calls: int | None = None,
        force_answer_instruction: str | None = None,
        max_force_answer_attempts: int = 0,
        httpx_timeout: float = 30.0,
        openai_max_retries: int | None = None,
        logger: logging.Logger | None = None,
    ):
        oai_kwargs = {"base_url": base_url, "api_key": api_key}
        if openai_max_retries is not None:
            oai_kwargs["max_retries"] = openai_max_retries
        self.openai = AsyncOpenAI(**oai_kwargs)
        self.httpx = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx_timeout,
        )
        self.model = model
        self.max_tokens = max_tokens
        self.max_tool_calls = max_tool_calls
        self.force_answer_instruction = force_answer_instruction
        self.max_force_answer_attempts = max_force_answer_attempts
        self.log = logger or logging.getLogger(__name__)

        self.tool_call_count = 0
        self.search_queries: list[str] = []
        self.cap_reached = False
        self.force_answer_attempts = 0
        self.token_usage = {"input_tokens": None, "output_tokens": None, "total_tokens": None}

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
        error_msg = fiber.get("error", "Unknown error")
        self.log.error(f"Tool error in {function}: {error_msg}")
        return f"Error: {error_msg}"

    def _accumulate_usage(self, response):
        usage = getattr(response, "usage", None)
        if not usage:
            return
        prev_in = self.token_usage["input_tokens"] or 0
        prev_out = self.token_usage["output_tokens"] or 0
        new_in = getattr(usage, "prompt_tokens", None) or 0
        new_out = getattr(usage, "completion_tokens", None) or 0
        self.token_usage["input_tokens"] = prev_in + new_in
        self.token_usage["output_tokens"] = prev_out + new_out
        self.token_usage["total_tokens"] = (
            self.token_usage["input_tokens"] + self.token_usage["output_tokens"]
        )

    async def handle_response(self, response, messages, all_tools, tool_to_uri):
        self._accumulate_usage(response)

        message = response.choices[0].message
        messages.append(message)

        # Base case: text response (or a text-tool-call we can retry).
        if not message.tool_calls:
            if (
                self.force_answer_instruction is not None
                and looks_like_text_tool_call(message.content)
                and self.force_answer_attempts < self.max_force_answer_attempts
            ):
                self.force_answer_attempts += 1
                self.log.warning(
                    "[LLM] Model emitted a tool call as plain text instead of a "
                    f"final answer; re-prompting for the final answer (attempt "
                    f"{self.force_answer_attempts}/{self.max_force_answer_attempts})."
                )
                messages.append({"role": "user", "content": self.force_answer_instruction})
                next_res = await self.openai.chat.completions.create(
                    model=self.model, messages=messages, max_tokens=self.max_tokens
                )
                return await self.handle_response(next_res, messages, all_tools, tool_to_uri)
            self.log.info("[LLM] Final text response generated.")
            return message.content

        n_calls = len(message.tool_calls)
        self.log.info(
            f"[LLM] Requested {n_calls} tool call(s) "
            f"(cumulative before this round: {self.tool_call_count})"
        )

        for call in message.tool_calls:
            func_name = call.function.name
            raw_args = call.function.arguments

            try:
                parsed_args = json.loads(raw_args)
                if isinstance(parsed_args, dict):
                    query = (
                        parsed_args.get("query")
                        or parsed_args.get("q")
                        or parsed_args.get("search_query")
                    )
                    if query:
                        self.search_queries.append(str(query))
            except (json.JSONDecodeError, TypeError):
                pass

            self.tool_call_count += 1

            uri = tool_to_uri.get(func_name)
            if not uri:
                self.log.error(f"URI not found for tool {func_name}")
                messages.append({
                    "role": "tool", "tool_call_id": call.id,
                    "content": f"Error: tool {func_name} not registered",
                })
                continue

            short_args = raw_args[:80] + "..." if len(raw_args) > 80 else raw_args
            self.log.info(f"  → Calling tool '{func_name}' with {short_args}")
            result = await self.call_tool(uri, func_name, json.loads(raw_args))
            self.log.info(f"  ← Tool '{func_name}' completed.")
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

        next_tools = all_tools
        if self.max_tool_calls is not None and self.tool_call_count >= self.max_tool_calls:
            self.cap_reached = True
            self.log.warning(
                f"Tool-call cap of {self.max_tool_calls} reached "
                f"(actual: {self.tool_call_count}). Forcing final answer."
            )
            next_tools = None
            if self.force_answer_instruction is not None:
                messages.append({"role": "user", "content": self.force_answer_instruction})

        kwargs = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens}
        if next_tools:
            kwargs["tools"] = next_tools

        next_res = await self.openai.chat.completions.create(**kwargs)
        return await self.handle_response(next_res, messages, all_tools, tool_to_uri)

    async def close(self):
        await self.httpx.aclose()
