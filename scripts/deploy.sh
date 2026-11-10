#!/usr/bin/env bash
# Launch80 server deployment -> /var/www/launch80
# Safe by default: LIVE=false, BUY_ENABLED=false. No keys are included or created.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TARGET="${TARGET:-/var/www/launch80}"

echo "== preflight: python =="
command -v python3 >/dev/null || { echo "ERROR: python3 not found; install Python 3.11-3.13 first"; exit 1; }
python3 - <<'P'
import sys
v=sys.version_info
ok=(3,11)<=(v.major,v.minor)<(3,14)
print(f"python {v.major}.{v.minor}.{v.micro} -> {'ok' if ok else 'UNSUPPORTED: need 3.11-3.13'}")
sys.exit(0 if ok else 1)
P

echo "== create $TARGET =="
if mkdir -p "$TARGET" 2>/dev/null && [ -w "$TARGET" ]; then
  echo "created (no sudo needed)"
else
  echo "/var/www needs sudo:"
  sudo mkdir -p "$TARGET"
  sudo chown "$(id -un)":"$(id -gn)" "$TARGET"
fi

echo "== copy app =="
cp -r "$HERE/app/." "$TARGET/"
cd "$TARGET"

echo "== venv + install =="
python3 -m venv .venv
. .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -e '.[dev]'

echo "== test gate (must pass) =="
python -m pytest -q

echo "== safe config =="
if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s|^DB_PATH=.*|DB_PATH=$TARGET/data/launch80-paper.sqlite|" .env
fi
chmod 600 .env
grep -E '^(LIVE|BUY_ENABLED)=' .env
echo "(edit .env yourself for any change; never commit it; keys go ONLY in this file on this server)"

echo "== read-only connectivity check =="
launch80 check

echo "== start service =="
mkdir -p data
if pgrep -f 'launch80 run' >/dev/null 2>&1; then
  echo "already running"
else
  nohup "$TARGET/.venv/bin/launch80" run > "$TARGET/data/service.log" 2>&1 &
  sleep 5
fi
curl -sf http://127.0.0.1:8088/health && echo

cat <<NOTE

DEPLOYED (paper mode, buys disabled) at: $TARGET
- Monitor: http://127.0.0.1:8088 (loopback only BY DESIGN - never expose it publicly;
  /positions and /events show sensitive data).
  View from your laptop: ssh -L 8088:127.0.0.1:8088 $(id -un)@<server>
- Enable paper buys: set BUY_ENABLED=true in $TARGET/.env then restart the service.
- Restart manually:
    pkill -TERM -f 'launch80 run'; sleep 5
    nohup $TARGET/.venv/bin/launch80 run > $TARGET/data/service.log 2>&1 &
- Runbook (run from $TARGET):
    .venv/bin/launch80 stats | positions | events | pause | resume | reconcile | abandon
- Supervised process: shared hosting has no user systemd. If you move to a VPS with
  root, create /etc/systemd/system/launch80.service with
  User=$(id -un), WorkingDirectory=$TARGET,
  ExecStart=$TARGET/.venv/bin/launch80 run, Restart=on-failure, TimeoutStopSec=150
NOTE
