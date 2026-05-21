"""
Test pipeline driver.

Replaces Stage 1's `target_company_list.json` with arbitrary test tickers,
runs the orchestrator with `--skip-stage-1` so Stages 2–4 process the test
list, then restores the original file in a `finally` block — even if the
orchestrator crashes or is interrupted.

This is a test-only utility. Production paths
(`pipeline tools/orchestrator.py`, `Stage 5 DRAFT/pipeline invoker/`, etc.)
never call it. Deleting this script + `.github/workflows/test_pipeline.yml`
leaves the rest of the pipeline functioning exactly as today.

CLI:
    python pipeline\\ tools/runTestPipeline.py --tickers "MSFT,NVDA"
    python pipeline\\ tools/runTestPipeline.py --tickers "Microsoft Corp (MSFT),Nvidia (NVDA)"

A bare ticker like `MSFT` is normalised to `"MSFT (MSFT)"` — Stage 2's
parser accepts the synthetic form. Pass a full `"Name (TICKER)"` if you
want Stage 2's research agents to see a real company name.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))

TARGET_LIST = os.path.join(REPO_ROOT, "Stage 1 DRAFT", "output", "target_company_list.json")
BACKUP_PATH = os.path.join(
    REPO_ROOT, "Stage 1 DRAFT", "output", ".target_company_list.json.test-backup"
)
ORCHESTRATOR = os.path.join(REPO_ROOT, "pipeline tools", "orchestrator.py")

# Matches "Anything (TICKER)" at end of string; TICKER allows letters, digits, . and -.
_TICKER_TAIL = re.compile(r"\(([A-Z][A-Z0-9.\-]*)\)\s*$")


def parse_entries(raw: str) -> list[str]:
    """Comma-split + normalise each entry to "Name (TICKER)" form."""
    out: list[str] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if _TICKER_TAIL.search(piece):
            out.append(piece)
        else:
            t = piece.upper()
            out.append(f"{t} ({t})")
    return out


def banner(msg: str) -> None:
    print(f"[Test pipeline] {msg}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Stages 2-4 against arbitrary test tickers."
    )
    parser.add_argument(
        "--tickers",
        required=True,
        help="Comma-separated entries. Each is either a bare ticker (e.g. MSFT) "
             "or already 'Name (TICKER)'.",
    )
    args = parser.parse_args()

    entries = parse_entries(args.tickers)
    if not entries:
        banner("ERROR: --tickers produced no entries after parsing.")
        return 1

    for required in (TARGET_LIST, ORCHESTRATOR):
        if not os.path.isfile(required):
            banner(f"ERROR: required file missing: {required}")
            return 1

    started = datetime.now(timezone.utc).isoformat()
    banner(f"Starting test run at {started}")
    banner(f"Test target list ({len(entries)}): {entries}")

    shutil.copy2(TARGET_LIST, BACKUP_PATH)
    banner(f"Backed up original target list → {os.path.basename(BACKUP_PATH)}")

    exit_code = 1
    try:
        with open(TARGET_LIST, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2, ensure_ascii=False)
        banner("Wrote test target list. Invoking orchestrator with --skip-stage-1...")

        result = subprocess.run(
            ["python", ORCHESTRATOR, "--skip-stage-1"],
            cwd=REPO_ROOT,
            check=False,
        )
        exit_code = result.returncode
        banner(f"Orchestrator exited with code {exit_code}.")
    finally:
        if os.path.isfile(BACKUP_PATH):
            shutil.move(BACKUP_PATH, TARGET_LIST)
            banner("Restored original target_company_list.json.")
        else:
            banner(
                "WARNING: backup was missing during restore; target list may "
                "be in the test state. Manual restore required."
            )

    ended = datetime.now(timezone.utc).isoformat()
    banner(f"Test run complete at {ended}. Exit code: {exit_code}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
