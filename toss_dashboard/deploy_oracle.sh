#!/usr/bin/env bash
# Copy the trader to a server over SSH and (re)start it in Docker, safely.
#
#   ./deploy_oracle.sh oracle               test, copy code + .env, build, restart, verify (rolls back on failure)
#   ./deploy_oracle.sh oracle --no-start    test, copy + build only
#   ./deploy_oracle.sh oracle --force       skip the "bot is mid-trade" guard (never needed normally)
#
# Safety:
#   1. the unit tests must pass locally, and the replay check (the live strategy code traded over past
#      days must match the research backtests: python -m trader.replay --gate), or nothing is copied
#   2. refuses to restart while the bot holds a scalp/pre-market/intraday position or has an order
#      working (a restart is safe then too, but there is no reason to take the risk)
#   3. the running image is kept as :prev; if the new engine isn't running 90 s after the restart,
#      the previous image is put back automatically
#   4. backend/VERSION records the git commit (+dirty) so every journal line can be tied to code
# State (backend/data/*.json) is copied only when the server has none. Research data is not copied.
# Only one place may run the bot (one Toss token, one Alpaca stream).
set -euo pipefail

TARGET=${1:?usage: deploy_oracle.sh user@host [--no-start|--force]}
MODE=${2:-}
REMOTE=make-me-rich
DOCKER=${DOCKER:-docker}
IMAGE=toss-trader-trader
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

if [[ "$MODE" != "--no-start" ]] && pgrep -f "^[^ ]*python[0-9.]* app\.py" >/dev/null; then
  echo "A local engine (python app.py) is running. Stop it first (one Toss token per key)." >&2
  exit 1
fi

echo "→ tests"
(cd toss_dashboard/backend && ../../.venv/bin/python -m pytest -q tests >/tmp/deploy_tests.log 2>&1) || {
  tail -30 /tmp/deploy_tests.log; echo "Tests failed: nothing deployed." >&2; exit 1; }
tail -1 /tmp/deploy_tests.log

echo "→ replay check (live strategy code vs the research backtests on past days)"
if [[ -f toss_dashboard/backend/data/research/scalp/alpaca_1m.parquet ]]; then
  (cd toss_dashboard/backend && ../../.venv/bin/python -m trader.replay --gate >/tmp/deploy_replay.log 2>&1) || {
    grep -v "refused by the guard" /tmp/deploy_replay.log | tail -25; echo "Replay parity failed: nothing deployed." >&2; exit 1; }
  grep -E "breakout:|pre-market:|guard refusals|parity gate" /tmp/deploy_replay.log | sed 's/^/  /'
else
  echo "  (no local research data: replay check skipped)"
fi

if [[ "$MODE" != "--no-start" && "$MODE" != "--force" ]]; then
  echo "→ checking the bot is not mid-trade"
  busy=$(ssh "$TARGET" "curl -s -m 5 http://127.0.0.1:5050/api/status" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print(""); sys.exit()  # not running: nothing to protect
why = []
sc = d.get("scalp") or {}
if (sc.get("state") or {}).get("qty", 0) > 0:
    why.append("scalp holds " + str(sc["state"].get("side")))
for row in (sc.get("pre") or {}).get("names", []):
    if row.get("qty", 0) > 0 or row.get("status") in ("entry_open", "in_trade", "exiting", "exit_pending"):
        why.append(f"pre-market {row.get('symbol')} {row.get('status')}")
sw = d.get("swing") or {}
today, clock = d.get("now_et", "")[:10], d.get("now_et", "")[11:16]
if sw.get("busy"):
    why.append("swing is rebalancing")
elif (sw.get("model") or {}).get("for_day") == today and sw.get("last_run") != today and "08:00" <= clock < "14:50":
    why.append("swing has not traded today yet (it trades at 09:36 ET)")
if d.get("positions"):
    why.append("intraday positions")
live = [o for o in d.get("orders", []) if o.get("status") not in ("FILLED", "CANCELED", "REJECTED", "REPLACED")]
if live:
    why.append(f"{len(live)} order(s) working")
print("; ".join(why))
')
  if [[ -n "$busy" ]]; then
    echo "The bot is mid-trade ($busy). Deploy later, or pass --force." >&2
    exit 1
  fi
fi

echo "→ version"
ver="$(git rev-parse --short HEAD)$(git diff --quiet HEAD -- toss_dashboard || echo -dirty) $(date -u +%Y-%m-%dT%H:%MZ)"
echo "$ver" > toss_dashboard/backend/VERSION
echo "  $ver"

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
ssh "$TARGET" "$DOCKER image tag $IMAGE:latest $IMAGE:prev 2>/dev/null || true; cd $REMOTE/toss_dashboard && $DOCKER compose build -q"

if [[ "$MODE" == "--no-start" ]]; then
  echo "Built, not started."
  exit 0
fi

echo "→ start + verify"
ssh "$TARGET" "cd $REMOTE/toss_dashboard && $DOCKER compose up -d >/dev/null 2>&1
for i in \$(seq 1 45); do
  if curl -sf -m 3 http://127.0.0.1:5050/healthz | grep -q '\"running\":true'; then echo '  engine running:' \$(curl -s -m 3 http://127.0.0.1:5050/healthz); exit 0; fi
  sleep 2
done
echo 'New version did not start: rolling back to the previous image' >&2
$DOCKER compose logs --tail 40 >&2
$DOCKER image tag $IMAGE:prev $IMAGE:latest && $DOCKER compose up -d --no-build >/dev/null 2>&1
sleep 15; curl -s -m 3 http://127.0.0.1:5050/healthz >&2; exit 1"
ssh "$TARGET" "cd $REMOTE/toss_dashboard && $DOCKER compose logs --since 2m | grep -vE 'GET /' | tail -12"
echo "Done. Dashboard: ssh -L 5050:localhost:5050 $TARGET  then open http://localhost:5050"
