#!/bin/sh
# Start bot + dashboard + opportunity scanner together in the same container.
# Dashboard reads from DynamoDB on AWS (auto-detected when no local SQLite).
# Bot writes to SQLite + DynamoDB mirror.

set -e

echo "Starting Polymarket Bot..."
.venv/bin/python scripts/run.py &
BOT_PID=$!

echo "Starting Dashboard on port 8888..."
.venv/bin/python scripts/dashboard.py &
DASH_PID=$!

echo "Starting Opportunity Scanner (every 30min)..."
PYTHONPATH=src .venv/bin/python scripts/opportunity_bot.py &
OPP_PID=$!

echo "Starting Auto-Claim (every 2h)..."
(while true; do node scripts/claim_winnings.js 2>&1 || true; sleep 7200; done) &
CLAIM_PID=$!

# If any process dies, kill the others and exit so ECS restarts the task
wait -n 2>/dev/null || wait
echo "A process exited — shutting down."
kill $BOT_PID $DASH_PID $OPP_PID $CLAIM_PID 2>/dev/null || true
