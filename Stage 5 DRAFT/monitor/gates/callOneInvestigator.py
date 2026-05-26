"""
Call 1 — LLM-driven investigation gate.

For each ticker that passed Gate 0 (mechanical macro filter), this module
investigates "what is happening?" via web search. The output is a structured
factual summary with cited sources that Call 2 will consume to decide whether
to rerun the pipeline.

Design points:
  - Mirrors the access patterns of Stage 5 hourly watcher's llmTriggerClassifier
    (FormulaChatClient, recursive tool-call handler, Moonshot formula-based
    tool registry for web search).
  - Cut off from training context — the prompt explicitly instructs the LLM
    to verify all claims via web search.
  - Citations with credibility tags (***/**/*) following the Stage 2 deep
    research pattern. Same in spirit as Stage 2's finance.md prompt.
  - Pure investigation. No decision-making. Call 2 does that.

Configuration:
    MODEL                  = kimi-k2.6
    MAX_TOKENS             = 32768
    MAX_WEB_SEARCH_CALLS   = 10  (cap, NOT target)
    SEARCH_FORMULA         = moonshot/web-search:latest

Public API:
    build_investigation_packet(ticker, ...) → dict
        Builds the structured input passed to the LLM. Assembled from
        upstream Stage 4 thesis, signal aggregator output, macro context,
        cadence label.

    investigate_ticker(packet, *, ...) → dict
        Sends the LLM call, runs the tool-call loop (web searches), parses
        the structured investigation output, validates it. Returns a result.

Output structure (from investigate_ticker):
    {
        "ticker": str,
        "investigation": {                # parsed LLM output, or None on failure
            "ticker": str,
            "investigation_summary": str,
            "established_facts": [str],
            "candidate_causes": [{"cause", "supporting_evidence",
                                   "confidence", "time_horizon"}],
            "relationship_to_existing_thesis": str,
            "sources_consulted": [{"url", "publisher", "date", "credibility"}],
            "search_queries_used": [str],
            "investigation_confidence": "high" | "medium" | "low",
            "ambiguity_notes": str,
        },
        "raw_response": str,
        "validation_violations": [str],
        "web_search_calls_made": int,
        "web_search_cap_reached": bool,
        "token_usage": {"input_tokens", "output_tokens", "total_tokens"},
        "model": str,
        "status": "ok" | "parse_failed" | "validation_failed" | "api_failed",
        "error": str | None,
    }

CLI:
    python3 callOneInvestigator.py --packet path/to/packet.json
"""

import os
import sys
import json
import re
import asyncio
import logging
import argparse
from datetime import datetime
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # Optional import — surfaced as API failure if missing at call time
    pass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "pipeline tools"))
from moonshot_formula_client import FormulaChatClient, normalise_formula_uri

logger = logging.getLogger(__name__)


# ============ CONFIG ============

MODEL = "kimi-k2.6"
MAX_TOKENS = 32768
MAX_WEB_SEARCH_CALLS = 10
SEARCH_FORMULA = "moonshot/web-search:latest"

# Bounded retries when the model returns a text tool-call instead of an answer.
MAX_FORCE_ANSWER_ATTEMPTS = 2

# Appended to the conversation when the model must stop calling tools and emit
# its final answer. Moonshot models otherwise sometimes "ask" for another tool
# by writing a <tool>...</tool_input> directive as plain text once the real
# tool list is withdrawn — which is not a usable answer.
FORCE_ANSWER_INSTRUCTION = (
    "You have used all permitted web searches — the search tool is no longer "
    "available. Do NOT request any more searches and do NOT emit any <tool> or "
    "<tool_input> directives. Using only the information already gathered, "
    "output your final answer now: the single JSON investigation object "
    "specified in your instructions, inside a ```json fenced code block, with "
    "no other text."
)


# ============ PATHS ============

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPTS_DIR = os.path.join(SCRIPT_DIR, "prompts")
PROMPT_FILENAME = "call_1_investigation.md"
# Logs directory — assumes module sits in Stage 5 DRAFT/monitor/gates/
# and logs go to Stage 5 DRAFT/logs/
LOGS_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "..", "logs"))


# ============ LOGGER ============

def _ensure_logger():
    if not logger.handlers:
        os.makedirs(LOGS_DIR, exist_ok=True)
        log_filename = os.path.join(
            LOGS_DIR,
            f"call_one_investigator_{datetime.now().strftime('%Y-%m-%d')}.log",
        )
        try:
            handler = logging.FileHandler(log_filename, encoding="utf-8")
        except OSError:
            handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


# ============ PROMPT LOADING ============

