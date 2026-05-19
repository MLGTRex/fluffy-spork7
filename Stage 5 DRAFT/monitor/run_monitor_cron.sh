#!/bin/bash
# Cron wrapper for the Stage 5 price-led monitor.
#
# Invoked by crontab at the four US-market cadence windows (see the crontab
# comments). cron runs with a bare environment, so this loads the project
# .env (Moonshot + Alpaca API keys) before launching runMonitor.py.
#
# By default runs the monitor FULLY LIVE (no flags): it can trigger Stage 2-4
# reruns and place paper trades. Pass --dry-run / --skip-llm when running this
# wrapper manually to test without side effects, e.g.:
#   ./run_monitor_cron.sh --dry-run --skip-llm

PROJECT_ROOT="/Users/benlewis/Documents/claude portfolio"
MONITOR="$PROJECT_ROOT/Stage 5 DRAFT/monitor/runMonitor.py"

cd "$PROJECT_ROOT" || exit 1

# cron has no shell profile — load API keys from .env.
if [ -f "$PROJECT_ROOT/.env" ]; then
  set -a
  . "$PROJECT_ROOT/.env"
  set +a
fi

echo "=== monitor cron run started: $(date) ==="
/usr/bin/python3 "$MONITOR" "$@"
status=$?
echo "=== monitor cron run finished: $(date) (exit $status) ==="
exit $status
