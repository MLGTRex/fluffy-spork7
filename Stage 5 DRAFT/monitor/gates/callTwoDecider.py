"""
Call 2 — LLM-driven rerun decision gate.

For each ticker that has a completed Call 1 investigation, this module
decides whether to rerun the investment pipeline. Binary decision.

Design:
  - Mirrors the Moonshot API pattern of callOneInvestigator.py
  - NO web search tool — Call 2 only has Call 1's output to work from
  - Single chat completion, no tool-call loop
  - Conservative bias: defaults to no-rerun on parse/validation/API failures
  - Ignores uncited claims (the prompt enforces this rule)

Configuration:
    MODEL                  = kimi-k2.6
    MAX_TOKENS             = 16384

Public API:
    build_decision_packet(ticker, ...) → dict
        Assembles inputs for the decision call.

    decide_rerun(packet, *, ...) → dict
        Sends LLM call, parses, validates. Returns structured result.

Output structure:
    {
        "ticker": str,
        "decision": {                       # parsed LLM output, or None on failure
            "ticker": str,
            "rerun_decision": bool,
            "rationale": str,
            "thesis_elements_touched": [str],
            "evidence_strength": "high"|"medium"|"low",
            "key_facts_relied_on": [str],
            "uncertainty_acknowledged": str,
            "considered_alternative": str,
        },
        "raw_response": str,
        "validation_violations": [str],
        "token_usage": {"input_tokens", "output_tokens", "total_tokens"},
        "model": str,
        "status": "ok" | "parse_failed" | "validation_failed" | "api_failed",
        "error": str | None,
    }

CLI:
    python3 callTwoDecider.py --packet path/to/packet.json
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
    from openai import AsyncOpenAI
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


logger = logging.getLogger(__name__)


# ============ CONFIG ============

MODEL = "kimi-k2.6"
MAX_TOKENS = 16384  # smaller than Call 1; decisions are shorter than investigations


# ============ PATHS ============

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPTS_DIR = os.path.join(SCRIPT_DIR, "prompts")
PROMPT_FILENAME = "call_2_decision.md"
LOGS_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "..", "logs"))


# ============ LOGGER ============

def _ensure_logger():
    if not logger.handlers:
        os.makedirs(LOGS_DIR, exist_ok=True)
        log_filename = os.path.join(
            LOGS_DIR,
            f"call_two_decider_{datetime.now().strftime('%Y-%m-%d')}.log",
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
        raise FileNotFoundError(f"Call 2 decision prompt not found at {path}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ============ PACKET BUILDER ============

def build_decision_packet(
    ticker: str,
    investigation_result: Optional[dict] = None,
    thesis: Optional[dict] = None,
    signal_decision: Optional[dict] = None,
    anchor: Optional[dict] = None,
) -> dict:
    """
    Assemble the structured input for the decider.

    Args:
        ticker: ticker symbol
        investigation_result: full result dict from investigate_ticker. The
                              decider sees the status, the validation
                              violations (if any), and the parsed investigation
                              if it succeeded.
        thesis: dict from candidate_summaries.json for this ticker
        signal_decision: signal aggregator decision (context only)
        anchor: ticker_evaluation_anchors entry (for "thesis built at price X"
                framing in the prompt)
    """
    return {
        "ticker": ticker,
        "investigation_result": investigation_result or {},
        "thesis": thesis or {},
        "signal_decision": signal_decision or {},
        "anchor": anchor or {},
    }


# ============ USER MESSAGE BUILDER ============

def _format_call_one_for_prompt(investigation_result: dict) -> str:
    """Render Call 1's result for the decider's user message."""
    if not investigation_result:
        return (
            "## Call 1 investigation status: NOT AVAILABLE\n\n"
            "No investigation was performed. Default to no-rerun."
        )

    status = investigation_result.get("status", "unknown")
    parts = [f"## Call 1 investigation status: `{status}`\n"]

    if status != "ok":
        parts.append(
            f"The investigation did not complete successfully. "
            f"Error: {investigation_result.get('error', '(no detail)')}"
        )
        if investigation_result.get("validation_violations"):
            parts.append("Validation violations:")
            for v in investigation_result["validation_violations"]:
                parts.append(f"  - {v}")
        parts.append(
            "\nGiven the failed investigation, you have no evidence base. "
            "Per the prompt instructions, default to no-rerun."
        )
        return "\n".join(parts)

    # status == "ok"
    inv = investigation_result.get("investigation") or {}
    if not inv:
        parts.append("Investigation marked OK but no parsed output. Treat as failed.")
        return "\n".join(parts)

    parts.append(f"**Investigation confidence:** {inv.get('investigation_confidence', 'unknown')}\n")

    parts.append("### Summary\n")
    parts.append(inv.get("investigation_summary", "_(no summary)_"))
    parts.append("")

    facts = inv.get("established_facts") or []
    parts.append(f"### Established facts ({len(facts)})\n")
    if facts:
        for f in facts:
            parts.append(f"- {f}")
    else:
        parts.append("_(none recorded)_")
    parts.append("")

    causes = inv.get("candidate_causes") or []
    parts.append(f"### Candidate causes ({len(causes)})\n")
    if causes:
        for c in causes:
            parts.append(
                f"- **{c.get('cause', '(no description)')}** "
                f"[confidence={c.get('confidence', '?')}, "
                f"horizon={c.get('time_horizon', '?')}]"
            )
            evidence = c.get("supporting_evidence", "")
            if evidence:
                parts.append(f"  - Supporting evidence: {evidence}")
    else:
        parts.append("_(none recorded — Call 1 could not pin a cause)_")
    parts.append("")

    parts.append("### Relationship to existing thesis\n")
    parts.append(inv.get("relationship_to_existing_thesis", "_(not stated)_"))
    parts.append("")

    sources = inv.get("sources_consulted") or []
    parts.append(f"### Sources consulted ({len(sources)})\n")
    if sources:
        for s in sources:
            parts.append(
                f"- {s.get('credibility', '?')} "
                f"{s.get('publisher', '?')} ({s.get('date', '?')}): "
                f"{s.get('url', '?')}"
            )
    else:
        parts.append("_(none recorded)_")
    parts.append("")

    ambiguity = inv.get("ambiguity_notes", "")
    if ambiguity.strip():
        parts.append("### Ambiguity notes\n")
        parts.append(ambiguity)
        parts.append("")

    return "\n".join(parts)


def _format_trigger_signal_for_prompt(signal_decision: dict) -> str:
    if not signal_decision:
        return "_(No signal context.)_"

    parts = []
    parts.append(f"- Trigger path: `{signal_decision.get('trigger_path', 'unknown')}`")
    fired = signal_decision.get("fired_signals") or []
    if fired:
        for s in fired:
            parts.append(
                f"- {s.get('signal_type', '?')} signal [{s.get('tier', '?')}]"
            )
    return "\n".join(parts)


def build_user_message(packet: dict) -> str:
    """Render the decision packet into the user-message text for the LLM."""
    ticker = packet["ticker"]
    sections = [f"# Rerun decision request: {ticker}\n"]

    sections.append(f"**Current time (UTC):** {datetime.utcnow().isoformat()}Z\n")

    # Trigger context (brief)
    sections.append("## Mechanical trigger that started this\n")
    sections.append(_format_trigger_signal_for_prompt(packet.get("signal_decision") or {}))
    sections.append("")

    # Anchor context
    anchor = packet.get("anchor") or {}
    if anchor.get("evaluated_at_price"):
        sections.append("## Evaluation anchor\n")
        sections.append(
            f"- Anchor price: {anchor['evaluated_at_price']} "
            f"(set on {anchor.get('evaluated_at', 'unknown')})"
        )
        sections.append(
            "  This is the price at which the existing investment thesis "
            "was last built. If cumulative move is large, the thesis "
            "scenarios may be stale even without new news."
        )
        sections.append("")

    # Call 1 output — the main input
    sections.append(_format_call_one_for_prompt(
        packet.get("investigation_result") or {}
    ))
    sections.append("")

    # Existing thesis
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
        "Now make the rerun decision per the criteria in your system prompt. "
        "Output the JSON object in a ```json fenced block, no text outside."
    )

    return "\n".join(sections)


# ============ JSON PARSING ============

def parse_json_from_llm_response(text: str) -> dict:
    """Extract JSON object from response. Raises ValueError on failure."""
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

VALID_EVIDENCE_STRENGTH = {"high", "medium", "low"}


def validate_decision(parsed: dict) -> list:
    """Validate the parsed decision output. Returns list of violations."""
    violations = []

    if not isinstance(parsed, dict):
        return ["Decision output is not a JSON object"]

    if not isinstance(parsed.get("ticker"), str) or not parsed["ticker"].strip():
        violations.append("'ticker' missing or empty")

    if not isinstance(parsed.get("rerun_decision"), bool):
        violations.append(
            f"'rerun_decision' must be bool, got "
            f"{type(parsed.get('rerun_decision')).__name__}"
        )

    rationale = parsed.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        violations.append("'rationale' missing or empty")

    thesis_touched = parsed.get("thesis_elements_touched")
    if not isinstance(thesis_touched, list):
        violations.append("'thesis_elements_touched' must be a list")
    else:
        for i, t in enumerate(thesis_touched):
            if not isinstance(t, str):
                violations.append(f"'thesis_elements_touched[{i}]' is not a string")

    strength = parsed.get("evidence_strength")
    if strength not in VALID_EVIDENCE_STRENGTH:
        violations.append(
            f"'evidence_strength' = {strength!r}; "
            f"must be one of {sorted(VALID_EVIDENCE_STRENGTH)}"
        )

    facts = parsed.get("key_facts_relied_on")
    if not isinstance(facts, list):
        violations.append("'key_facts_relied_on' must be a list")
    else:
        for i, f in enumerate(facts):
            if not isinstance(f, str):
                violations.append(f"'key_facts_relied_on[{i}]' is not a string")

    for key in ("uncertainty_acknowledged", "considered_alternative"):
        if key not in parsed:
            violations.append(f"'{key}' is missing")
        elif not isinstance(parsed[key], str):
            violations.append(f"'{key}' must be a string")

    return violations


# ============ PUBLIC ENTRY POINT ============

async def decide_rerun(packet: dict) -> dict:
    """
    Run the rerun decision LLM call for one ticker.

    Args:
        packet: dict from build_decision_packet

    Returns: result dict (see module docstring). Never raises.

    Conservative behavior on failures:
        - If Call 1 was failed/missing, the prompt is instructed to default
          no-rerun. The decision will reflect that.
        - If THIS call's API/parse/validation fails, status reflects that
          and `decision` is None. The orchestrator should treat as no-rerun.
    """
    _ensure_logger()

    ticker = packet.get("ticker", "<unknown>")

    result = {
        "ticker": ticker,
        "decision": None,
        "raw_response": None,
        "validation_violations": [],
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

    try:
        openai = AsyncOpenAI(base_url=base_url, api_key=api_key)
    except Exception as e:
        result["status"] = "api_failed"
        result["error"] = f"Could not initialize OpenAI client: {e}"
        logger.exception("Client init failed:")
        return result

    logger.info(f"[{ticker}] Call 2 decider starting (model={MODEL})")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    try:
        response = await openai.chat.completions.create(
            model=MODEL,
            messages=messages,
            max_tokens=MAX_TOKENS,
        )
    except Exception as e:
        result["status"] = "api_failed"
        result["error"] = f"LLM call failed: {e}"
        logger.exception(f"[{ticker}] LLM call failed:")
        return result

    # Token usage
    usage = getattr(response, "usage", None)
    if usage:
        result["token_usage"]["input_tokens"] = getattr(usage, "prompt_tokens", None)
        result["token_usage"]["output_tokens"] = getattr(usage, "completion_tokens", None)
        if result["token_usage"]["input_tokens"] is not None and \
                result["token_usage"]["output_tokens"] is not None:
            result["token_usage"]["total_tokens"] = (
                result["token_usage"]["input_tokens"]
                + result["token_usage"]["output_tokens"]
            )

    try:
        final_text = response.choices[0].message.content
    except (AttributeError, IndexError) as e:
        result["status"] = "api_failed"
        result["error"] = f"Could not extract response content: {e}"
        logger.exception(f"[{ticker}] Response extraction failed:")
        return result

    result["raw_response"] = final_text

    logger.info(
        f"[{ticker}] Call 2 response received "
        f"(tokens in={result['token_usage']['input_tokens']} "
        f"out={result['token_usage']['output_tokens']})"
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

    violations = validate_decision(parsed)
    if violations:
        result["status"] = "validation_failed"
        result["validation_violations"] = violations
        result["decision"] = parsed  # surface for inspection
        logger.error(f"[{ticker}] Decision failed validation: {violations}")
        return result

    result["decision"] = parsed
    result["status"] = "ok"
    logger.info(
        f"[{ticker}] Decision: rerun={parsed.get('rerun_decision')}, "
        f"strength={parsed.get('evidence_strength')}"
    )
    return result


# ============ CONVENIENCE: orchestrator-facing helper ============

def orchestrator_should_rerun(decision_result: dict) -> bool:
    """
    Conservative interpretation of a decision result.

    Returns True only when:
      - status == "ok"
      - decision is parsed
      - rerun_decision == True

    Any other state (parse failure, validation failure, API failure, missing)
    returns False. The orchestrator should never rerun on a failed decision.
    """
    if not decision_result:
        return False
    if decision_result.get("status") != "ok":
        return False
    decision = decision_result.get("decision") or {}
    return decision.get("rerun_decision") is True


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

    result = await decide_rerun(packet)

    print(f"\n{'='*70}")
    print(f"Call 2 decision result")
    print(f"{'='*70}")
    print(f"  Ticker:           {result['ticker']}")
    print(f"  Status:           {result['status']}")
    print(f"  Model:            {result['model']}")
    print(f"  Token usage:      {result['token_usage']}")
    if result["error"]:
        print(f"  Error:            {result['error']}")
    if result["validation_violations"]:
        print(f"  Violations:")
        for v in result["validation_violations"]:
            print(f"    - {v}")
    if result["decision"]:
        d = result["decision"]
        print(f"\n  Rerun decision:   {d.get('rerun_decision')}")
        print(f"  Evidence strength: {d.get('evidence_strength')}")
        print(f"  Rationale:")
        print(f"    {d.get('rationale', '')[:500]}...")
        print(f"  Thesis elements:  {d.get('thesis_elements_touched', [])}")
    print(f"\n  Orchestrator should rerun? {orchestrator_should_rerun(result)}")
    print(f"{'='*70}\n")


def _parse_cli_args():
    parser = argparse.ArgumentParser(description="Call 2 decision — debug one packet.")
    parser.add_argument("--packet", type=str, required=True,
                        help="Path to a JSON file containing the decision packet.")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_cli_args()
    _setup_cli_logging()
    asyncio.run(_cli_main(args))