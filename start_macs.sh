#!/bin/bash
# MACS-V2 Daemon Wrapper
# Auto-restarts on crash, loops every 15m. Perfect for pm2 or just nohup.

echo "MACS-V2 Background Daemon Started."

# Activate local venv if it exists (for local dev)
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
elif [ -f "/app/.venv/bin/activate" ]; then
    # Railway explicit activation fallback
    source /app/.venv/bin/activate
fi

while true; do
    echo "Running analysis cycle at $(date)..."
    # Skip the cycle rather than trade when the database can't record it.
    if python3 -m scripts.preflight; then
        python3 run.py analyze
    else
        echo "Preflight failed; skipping this cycle."
    fi
    echo "Cycle complete. Sleeping 900s..."
    sleep 900
done
