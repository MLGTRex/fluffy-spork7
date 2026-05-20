"""
Stage 6 configuration.

Every upstream path used by Stage 6 lives here. The independence principle
requires Stage 6 to never import from another stage's source tree; it only
ever reads JSON/CSV files at these paths.

Stage 6 writes are confined to the directories under SCRIPT_DIR.
"""

import os
import re

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))


# ============ Upstream (read-only) ============

STAGE1_OUTPUT = os.path.join(REPO_ROOT, "Stage 1 DRAFT", "output")
STAGE1_COMPANY_DATA = os.path.join(STAGE1_OUTPUT, "company_data")

STAGE2_OUTPUT = os.path.join(REPO_ROOT, "Stage 2 DRAFT", "output")
STAGE3_OUTPUT = os.path.join(REPO_ROOT, "Stage 3 DRAFT", "output")

STAGE4_OUTPUT = os.path.join(REPO_ROOT, "Stage 4 DRAFT", "output")
STAGE4_PORTFOLIO_HISTORY = os.path.join(STAGE4_OUTPUT, "portfolio history")
STAGE4_EXECUTION_OUTPUT = os.path.join(
    REPO_ROOT, "Stage 4 DRAFT", "portfolio execution", "output"
)

STAGE5_STATE = os.path.join(REPO_ROOT, "Stage 5 DRAFT", "state")
STAGE5_MONITOR_OUTPUT = os.path.join(REPO_ROOT, "Stage 5 DRAFT", "monitor", "output")

UPSTREAM_LOG_ROOTS = [
    os.path.join(REPO_ROOT, "Stage 1 DRAFT"),
    os.path.join(REPO_ROOT, "Stage 2 DRAFT"),
    os.path.join(REPO_ROOT, "Stage 3 DRAFT"),
    os.path.join(REPO_ROOT, "Stage 4 DRAFT"),
    os.path.join(REPO_ROOT, "Stage 5 DRAFT"),
    os.path.join(REPO_ROOT, "pipeline tools"),
]


# ============ Stage 6 (write) ============

BACKUPS_DIR = os.path.join(SCRIPT_DIR, "backups")
SNAPSHOTS_DIR = os.path.join(BACKUPS_DIR, "snapshots")
LOGS_ARCHIVE_DIR = os.path.join(BACKUPS_DIR, "logs")

CACHE_DIR = os.path.join(SCRIPT_DIR, "cache")
STATE_DIR = os.path.join(SCRIPT_DIR, "state")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
PER_TICKER_DOSSIER_DIR = os.path.join(OUTPUT_DIR, "per_ticker_dossier")
RUNS_DIR = os.path.join(SCRIPT_DIR, "runs")

PREDICTION_LOG_PATH = os.path.join(STATE_DIR, "prediction_log.jsonl")

ALPACA_ACCOUNT_CACHE = os.path.join(CACHE_DIR, "alpaca_account.json")
ALPACA_POSITIONS_CACHE = os.path.join(CACHE_DIR, "alpaca_positions.json")
ALPACA_PORTFOLIO_HISTORY_CACHE = os.path.join(CACHE_DIR, "alpaca_portfolio_history.json")
BENCHMARK_CACHE = os.path.join(CACHE_DIR, "benchmark_bars.json")


# ============ Alpaca / market data ============

ALPACA_API_KEY_ENV = "ALPACA_API_KEY"
ALPACA_SECRET_KEY_ENV = "ALPACA_SECRET_KEY"
ALPACA_PAPER = True

BENCHMARK_SYMBOL = "SPY"
PORTFOLIO_HISTORY_PERIOD = "1A"
PORTFOLIO_HISTORY_TIMEFRAME = "1D"


# ============ Snapshot scope ============
#
# {section: [(directory, glob_pattern), ...]}
# Listed once, both runSnapshot and buildIndices consult this.

SNAPSHOT_SOURCES = {
    "stage1": [
        (STAGE1_OUTPUT, "*.json"),
        (STAGE1_COMPANY_DATA, "*.json"),
    ],
    "stage2": [
        (STAGE2_OUTPUT, "*_research.json"),
    ],
    "stage3": [
        (STAGE3_OUTPUT, "*_research.json"),
    ],
    "stage4": [
        (STAGE4_OUTPUT, "*.json"),
        (STAGE4_PORTFOLIO_HISTORY, "*.json"),
    ],
    "stage4_execution": [
        (STAGE4_EXECUTION_OUTPUT, "execution_*.json"),
    ],
    "stage5": [
        (STAGE5_STATE, "*.json"),
        (STAGE5_MONITOR_OUTPUT, "monitor_run_*.json"),
    ],
}


# ============ Helpers ============

_TICKER_REGEX = re.compile(r"\(([A-Z][A-Z0-9.\-]*)\)")


def extract_ticker_from_company_string(name: str):
    """Pulls 'MSFT' out of strings like 'Microsoft Corp (MSFT)'."""
    if not name:
        return None
    m = _TICKER_REGEX.search(name)
    return m.group(1) if m else None


def ensure_stage6_dirs():
    """Create every Stage 6 write directory if missing. Safe to call repeatedly."""
    for d in [
        BACKUPS_DIR,
        SNAPSHOTS_DIR,
        LOGS_ARCHIVE_DIR,
        CACHE_DIR,
        STATE_DIR,
        OUTPUT_DIR,
        PER_TICKER_DOSSIER_DIR,
        RUNS_DIR,
    ]:
        os.makedirs(d, exist_ok=True)