def _load_prompt() -> str:
    path = os.path.join(PROMPTS_DIR, PROMPT_FILENAME)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Call 1 investigation prompt not found at {path}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ============ PACKET BUILDER ============

def build_investigation_packet(
    ticker: str,
    thesis: Optional[dict] = None,
    signal_decision: Optional[dict] = None,
    macro_context: Optional[dict] = None,
    cadence_window: Optional[str] = None,
    anchor: Optional[dict] = None,
    sector_etf_symbol: Optional[str] = None,
) -> dict:
    """
    Assemble the structured input for the investigator.

    Args:
        ticker: ticker symbol
        thesis: dict from candidate_summaries.json for this ticker (the
                Stage 4 selection summary). Read by the LLM to know the
                existing thesis.
        signal_decision: signal aggregator decision dict (with fired_signals,
                         trigger_path, etc.)
        macro_context: macro_context output dict (with SPY, sector ETF moves)
        cadence_window: which window the trigger fired in. One of:
                        "pre_open", "post_open", "pre_close", "post_close",
                        or None / "unknown".
        anchor: ticker_evaluation_anchors entry for this ticker (with
                evaluated_at, evaluated_at_price). Optional.
        sector_etf_symbol: which sector ETF was used (e.g. "XLK"). Optional.
    """
    return {
        "ticker": ticker,
        "thesis": thesis or {},
        "signal_decision": signal_decision or {},
        "macro_context": macro_context or {},
        "cadence_window": cadence_window or "unknown",
        "anchor": anchor or {},
        "sector_etf_symbol": sector_etf_symbol,
    }


# ============ USER MESSAGE BUILDER ============

def _format_signal_decision_for_prompt(signal_decision: dict) -> str:
    """Render the signal-aggregator decision in a readable form."""
    if not signal_decision:
        return "_(No signal decision available.)_"

    parts = []
    parts.append(f"- Trigger path: `{signal_decision.get('trigger_path', 'unknown')}`")

    fired = signal_decision.get("fired_signals") or []
    if not fired:
        parts.append("- Fired signals: _(none recorded)_")
    else:
        parts.append("- Fired signals:")
        for s in fired:
            stype = s.get("signal_type", "?")
            tier = s.get("tier", "?")
            details = s.get("details", {})
            # Concise per-signal summary
            if stype == "price":
                dm = details.get("daily_move", {})
                mfo = details.get("move_from_open", {})
                fragments = []
                if dm.get("fired"):
                    fragments.append(
                        f"daily_move={dm.get('current_pct')}% "
                        f"(absolute_pct={dm.get('absolute_pct')}%, "
                        f"pct_reached_in_distribution={dm.get('percentile_reached')})"
                    )
                if mfo.get("fired"):
                    fragments.append(f"move_from_open={mfo.get('current_pct')}%")
                parts.append(f"  - {stype} [{tier}]: " + "; ".join(fragments))
            elif stype == "volume":
                parts.append(
                    f"  - {stype} [{tier}]: current_volume={details.get('current_volume')}, "
                    f"ratio_to_median={details.get('ratio_to_median')}, "
                    f"percentile_reached={details.get('percentile_reached')}"
                )
            elif stype == "cumulative":
                parts.append(
                    f"  - {stype} [{tier}]: "
                    f"cumulative_move={details.get('cumulative_move_pct')}%, "
                    f"fired_via={details.get('fired_via')}"
                )
            else:
                parts.append(f"  - {stype} [{tier}]: {json.dumps(details, default=str)[:200]}")
    return "\n".join(parts)


def _format_macro_context_for_prompt(macro_context: dict,
                                      sector_etf_symbol: Optional[str]) -> str:
    """Format the macro context as neutral data."""
    if not macro_context:
        return "_(No macro context available.)_"

    indicators = macro_context.get("indicators") or {}
    parts = []

    spy = indicators.get("SPY") or {}
    if spy.get("daily_change_pct") is not None:
        parts.append(f"- SPY today: {spy['daily_change_pct']:.2f}%")
    else:
        parts.append("- SPY today: _(unavailable)_")

    vix = indicators.get("VIX") or {}
    if vix.get("current_price") is not None:
        parts.append(f"- VIX current: {vix['current_price']:.2f}")

    if sector_etf_symbol:
        sec = indicators.get(sector_etf_symbol) or {}
        if sec.get("daily_change_pct") is not None:
            parts.append(
                f"- Sector ETF ({sector_etf_symbol}) today: "
                f"{sec['daily_change_pct']:.2f}%"
            )

    parts.append(
        "\nNote: this is neutral data only — Gate 0 already determined the "
        "ticker's move is not fully explained by macro context. Use this for "
        "your investigation but do not let it bias the conclusion."
    )
    return "\n".join(parts)


