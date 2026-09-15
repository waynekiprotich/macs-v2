"""When the worker starts its next cycle: shortly after each 15-minute candle
closes. The timing is recomputed from the clock every time, so cycles never
drift the way a fixed sleep does."""
import math
import time
from typing import Callable, Optional, Tuple

GRANULARITY_SECONDS = 900
# Seconds after the boundary before starting, so Deriv has closed the candle.
START_DELAY_SECONDS = 10
# A cycle that would start later than this after its boundary is skipped: its
# candle's close is no longer the entry the signal describes.
MAX_LATENESS_SECONDS = 120


def next_cycle(now: float, last_boundary: Optional[int] = None, granularity: int = GRANULARITY_SECONDS,
               start_delay: float = START_DELAY_SECONDS,
               max_lateness: float = MAX_LATENESS_SECONDS) -> Tuple[float, int]:
    """Return (seconds to wait, boundary) for the next cycle.

    A cycle belongs to a boundary, the close of the candle it evaluates, and
    may start between start_delay and max_lateness seconds after it. A boundary
    whose window has passed is skipped rather than run late, and a boundary at
    or before last_boundary is never run again."""
    boundary = math.floor(now / granularity) * granularity
    if (last_boundary is None or boundary > last_boundary) and now - boundary <= max_lateness:
        return max(0.0, boundary + start_delay - now), boundary
    following = boundary + granularity
    if last_boundary is not None and following <= last_boundary:
        # The clock stepped backwards: never repeat a boundary already run.
        following = int(last_boundary) + granularity
    return following + start_delay - now, following


def wait_for_cycle(last_boundary: Optional[int] = None, clock: Callable[[], float] = time.time,
                   sleep: Callable[[float], None] = time.sleep, max_chunk: float = 30.0) -> int:
    """Sleep until the next cycle is due and return its boundary. The clock is
    re-read after every chunk, so oversleeping, a paused container or a clock
    step lands on the right boundary instead of accumulating drift."""
    while True:
        wait, boundary = next_cycle(clock(), last_boundary)
        if wait <= 0:
            return boundary
        sleep(min(wait, max_chunk))
