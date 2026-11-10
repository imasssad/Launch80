#!/usr/bin/env bash
# Restart Launch80 only when it is actually dead. Safe to run from cron every
# few minutes: the app's own flock means a double start cannot corrupt state,
# and this script exits quietly when the service is healthy.
#
#   */5 * * * * /var/www/vhosts/launch80.everests.fund/launch80/scripts/watchdog.sh
#
# It restarts a DEAD process. It never changes LIVE, BUY_ENABLED or the paused
# flag: a bot paused by its own risk logic stays paused, which is the point.
set -uo pipefail
APP="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$APP/data/watchdog.log"
mkdir -p "$APP/data"

stamp() { date -u +%FT%TZ; }

if pgrep -f "$APP/.venv/bin/launch80 run" >/dev/null 2>&1; then
  # Process is alive; confirm the HTTP monitor still answers.
  if curl -sf --max-time 5 http://127.0.0.1:8088/health >/dev/null 2>&1; then
    exit 0
  fi
  # Deliberately do NOT restart here: killing a live process mid-trade is
  # worse than a stale monitor. Flag it for a human instead.
  echo "$(stamp) WARN process alive but /health unresponsive; leaving it alone" >> "$LOG"
  exit 0
fi

echo "$(stamp) DEAD service not running; restarting" >> "$LOG"
cd "$APP" || exit 1
nohup "$APP/.venv/bin/launch80" run >> "$APP/data/service.log" 2>&1 &
sleep 8
if curl -sf --max-time 5 http://127.0.0.1:8088/health >/dev/null 2>&1; then
  echo "$(stamp) OK restarted, health responding" >> "$LOG"
else
  echo "$(stamp) FAIL restart did not come up; investigate data/service.log" >> "$LOG"
fi
