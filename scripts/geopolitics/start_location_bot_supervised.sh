#!/bin/bash
set -u
cd /home/tstuv/poly/anthropic-exec-bot
set -a
source .env
set +a
# So the classifier's CLI provider can find Codex/Claude binaries in
# non-login shells.
export PATH="$HOME/.local/bin:$PATH"

mkdir -p logs
STOPFILE=logs/location_qatar.stop
rm -f "$STOPFILE"

RUN_LOG=logs/location_qatar_live.out
SUP_LOG=logs/location_qatar_supervisor.log
CHILD_PIDFILE=logs/location_qatar_live.pid
SUP_PIDFILE=logs/location_qatar_supervisor.pid

echo $$ > "$SUP_PIDFILE"

cleanup() {
  if [[ -f "$CHILD_PIDFILE" ]]; then
    kill "$(cat "$CHILD_PIDFILE")" 2>/dev/null
  fi
  exit 0
}
trap cleanup TERM INT

echo "$(date -u +%FT%TZ) supervisor started (pid $$)" >> "$SUP_LOG"

while true; do
  if [[ -f "$STOPFILE" ]]; then
    echo "$(date -u +%FT%TZ) stop file present; exiting supervisor" >> "$SUP_LOG"
    rm -f "$STOPFILE"
    break
  fi
  echo "$(date -u +%FT%TZ) launching run-location-protection --live" >> "$SUP_LOG"
  .venv/bin/python -m polybot.geopolitics run-location-protection --config configs/geopolitics/qatar-sept30-yes-protection.yaml --live >> "$RUN_LOG" 2>&1 &
  CHILD=$!
  echo "$CHILD" > "$CHILD_PIDFILE"
  wait "$CHILD"
  CODE=$?
  echo "$(date -u +%FT%TZ) run-location-protection exited (code $CODE); restarting in 10s" >> "$SUP_LOG"
  sleep 10
done