def build_user_message(packet: dict) -> str:
    """Render the investigation packet into the user-message text for the LLM."""
    ticker = packet["ticker"]
    sections = [f"# Investigation request: {ticker}\n"]

    sections.append(f"**Current time (UTC):** {datetime.utcnow().isoformat()}Z")
    sections.append(f"**Cadence window:** {packet.get('cadence_window', 'unknown')}\n")

    # Trigger details
    sections.append("## What fired the trigger\n")
    sections.append(_format_signal_decision_for_prompt(packet.get("signal_decision") or {}))
    sections.append("")

    # Anchor (cumulative context)
    anchor = packet.get("anchor") or {}
    if anchor.get("evaluated_at_price"):
        sections.append("## Evaluation anchor\n")
        sections.append(
            f"- Anchor price: {anchor['evaluated_at_price']} "
            f"(set on {anchor.get('evaluated_at', 'unknown')})"
        )
        sections.append(
            "  This is the price at which the existing investment thesis was last built. "
            "Cumulative moves are measured from here."
        )
        sections.append("")

    # Macro context (neutral data)
    sections.append("## Macro context today (neutral data)\n")
    sections.append(_format_macro_context_for_prompt(
        packet.get("macro_context") or {},
        packet.get("sector_etf_symbol"),
    ))
    sections.append("")

    # Thesis
    thesis = packet.get("thesis") or {}
    sections.append("## Existing investment thesis (from Stage 4 selection)\n")
    if thesis:
        sections.append("```json")
        sections.append(json.dumps(thesis, indent=2, default=str))
        sections.append("```")
    else:
        sections.append("_(No existing thesis available — treat the ticker as unfamiliar.)_")
    sections.append("")

    # Instructions
    sections.append("---")
    sections.append("")
    sections.append(
        "Now investigate what is happening with this ticker today. Use web search "
        "to find recent news, press releases, regulatory filings, sector context — "
        "whatever helps explain the signal. Cite every factual claim with "
        "credibility tags. Output the JSON object in a ```json fenced "
        "block, no text outside."
    )

    return "\n".join(sections)


# ============ JSON PARSING ============

