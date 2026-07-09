#!/bin/bash
cd /home/tstuv/poly/anthropic-exec-bot
set -a
source .env
set +a
nohup .venv/bin/python -m polybot.geopolitics run-iran --config configs/geopolitics/iran-july17-yes-protection.yaml > logs/iran_july17_live.out 2>&1 &
echo $! > logs/iran_july17_live.pid
sleep 3
echo "STARTED_PID=$(cat logs/iran_july17_live.pid)"
ps -p "$(cat logs/iran_july17_live.pid)" -o pid,cmd --no-headers
