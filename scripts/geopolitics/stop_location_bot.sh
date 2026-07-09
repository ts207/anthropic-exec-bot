#!/bin/bash
cd /home/tstuv/poly/anthropic-exec-bot
mkdir -p logs
# Order matters: touch the stop file before killing the child, so the
# supervisor's loop sees it on the next iteration and exits instead of
# relaunching.
touch logs/location_qatar.stop
if [[ -f logs/location_qatar_live.pid ]]; then
  kill "$(cat logs/location_qatar_live.pid)" 2>/dev/null
fi
if [[ -f logs/location_qatar_supervisor.pid ]]; then
  kill "$(cat logs/location_qatar_supervisor.pid)" 2>/dev/null
fi
echo "stop requested"
