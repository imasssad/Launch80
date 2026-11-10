#!/usr/bin/env bash
# Runs ON the server after each code sync (called by .github/workflows/deploy.yml).
# Never touches .env, data/ or the SQLite journal. Restart is graceful: an
# in-flight trade quarantines to RECOVERY_* and pauses, by design.
set -euo pipefail
cd "$(dirname "$0")/.."

test -x .venv/bin/pip || { echo "ERROR: venv missing - run the initial deploy.sh first"; exit 1; }

echo "== install =="
.venv/bin/pip install --quiet -e '.[dev]'

echo "== test gate on the server =="
.venv/bin/python -m pytest -q

echo "== graceful restart =="
if pgrep -f 'launch80 run' >/dev/null 2>&1; then
  pkill -TERM -f 'launch80 run' || true
  for i in $(seq 1 30); do pgrep -f 'launch80 run' >/dev/null 2>&1 || break; sleep 1; done
  pgrep -f 'launch80 run' >/dev/null 2>&1 && { echo "ERROR: old service did not stop"; exit 1; }
fi
mkdir -p data
nohup "$PWD/.venv/bin/launch80" run > data/service.log 2>&1 &
sleep 6

echo "== health =="
curl -sf http://127.0.0.1:8088/health
echo
echo "deploy OK $(date -u +%FT%TZ)"
