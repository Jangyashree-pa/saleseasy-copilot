"""In-memory rate limiting: per-client sliding window plus a global daily cap.

Good enough for a single-instance demo. Known limitations (see README): state
is per process and resets on restart; behind several instances you would use
Redis or the platform's edge rate limiting instead.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    reason: str | None = None
    retry_after_s: int = 0


class RateLimiter:
    def __init__(
        self,
        per_minute: int,
        daily_cap: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], str] = lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    ):
        self.per_minute = per_minute
        self.daily_cap = daily_cap
        self._clock = clock
        self._today = today
        self._hits: dict[str, deque[float]] = {}
        self._day = today()
        self._day_count = 0
        self._lock = threading.Lock()

    def check(self, key: str) -> LimitDecision:
        with self._lock:
            now = self._clock()
            day = self._today()
            if day != self._day:
                self._day, self._day_count = day, 0
            if self._day_count >= self.daily_cap:
                return LimitDecision(False, "Daily request cap for this demo reached. Try again tomorrow.", 3600)

            window = self._hits.setdefault(key, deque())
            while window and now - window[0] >= 60:
                window.popleft()
            if len(window) >= self.per_minute:
                retry = max(1, int(60 - (now - window[0])) + 1)
                return LimitDecision(False, f"Rate limit: {self.per_minute} requests per minute.", retry)

            window.append(now)
            self._day_count += 1
            if len(self._hits) > 5000:  # bound memory: drop idle clients
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] >= 60]:
                    del self._hits[k]
            return LimitDecision(True)


def client_key(remote_host: str | None, forwarded_for: str | None, trusted_hops: int) -> str:
    """Client identity for rate limiting.

    With ``trusted_hops`` = N > 0 we take the Nth address from the right of
    X-Forwarded-For, i.e. the one appended by our own proxy, which a client
    cannot spoof by sending its own header. With 0 we ignore the header.
    """
    if trusted_hops > 0 and forwarded_for:
        parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
        if len(parts) >= trusted_hops:
            return parts[-trusted_hops]
    return remote_host or "unknown"