def parse_json_from_llm_response(text: str) -> dict:
    """Extract JSON object from response. Raises ValueError on failure.
    Matches the pattern used by llmTriggerClassifier."""
    if not text:
        raise ValueError("Empty response from LLM")

    fence_match = re.search(r"```json\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence_match:
        json_str = fence_match.group(1).strip()
    else:
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start == -1 or brace_end == -1 or brace_end <= brace_start:
            raise ValueError("No JSON object found in response")
        json_str = text[brace_start:brace_end + 1]

    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON parse error: {e}")


# ============ VALIDATION ============

VALID_CONFIDENCE_LEVELS = {"high", "medium", "low"}
VALID_TIME_HORIZONS = {"minutes", "hours", "days", "weeks", "longer"}
VALID_CREDIBILITY_TAGS = {"***", "**", "*"}


def validate_investigation(parsed: dict) -> list:
    """
    Validate the parsed investigation output. Returns list of violation
    strings; empty if valid.
    """
    violations = []

    if not isinstance(parsed, dict):
        return ["Investigation output is not a JSON object"]

    # ticker
    if not isinstance(parsed.get("ticker"), str) or not parsed["ticker"].strip():
        violations.append("'ticker' missing or empty")

    # investigation_summary
    summary = parsed.get("investigation_summary")
    if not isinstance(summary, str) or not summary.strip():
        violations.append("'investigation_summary' missing or empty")

    # established_facts
    facts = parsed.get("established_facts")
    if not isinstance(facts, list):
        violations.append("'established_facts' must be a list")
    else:
        for i, f in enumerate(facts):
            if not isinstance(f, str):
                violations.append(f"'established_facts[{i}]' is not a string")

    # candidate_causes
    causes = parsed.get("candidate_causes")
    if not isinstance(causes, list):
        violations.append("'candidate_causes' must be a list")
    else:
        for i, c in enumerate(causes):
            if not isinstance(c, dict):
                violations.append(f"'candidate_causes[{i}]' is not a dict")
                continue
            for k in ("cause", "supporting_evidence"):
                if not isinstance(c.get(k), str) or not c[k].strip():
                    violations.append(f"'candidate_causes[{i}].{k}' missing or empty")
            conf = c.get("confidence")
            if conf not in VALID_CONFIDENCE_LEVELS:
                violations.append(
                    f"'candidate_causes[{i}].confidence' = {conf!r}; "
                    f"must be one of {sorted(VALID_CONFIDENCE_LEVELS)}"
                )
            horizon = c.get("time_horizon")
            if horizon not in VALID_TIME_HORIZONS:
                violations.append(
                    f"'candidate_causes[{i}].time_horizon' = {horizon!r}; "
                    f"must be one of {sorted(VALID_TIME_HORIZONS)}"
                )

    # relationship_to_existing_thesis
    rel = parsed.get("relationship_to_existing_thesis")
    if not isinstance(rel, str):
        violations.append("'relationship_to_existing_thesis' missing or not a string")

    # sources_consulted
    sources = parsed.get("sources_consulted")
    if not isinstance(sources, list):
        violations.append("'sources_consulted' must be a list")
    else:
        for i, s in enumerate(sources):
            if not isinstance(s, dict):
                violations.append(f"'sources_consulted[{i}]' is not a dict")
                continue
            for k in ("url", "publisher", "date"):
                if k not in s or not isinstance(s[k], str):
                    violations.append(f"'sources_consulted[{i}].{k}' missing or not a string")
            cred = s.get("credibility")
            if cred not in VALID_CREDIBILITY_TAGS:
                violations.append(
                    f"'sources_consulted[{i}].credibility' = {cred!r}; "
                    f"must be one of {sorted(VALID_CREDIBILITY_TAGS)}"
                )

    # search_queries_used
    queries = parsed.get("search_queries_used")
    if not isinstance(queries, list):
        violations.append("'search_queries_used' must be a list")
    else:
        for i, q in enumerate(queries):
            if not isinstance(q, str):
                violations.append(f"'search_queries_used[{i}]' is not a string")

    # investigation_confidence
    inv_conf = parsed.get("investigation_confidence")
    if inv_conf not in VALID_CONFIDENCE_LEVELS:
        violations.append(
            f"'investigation_confidence' = {inv_conf!r}; "
            f"must be one of {sorted(VALID_CONFIDENCE_LEVELS)}"
        )

    # ambiguity_notes — can be empty but must be a string
    if "ambiguity_notes" not in parsed:
        violations.append("'ambiguity_notes' is missing")
    elif not isinstance(parsed["ambiguity_notes"], str):
        violations.append("'ambiguity_notes' must be a string")

    return violations



# ============ PUBLIC ENTRY POINT ============

async def investigate_ticker(
    packet: dict,
    *,
    search_formula: str = None,
    max_search_calls: int = None,
) -> dict:
    """
    Run the investigation LLM call for one ticker.

    Args:
        packet: dict from build_investigation_packet
        search_formula: override SEARCH_FORMULA
        max_search_calls: override MAX_WEB_SEARCH_CALLS

    Returns: result dict (see module docstring). Never raises.
    """
    _ensure_logger()

    ticker = packet.get("ticker", "<unknown>")
    formula = search_formula or SEARCH_FORMULA
    cap = max_search_calls if max_search_calls is not None else MAX_WEB_SEARCH_CALLS

    result = {
        "ticker": ticker,
        "investigation": None,
        "raw_response": None,
        "validation_violations": [],
        "web_search_calls_made": 0,
        "web_search_cap_reached": False,
        "token_usage": {"input_tokens": None, "output_tokens": None,
                        "total_tokens": None},
        "model": MODEL,
        "status": "unknown",
        "error": None,
    }

    api_key = os.getenv("MOONSHOT_API_KEY")
    base_url = os.getenv("MOONSHOT_BASE_URL") or "https://api.moonshot.ai/v1"
    if not api_key:
        result["status"] = "api_failed"
        result["error"] = "MOONSHOT_API_KEY env var not set"
        logger.error(result["error"])
        return result

    try:
        system_prompt = _load_prompt()
    except Exception as e:
        result["status"] = "api_failed"
        result["error"] = f"Could not load prompt: {e}"
        logger.exception("Prompt load failed:")
        return result

    user_message = build_user_message(packet)

    client = FormulaChatClient(
        base_url=base_url, api_key=api_key,
        model=MODEL, max_tokens=MAX_TOKENS,
        max_tool_calls=cap,
        force_answer_instruction=FORCE_ANSWER_INSTRUCTION,
        max_force_answer_attempts=MAX_FORCE_ANSWER_ATTEMPTS,
        httpx_timeout=60.0,
        logger=logger,
    )

    try:
        uri = normalise_formula_uri(formula)
        try:
            raw_tools = await client.get_tools(uri)
        except Exception as e:
            result["status"] = "api_failed"
            result["error"] = f"Could not fetch tools from {uri}: {e}"
            logger.exception("Tool fetch failed:")
            return result

        all_tools = []
        tool_to_uri = {}
        for tool in raw_tools:
            func = tool.get("function")
            if not func:
                continue
            func_name = func.get("name")
            if func_name and func_name not in tool_to_uri:
                all_tools.append(tool)
                tool_to_uri[func_name] = uri

        logger.info(
            f"[{ticker}] Call 1 investigator starting "
            f"(model={MODEL}, search_cap={cap}, tools={len(all_tools)})"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        try:
            initial_response = await client.openai.chat.completions.create(
                model=client.model, messages=messages,
                tools=all_tools if all_tools else None,
                max_tokens=client.max_tokens,
            )
        except Exception as e:
            result["status"] = "api_failed"
            result["error"] = f"Initial LLM call failed: {e}"
            logger.exception(f"[{ticker}] Initial LLM call failed:")
            return result

        try:
            final_text = await client.handle_response(
                initial_response, messages, all_tools, tool_to_uri
            )
        except Exception as e:
            result["status"] = "api_failed"
            result["error"] = f"LLM tool-call loop failed: {e}"
            logger.exception(f"[{ticker}] Tool-call loop failed:")
            result["web_search_calls_made"] = client.tool_call_count
            result["web_search_cap_reached"] = client.cap_reached
            result["token_usage"] = client.token_usage
            return result

        result["raw_response"] = final_text
        result["web_search_calls_made"] = client.tool_call_count
        result["web_search_cap_reached"] = client.cap_reached
        result["token_usage"] = client.token_usage

        logger.info(
            f"[{ticker}] LLM investigation response received "
            f"(searches={client.tool_call_count}/{cap}, "
            f"tokens in={client.token_usage['input_tokens']} "
            f"out={client.token_usage['output_tokens']})"
        )

        try:
            parsed = parse_json_from_llm_response(final_text or "")
        except ValueError as e:
            result["status"] = "parse_failed"
            result["error"] = f"JSON parse error: {e}"
            result["validation_violations"] = [str(e)]
            logger.error(f"[{ticker}] {result['error']}")
            logger.error(
                f"[{ticker}] raw response that failed to parse "
                f"(first 1000 chars): {(final_text or '')[:1000]}"
            )
            return result

        violations = validate_investigation(parsed)
        if violations:
            result["status"] = "validation_failed"
            result["validation_violations"] = violations
            result["investigation"] = parsed
            logger.error(f"[{ticker}] Investigation failed validation: {violations}")
            return result

        result["investigation"] = parsed
        result["status"] = "ok"
        logger.info(
            f"[{ticker}] Investigation ok: "
            f"confidence={parsed.get('investigation_confidence')}, "
            f"causes={len(parsed.get('candidate_causes', []))}, "
            f"sources={len(parsed.get('sources_consulted', []))}"
        )
        return result

    finally:
        await client.close()


# ============ CLI ============

def _setup_cli_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)-7s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[logging.StreamHandler()],
    )


