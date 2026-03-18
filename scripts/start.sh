#!/bin/sh
# Start bot + dashboard together in the same container.
# Dashboard reads from DynamoDB on AWS (auto-detected when no local SQLite).
# Bot writes to SQLite + DynamoDB mirror.

set -e

echo "Starting Polymarket Bot..."
.venv/bin/python scripts/run.py &
BOT_PID=$!

echo "Starting Dashboard on port 8888..."
.venv/bin/python scripts/dashboard.py &
DASH_PID=$!

# If either process dies, kill the other and exit so ECS restarts the task
wait -n 2>/dev/null || wait
echo "A process exited — shutting down."
kill $BOT_PID $DASH_PID 2>/dev/null || true
