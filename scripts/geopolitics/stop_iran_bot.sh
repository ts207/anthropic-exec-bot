#!/bin/bash
cd /home/tstuv/poly/anthropic-exec-bot
mkdir -p logs
# Order matters: touch the stop file before killing the child, so the
# supervisor's loop sees it on the next iteration and exits instead of
# relaunching.
touch logs/iran_july17.stop
if [[ -f logs/iran_july17_live.pid ]]; then
  kill "$(cat logs/iran_july17_live.pid)" 2>/dev/null
fi
if [[ -f logs/iran_july17_supervisor.pid ]]; then
  kill "$(cat logs/iran_july17_supervisor.pid)" 2>/dev/null
fi
echo "stop requested"
