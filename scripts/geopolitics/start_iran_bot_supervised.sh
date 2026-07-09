#!/bin/bash
set -u
cd /home/tstuv/poly/anthropic-exec-bot
set -a
source .env
set +a
# So the classifier's "claude_cli" provider can find the Claude Code CLI
# (installed to ~/.local/bin), which isn't on PATH for non-login shells.
export PATH="$HOME/.local/bin:$PATH"

mkdir -p logs
STOPFILE=logs/iran_july17.stop
rm -f "$STOPFILE"

RUN_LOG=logs/iran_july17_live.out
SUP_LOG=logs/iran_july17_supervisor.log
CHILD_PIDFILE=logs/iran_july17_live.pid
SUP_PIDFILE=logs/iran_july17_supervisor.pid

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
  echo "$(date -u +%FT%TZ) launching run-iran" >> "$SUP_LOG"
  .venv/bin/python -m polybot.geopolitics run-iran --config configs/geopolitics/iran-july17-yes-protection.yaml >> "$RUN_LOG" 2>&1 &
  CHILD=$!
  echo "$CHILD" > "$CHILD_PIDFILE"
  wait "$CHILD"
  CODE=$?
  echo "$(date -u +%FT%TZ) run-iran exited (code $CODE); restarting in 10s" >> "$SUP_LOG"
  sleep 10
done
