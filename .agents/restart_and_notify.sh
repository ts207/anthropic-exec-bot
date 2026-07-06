#!/bin/bash
set -u
cd /home/tstuv/poly/anthropic-exec-bot
set -a; source .env; set +a

OLD_PID=$(cat logs/location_qatar_live.pid 2>/dev/null)
echo "old child pid: ${OLD_PID:-none}"
if [[ -n "${OLD_PID:-}" ]]; then
  kill "$OLD_PID" 2>/dev/null && echo "sent SIGTERM to $OLD_PID"
fi

echo "waiting for supervisor to relaunch..."
for i in $(seq 1 20); do
  sleep 2
  NEW_PID=$(cat logs/location_qatar_live.pid 2>/dev/null)
  if [[ -n "${NEW_PID:-}" && "$NEW_PID" != "${OLD_PID:-}" ]] && kill -0 "$NEW_PID" 2>/dev/null; then
    echo "relaunched: new child pid $NEW_PID"
    break
  fi
done

echo "--- supervisor log tail ---"
tail -3 logs/location_qatar_supervisor.log
echo "--- run log tail ---"
tail -3 logs/location_qatar_live.out

.venv/bin/python .agents/send_status_telegram.py
