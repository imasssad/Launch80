#!/usr/bin/env bash
# Generate dashboard credentials, write them to .env, restart, verify.
#
# The password is generated, never typed, so it cannot be a reused login and
# never reaches shell history. It is printed ONCE: save it to a password
# manager before closing the terminal. Re-running issues a fresh password and
# invalidates the old one.
#
#   ./scripts/enable-dashboard-auth.sh
set -euo pipefail
APP="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP"

test -x .venv/bin/python || { echo "ERROR: venv missing; run deploy.sh first"; exit 1; }
test -f .env || { echo "ERROR: no .env in $APP"; exit 1; }

PW=$(.venv/bin/python -c 'import secrets,string
alphabet = string.ascii_letters + string.digits
print("".join(secrets.choice(alphabet) for _ in range(28)))')
SECRET=$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(48))')

cp .env ".env.bak.$(date -u +%Y%m%dT%H%M%SZ)"
# Drop any previous values, then append the new ones, so re-running replaces
# rather than accumulating duplicate keys.
grep -vE '^(ADMIN_PASSWORD|SESSION_SECRET|COOKIE_SECURE)=' .env > .env.tmp
{
  echo "ADMIN_PASSWORD=$PW"
  echo "SESSION_SECRET=$SECRET"
  echo "COOKIE_SECURE=true"
} >> .env.tmp
mv .env.tmp .env
chmod 600 .env

echo "== restarting =="
pkill -TERM -f "$APP/.venv/bin/launch80 run" 2>/dev/null || true
for _ in $(seq 1 30); do pgrep -f "$APP/.venv/bin/launch80 run" >/dev/null 2>&1 || break; sleep 1; done
mkdir -p data
nohup "$APP/.venv/bin/launch80" run >> "$APP/data/service.log" 2>&1 &
sleep 10

echo "== verifying the gate =="
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8088/positions || echo 000)
if [ "$code" != "401" ]; then
  echo "FAILED: /positions returned $code, expected 401. Auth is NOT protecting data."
  echo "Check $APP/data/service.log before exposing this to a network."
  exit 1
fi
echo "  /positions without a session -> 401 (correct)"
curl -s http://127.0.0.1:8088/health; echo

cat <<NOTE

=========================================================================
  DASHBOARD PASSWORD (shown once - save it now):

      $PW

  Username: anything. Sign in at your domain, or through an SSH tunnel:
      ssh -L 8088:127.0.0.1:8088 $(id -un)@<server>   then http://localhost:8088/

  Stored in $APP/.env (chmod 600). Previous .env backed up alongside it.
  Re-run this script to rotate; it invalidates the old password.
=========================================================================
NOTE
