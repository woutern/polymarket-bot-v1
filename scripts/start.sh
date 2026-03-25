#!/bin/sh
# Start MM bot + dashboard + opportunity scanner together in the same container.
# MM bot runs V2 both-sides strategy in live mode (credentials from env/Secrets Manager).
# Dashboard reads from DynamoDB on AWS (auto-detected when no local SQLite).

set -e

# Write initial heartbeat so ECS health check passes during startup
python3 -c "import time; open('/tmp/heartbeat','w').write(str(time.time()))"

echo "Starting MarketMaker Bot: BTC_5M..."
PYTHONPATH=/app/src .venv/bin/python scripts/run_mm.py --live --yes --budget "${MM_BUDGET:-80}" --model --pair BTC_5M &
BTC_PID=$!

echo "Starting MarketMaker Bot: ETH_5M..."
PYTHONPATH=/app/src .venv/bin/python scripts/run_mm.py --live --yes --budget "${ETH_BUDGET:-50}" --model --pair ETH_5M &
ETH_PID=$!

echo "Starting MarketMaker Bot: SOL_5M..."
PYTHONPATH=/app/src .venv/bin/python scripts/run_mm.py --live --yes --budget "${SOL_BUDGET:-50}" --model --pair SOL_5M &
SOL_PID=$!

echo "Starting MarketMaker Bot: XRP_5M..."
PYTHONPATH=/app/src .venv/bin/python scripts/run_mm.py --live --yes --budget "${XRP_BUDGET:-50}" --model --pair XRP_5M &
XRP_PID=$!

echo "Starting Dashboard on port 8888..."
.venv/bin/python scripts/dashboard.py &
DASH_PID=$!

echo "Starting Opportunity Scanner (every 30min)..."
PYTHONPATH=src .venv/bin/python scripts/opportunity_bot.py &
OPP_PID=$!

echo "Starting Auto-Claim (every 10min)..."
(while true; do node scripts/claim_winnings.js 2>&1 || true; sleep 600; done) &
CLAIM_PID=$!

# If any process dies, kill the others and exit so ECS restarts the task
wait -n 2>/dev/null || wait
echo "A process exited — shutting down."
kill $BTC_PID $ETH_PID $SOL_PID $XRP_PID $DASH_PID $OPP_PID $CLAIM_PID 2>/dev/null || true
