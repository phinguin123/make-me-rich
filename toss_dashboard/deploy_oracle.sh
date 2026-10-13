#!/usr/bin/env bash
# Copy the trader to a server over SSH and (re)start it in Docker.
#
#   ./deploy_oracle.sh ubuntu@<server-ip>              copy code + .env, build, restart
#   ./deploy_oracle.sh ubuntu@<server-ip> --no-start   copy + build only
#
# State (backend/data/*.json) is copied only when the server has none, so a running
# server's positions and P&L are never overwritten. Research data is not copied.
# Only one place may run the bot: a second process gets a new Toss token and logs the
# first one out, so the script refuses to start while the local engine is up.
set -euo pipefail

TARGET=${1:?usage: deploy_oracle.sh user@host [--no-start]}
MODE=${2:-}
REMOTE=make-me-rich
DOCKER=${DOCKER:-docker}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

# (an `ssh -L 5050:...` dashboard tunnel also answers on :5050, so look for the process itself)
if [[ "$MODE" != "--no-start" ]] && pgrep -f "^[^ ]*python[0-9.]* app\.py" >/dev/null; then
  echo "A local engine (python app.py) is running. Stop it first (one Toss token per key)." >&2
  exit 1
fi

echo "→ code"
tar czf - --exclude=node_modules --exclude=__pycache__ --exclude=dist \
  --exclude=toss_dashboard/backend/data \
  toss_dashboard | ssh "$TARGET" "mkdir -p $REMOTE && tar xzf - -C $REMOTE"

echo "→ .env"
ssh "$TARGET" "umask 077 && cat > $REMOTE/.env" < .env

echo "→ state (only files the server doesn't have)"
(cd toss_dashboard/backend/data && ls *.json 2>/dev/null | tar czf - -T -) |
  ssh "$TARGET" "mkdir -p $REMOTE/toss_dashboard/backend/data/logs && tar xzf - --skip-old-files -C $REMOTE/toss_dashboard/backend/data"

echo "→ build"
ssh "$TARGET" "cd $REMOTE/toss_dashboard && $DOCKER compose build"

if [[ "$MODE" != "--no-start" ]]; then
  echo "→ start"
  ssh "$TARGET" "cd $REMOTE/toss_dashboard && $DOCKER compose up -d && sleep 15 && $DOCKER compose ps && $DOCKER compose logs --tail 20"
fi
echo "Done. Dashboard: ssh -L 5050:localhost:5050 $TARGET  then open http://localhost:5050"
