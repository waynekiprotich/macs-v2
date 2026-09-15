#!/bin/bash
# MACS-V2 Daemon Wrapper
# Runs one cycle shortly after each 15-minute candle closes (core/schedule.py).

echo "MACS-V2 Background Daemon Started."

# Activate local venv if it exists (for local dev)
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
elif [ -f "/app/.venv/bin/activate" ]; then
    # Railway explicit activation fallback
    source /app/.venv/bin/activate
fi

last_boundary=""
while true; do
    # Sleep until just after the next candle closes. The wait is computed from
    # the clock every cycle, so start times don't drift with cycle duration.
    if ! boundary=$(python3 -m scripts.wait_for_cycle ${last_boundary:+--after "$last_boundary"}); then
        echo "Scheduler failed; retrying in 60s without running a cycle."
        sleep 60
        continue
    fi
    last_boundary="$boundary"
    echo "Running analysis cycle at $(date)..."
    # Skip the cycle rather than trade when the database can't record it.
    if python3 -m scripts.preflight; then
        python3 run.py analyze
    else
        echo "Preflight failed; skipping this cycle."
    fi
    echo "Cycle complete."
done
