"""
Reconciliation Debate Adjudicator.

Settles one proposed portfolio change with an adversarial debate. Mirrors the
pipeline's existing `case -> rebuttal -> synthesis` flow (single round), but is
self-contained: the repo's debate modules each hardcode their own client and
bull/bear prompts, so this module reuses the *pattern* rather than importing them,
keeping the shared Stage-1/2 debate code untouched.

A debate has two sides:
    - the status-quo side, arguing to RESIST the proposed change
    - the change side, arguing to MAKE the proposed change

The judge returns a signed score: positive => resist the change, negative => make
it. Threshold 0.
"""

import os
import re
import json
import asyncio
import logging

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

MODEL = "kimi-k2.6"
MAX_TOKENS = 32768

STAGE4_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(STAGE4_ROOT, "prompts")


def _load_prompt(name: str) -> str:
    path = os.path.join(PROMPTS_DIR, name)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Reconciliation debate prompt not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


async def _call_llm(client: AsyncOpenAI, system_prompt: str, user_message: str) -> str:
    response = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        max_tokens=MAX_TOKENS,
    )
    return response.choices[0].message.content


def _extract_verdict(content: str) -> dict:
    """Extract the trailing JSON verdict block from the synthesis output."""
    result = {"score": None, "categorical": "", "score_confidence": ""}
    matches = re.findall(r'```json\s*(\{.*?\})\s*```', content or "", re.DOTALL)
    if not matches:
        matches = re.findall(r'(\{[^{}]*"score"[^{}]*\})', content or "", re.DOTALL)
    if not matches:
        logger.warning("No JSON verdict block found in reconciliation synthesis.")
        return result
    try:
        parsed = json.loads(matches[-1])
        result["score"] = parsed.get("score")
        result["categorical"] = parsed.get("categorical", "")
        result["score_confidence"] = parsed.get("score_confidence", "")
    except json.JSONDecodeError as e:
        logger.warning(f"Reconciliation verdict JSON parse failed: {e}")
    return result


def _framing(change_kind: str, company_name: str) -> dict:
    """Human-readable framing of the change under debate."""
    if change_kind == "drop_incumbent":
        return {
            "description": (
                f"DROP {company_name} — a position the portfolio currently holds."
            ),
            "keep_instruction": (
                f"You are the STATUS-QUO side. Argue to RETAIN {company_name} in "
                f"the portfolio."
            ),
            "change_instruction": (
                f"You are the CHANGE side. Argue to DROP {company_name} from the "
                f"portfolio."
            ),
        }
    if change_kind == "add_name":
        return {
            "description": (
                f"ADD {company_name} — a name the portfolio does not currently hold."
            ),
            "keep_instruction": (
                f"You are the STATUS-QUO side. Argue to keep the status quo and "
                f"NOT add {company_name}."
            ),
            "change_instruction": (
                f"You are the CHANGE side. Argue to ADD {company_name} to the "
                f"portfolio."
            ),
        }
    raise ValueError(f"Unknown change_kind: {change_kind}")


async def adjudicate(change_kind: str, ticker: str, company_name: str,
                     research_dump: str) -> dict:
    """
    Run one single-round debate over a proposed change.

    Args:
        change_kind: "drop_incumbent" or "add_name".
        ticker: ticker under debate (for logging).
        company_name: display name used in the debate framing.
        research_dump: assembled evidence (original thesis, delta research,
            realized P&L, candidate rationale, staleness notes).

    Returns:
        {
            "score": int | None,        # +ve => resist change, -ve => make change
            "categorical": str,
            "score_confidence": str,
            "resist_change": bool,      # True => keep status quo
            "keep_case", "change_case", "keep_rebuttal", "change_rebuttal",
            "synthesis": str,
        }
    """
    frame = _framing(change_kind, company_name)
    keep_case_prompt = _load_prompt("reconciliation_keep_case.md")
    change_case_prompt = _load_prompt("reconciliation_change_case.md")
    rebuttal_prompt = _load_prompt("reconciliation_rebuttal.md")
    synthesis_prompt = _load_prompt("reconciliation_synthesis.md")

    api_key = os.getenv("MOONSHOT_API_KEY")
    base_url = os.getenv("MOONSHOT_BASE_URL") or "https://api.moonshot.ai/v1"
    if not api_key:
        raise EnvironmentError("Missing MOONSHOT_API_KEY environment variable")
    client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    try:
        logger.info(f"[{ticker}] reconciliation debate: {frame['description']}")

        def case_user(instruction):
            return (
                f"# THE CHANGE UNDER DEBATE\n\n{frame['description']}\n\n"
                f"{instruction}\n\n"
                f"---\n\n# RESEARCH DUMP\n\n{research_dump}"
            )

        keep_case, change_case = await asyncio.gather(
            _call_llm(client, keep_case_prompt, case_user(frame["keep_instruction"])),
            _call_llm(client, change_case_prompt, case_user(frame["change_instruction"])),
        )

        def rebuttal_user(side_instruction, own_case, opposing_case):
            return (
                f"# THE CHANGE UNDER DEBATE\n\n{frame['description']}\n\n"
                f"{side_instruction}\n\n"
                f"---\n\n# YOUR OPENING CASE\n\n{own_case}\n\n"
                f"---\n\n# THE OPPOSING CASE TO REBUT\n\n{opposing_case}\n\n"
                f"---\n\n# RESEARCH DUMP (REFERENCE)\n\n{research_dump}"
            )

        keep_rebuttal, change_rebuttal = await asyncio.gather(
            _call_llm(client, rebuttal_prompt,
                      rebuttal_user(frame["keep_instruction"], keep_case, change_case)),
            _call_llm(client, rebuttal_prompt,
                      rebuttal_user(frame["change_instruction"], change_case, keep_case)),
        )

        synthesis_user = (
            f"# THE CHANGE UNDER DEBATE\n\n{frame['description']}\n\n"
            f"---\n\n# STATUS-QUO CASE\n\n{keep_case}\n\n"
            f"---\n\n# CHANGE CASE\n\n{change_case}\n\n"
            f"---\n\n# STATUS-QUO REBUTTAL\n\n{keep_rebuttal}\n\n"
            f"---\n\n# CHANGE REBUTTAL\n\n{change_rebuttal}"
        )
        synthesis = await _call_llm(client, synthesis_prompt, synthesis_user)
    finally:
        await client.close()

    verdict = _extract_verdict(synthesis)
    score = verdict["score"]
    # Unparseable verdict defaults to resisting the change (no arbitrary churn).
    resist_change = score is None or score >= 0

    logger.info(
        f"[{ticker}] debate verdict: score={score} "
        f"categorical={verdict['categorical']} resist_change={resist_change}"
    )

    return {
        "score": score,
        "categorical": verdict["categorical"],
        "score_confidence": verdict["score_confidence"],
        "resist_change": resist_change,
        "keep_case": keep_case,
        "change_case": change_case,
        "keep_rebuttal": keep_rebuttal,
        "change_rebuttal": change_rebuttal,
        "synthesis": synthesis,
    }
