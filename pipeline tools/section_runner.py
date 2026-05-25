"""
Per-section completeness gating for pipeline stage runners.

A "section" is one of: deep research, debate cases, debate rebuttal, debate
synthesis, research digest, scenarios phase 1/2/3, valuation metrics,
consolidation. Each section is invoked once by its stage's main.py and is
expected to produce specific deliverable fields for every target company.

Today each section runs once over the target list with bounded concurrency
and exits 0 regardless of partial failures. The orchestrator catches the
gap at the stage boundary and respawns the whole stage subprocess. That
costs a cold start per retry, and any single-attempt failure inside the
stage forces every downstream section in that run to skip the affected
company.

This helper keeps the retry loop *inside* the section: run the work,
re-check completeness via a caller-supplied predicate, retry just the
still-incomplete subset, repeat up to a configurable budget. On exhaustion
the helper returns a structured result; the caller decides whether to
sys.exit(1) (the current convention is hard halt, per pipeline-wide
agreement). The orchestrator's outer retry budget remains as a safety net
for issues that survive even N internal attempts (e.g. full API outage).

First attempt always runs `process_one` on the full target list so the
per-company freshness / staleness / reset logic that already lives inside
process_one fires normally. Subsequent attempts only rerun the subset
still failing the predicate.

Retry budgets are read from pipeline tools/section_attempts.json. Missing
keys fall back to defaults.max_attempts; a corrupt or missing config falls
back to FALLBACK_MAX_ATTEMPTS (3).
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable

FALLBACK_MAX_ATTEMPTS = 3
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "section_attempts.json")


@dataclass
class SectionResult:
    section_key: str
    attempts_used: int
    incomplete_companies: list = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return not self.incomplete_companies


def _max_attempts(section_key: str) -> int:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        sections = cfg.get("sections", {}) or {}
        if section_key in sections:
            return int(sections[section_key])
        defaults = cfg.get("defaults", {}) or {}
        if "max_attempts" in defaults:
            return int(defaults["max_attempts"])
    except Exception as e:
        print(f"[section_runner] WARNING: could not read {_CONFIG_PATH}: {e}; using fallback={FALLBACK_MAX_ATTEMPTS}")
    return FALLBACK_MAX_ATTEMPTS


async def run_section_until_complete(
    target_companies: list,
    process_one: Callable[[str], Awaitable[None]],
    is_complete: Callable[[str], bool],
    *,
    section_key: str,
    concurrency: int,
) -> SectionResult:
    """
    Drive a section to completeness across the target list.

    Parameters
    ----------
    target_companies : list[str]
        Caller's full target list (e.g. read from target_company_list.json).
    process_one : async (target_company) -> None
        Per-company worker. Should swallow its own exceptions and write
        progress to disk; the helper catches anything that escapes.
    is_complete : (target_company) -> bool
        Predicate that reads the company's persisted JSON and returns True
        iff this section's deliverable fields are present and non-empty.
        Should NOT raise; treat missing file / parse error as "not complete".
    section_key : str
        Looked up in section_attempts.json for the retry budget.
    concurrency : int
        Semaphore size — matches existing per-section COMPANY_CONCURRENCY.
    """
    max_attempts = _max_attempts(section_key)

    if not target_companies:
        print(f"[{section_key}] no target companies; nothing to do.")
        return SectionResult(section_key=section_key, attempts_used=0, incomplete_companies=[])

    sem = asyncio.Semaphore(concurrency)

    async def bounded(company):
        async with sem:
            try:
                await process_one(company)
            except Exception as e:
                print(f"[{section_key}] {company} crashed during process_one: {e}")

    # Attempt 1: run for everyone. Lets the per-company freshness/staleness/reset
    # logic inside process_one fire normally — already-complete companies are no-ops.
    print(f"[{section_key}] attempt 1/{max_attempts} on {len(target_companies)} companies")
    await asyncio.gather(*(bounded(c) for c in target_companies), return_exceptions=True)

    incomplete = [c for c in target_companies if not is_complete(c)]
    attempt = 1
    if not incomplete:
        print(f"[{section_key}] complete after attempt 1 ({len(target_companies)} companies).")
        return SectionResult(section_key=section_key, attempts_used=1, incomplete_companies=[])

    # Retries: only process the still-incomplete subset.
    while incomplete and attempt < max_attempts:
        attempt += 1
        print(
            f"[{section_key}] {len(incomplete)} incomplete after attempt {attempt - 1}; "
            f"retrying (attempt {attempt}/{max_attempts}): {incomplete}"
        )
        await asyncio.gather(*(bounded(c) for c in incomplete), return_exceptions=True)
        incomplete = [c for c in incomplete if not is_complete(c)]

    if incomplete:
        print(
            f"[{section_key}] EXHAUSTED {max_attempts} attempts; "
            f"{len(incomplete)} companies still incomplete: {incomplete}"
        )
    else:
        print(f"[{section_key}] complete after {attempt} attempt(s).")

    return SectionResult(section_key=section_key, attempts_used=attempt, incomplete_companies=incomplete)
