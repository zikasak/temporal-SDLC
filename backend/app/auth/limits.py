"""Bounds what unauthenticated sign-in attempts can cost the API."""

import asyncio
import math
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from app.auth.passwords import verify_password

# A derivation holds about 32 MiB and a core while it runs. Its own small pool keeps
# sign-in bursts out of the default executor that other routes use.
_CONCURRENT = 2
MAX_WAITING = 16
_executor = ThreadPoolExecutor(max_workers=_CONCURRENT, thread_name_prefix="password")
_pending = 0


class SignInBusy(Exception):
    pass


async def check_password(password: str, stored: str) -> bool:
    """verify_password off the event loop; SignInBusy instead of an unbounded queue."""
    global _pending
    if _pending >= _CONCURRENT + MAX_WAITING:
        raise SignInBusy
    _pending += 1
    try:
        return await asyncio.get_running_loop().run_in_executor(
            _executor, verify_password, password, stored)
    finally:
        _pending -= 1


class AttemptLimiter:
    """Sliding-window attempt count per client, kept in this process."""

    def __init__(self, limit: int, window: float = 60.0) -> None:
        self._limit = limit
        self._window = window
        self._attempts: dict[str, deque[float]] = {}

    def retry_after(self, client: str) -> int | None:
        """Record an attempt, or return how many seconds the client must wait first."""
        now = time.monotonic()
        if len(self._attempts) > 10_000:
            self._attempts = {key: attempts for key, attempts in self._attempts.items()
                              if attempts[-1] > now - self._window}
        attempts = self._attempts.setdefault(client, deque())
        while attempts and attempts[0] <= now - self._window:
            attempts.popleft()
        if len(attempts) >= self._limit:
            return math.ceil(attempts[0] + self._window - now)
        attempts.append(now)
        return None
