"""Block until the next candle-aligned cycle is due, then print its boundary
(epoch seconds) on stdout for start_macs.sh.

    python -m scripts.wait_for_cycle [--after BOUNDARY]
"""
import argparse
import sys
from datetime import datetime, timezone

from core.schedule import wait_for_cycle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--after", type=int, default=None, help="boundary of the cycle that ran last")
    args = parser.parse_args()
    boundary = wait_for_cycle(args.after)
    closed = datetime.fromtimestamp(boundary, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"Candle closed at {closed}; starting cycle.", file=sys.stderr, flush=True)
    print(boundary, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
