#!/bin/sh
# V3 Paper Mode — BTC_15M only, paper trading with LightGBM signal + learning loop.
# Runs until kill_switch or container stop.
set -e

# Write initial heartbeat so ECS health check passes during startup
python3 -c "import time; open('/tmp/heartbeat','w').write(str(time.time()))"

echo "Starting V3 Paper Trader: BTC_15M (paper, learning loop)..."
PYTHONPATH=/app/src .venv/bin/python scripts/run_v3_paper.py --budget "${BTC_V3_BUDGET:-80}" &
V3_PID=$!

echo "Starting Dashboard on port 8888..."
.venv/bin/python scripts/dashboard.py &
DASH_PID=$!

# If any process dies, kill the others and exit so ECS restarts the task
wait -n 2>/dev/null || wait
echo "A process exited — shutting down."
kill $V3_PID $DASH_PID 2>/dev/null || true