async def _cli_main(args):
    with open(args.packet, "r", encoding="utf-8") as f:
        packet = json.load(f)

    result = await investigate_ticker(packet, max_search_calls=args.max_search)

    print(f"\n{'='*70}")
    print(f"Call 1 investigation result")
    print(f"{'='*70}")
    print(f"  Ticker:           {result['ticker']}")
    print(f"  Status:           {result['status']}")
    print(f"  Model:            {result['model']}")
    print(f"  Web searches:     {result['web_search_calls_made']}")
    print(f"  Cap reached:      {result['web_search_cap_reached']}")
    print(f"  Token usage:      {result['token_usage']}")
    if result["error"]:
        print(f"  Error:            {result['error']}")
    if result["validation_violations"]:
        print(f"  Violations:")
        for v in result["validation_violations"]:
            print(f"    - {v}")
    if result["investigation"]:
        inv = result["investigation"]
        print(f"\n  Summary:")
        print(f"    {inv.get('investigation_summary', '')[:500]}...")
        print(f"\n  Confidence:       {inv.get('investigation_confidence')}")
        print(f"  Causes:           {len(inv.get('candidate_causes', []))}")
        print(f"  Sources:          {len(inv.get('sources_consulted', []))}")
    print(f"{'='*70}\n")


def _parse_cli_args():
    parser = argparse.ArgumentParser(description="Call 1 investigation — debug one packet.")
    parser.add_argument("--packet", type=str, required=True,
                        help="Path to a JSON file containing the investigation packet.")
    parser.add_argument("--max-search", type=int, default=None,
                        help="Override MAX_WEB_SEARCH_CALLS for this run.")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_cli_args()
    _setup_cli_logging()
    asyncio.run(_cli_main(args))