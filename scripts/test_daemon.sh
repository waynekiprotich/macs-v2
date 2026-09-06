#!/bin/bash
# Fast Test Daemon Wrapper
echo "Test Daemon Started."
for i in {1..3}; do
    echo "--- Cycle $i ---"
    source venv/bin/activate && python3 run.py analyze
    echo "Sleeping 15s..."
    sleep 15
done
echo "Test Daemon Complete."
