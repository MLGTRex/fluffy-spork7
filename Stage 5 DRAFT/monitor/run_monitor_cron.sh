#!/bin/bash
# Cron wrapper for the Stage 5 price-led monitor.
#
# Invoked by crontab at the four US-market cadence windows (see the crontab
# comments). cron runs with a bare environment, so this loads the project
# .env (Moonshot + Alpaca API keys, and GITHUB_TOKEN) before launching
# runMonitor.py.
#
# After the run it commits any changed pipeline output and pushes it to the
# GitHub repo, so results can be viewed remotely.
#
# By default runs the monitor FULLY LIVE (no flags): it can trigger Stage 2-4
# reruns and place paper trades. Pass --dry-run / --skip-llm when running this
# wrapper manually to test without side effects, e.g.:
#   ./run_monitor_cron.sh --dry-run --skip-llm

PROJECT_ROOT="/Users/benlewis/Documents/claude portfolio"
MONITOR="$PROJECT_ROOT/Stage 5 DRAFT/monitor/runMonitor.py"

cd "$PROJECT_ROOT" || exit 1

# cron has no shell profile — load API keys + GITHUB_TOKEN from .env.
if [ -f "$PROJECT_ROOT/.env" ]; then
  set -a
  . "$PROJECT_ROOT/.env"
  set +a
fi

echo "=== monitor cron run started: $(date) ==="

# Pull the latest remote state before running, so the monitor reads the
# current pipeline output (and not a stale local working tree). Uses the same
# keychain-independent credential helper as the end-of-run push. Best-effort:
# if the pull fails, log a warning and continue against the existing local
# state — the end-of-run push will retry the sync.
cred='!f(){ echo username=x-access-token; echo "password=$GITHUB_TOKEN"; }; f'
echo "=== pulling latest state from origin: $(date) ==="
if ! git -c credential.helper= -c credential.helper="$cred" pull --rebase --autostash origin main; then
  echo "WARNING: pre-run pull failed; continuing against existing local state." >&2
  git rebase --abort 2>/dev/null
fi

/usr/bin/python3 "$MONITOR" "$@"
status=$?
echo "=== monitor run finished: $(date) (exit $status) ==="

# --- Commit & push pipeline state to GitHub (so output is viewable remotely) ---
echo "=== committing pipeline state: $(date) ==="
git add -A
if git diff --cached --quiet; then
  echo "No pipeline state changes to commit."
else
  git -c user.name="MLGTRex" -c user.email="lewisben672@gmail.com" \
    commit -q -m "monitor: state update ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
  # Authenticate with GITHUB_TOKEN from .env. This is keychain-independent so
  # it works from a non-interactive cron context. The token is only ever read
  # from the environment — it never appears on the command line.
  cred='!f(){ echo username=x-access-token; echo "password=$GITHUB_TOKEN"; }; f'
  pushed=0
  for attempt in 1 2 3 4 5; do
    if git -c credential.helper= -c credential.helper="$cred" pull --rebase --autostash origin main \
       && git -c credential.helper= -c credential.helper="$cred" push origin HEAD:main; then
      echo "Pushed pipeline state on attempt $attempt."
      pushed=1
      break
    fi
    git rebase --abort 2>/dev/null
    echo "Push attempt $attempt failed; retrying in 5s..."
    sleep 5
  done
  if [ "$pushed" -ne 1 ]; then
    echo "WARNING: could not push pipeline state after 5 attempts." >&2
    echo "         The commit is saved locally and will push on a later run." >&2
  fi
fi

echo "=== monitor cron run complete: $(date) ==="
exit $status
