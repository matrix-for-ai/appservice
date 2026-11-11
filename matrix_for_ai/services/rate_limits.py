import math
import time
from collections import deque
from contextlib import contextmanager

from ..api_errors import ApiError


class RateLimit:
    def __init__(self, *, limit: int | None, period_seconds: float, per: str, what: str,
                 clock=time.monotonic) -> None:
        self._limit = limit
        self._period_seconds = period_seconds
        self._per = per
        self._what = what
        self._clock = clock
        self._hits: dict[str, deque] = {}

    def take(self, key: str) -> float | None:
        if self._limit is None:
            return None
        now = self._clock()
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self._period_seconds:
            hits.popleft()
        if len(hits) >= self._limit:
            retry_after = max(1, math.ceil(hits[0] + self._period_seconds - now))
            raise ApiError(
                429, "rate_limited",
                f"at most {self._limit} {self._what} per {self._per}; retry in {retry_after}s",
                headers={"Retry-After": str(retry_after)})
        hits.append(now)
        return now

    @contextmanager
    def counted_if_successful(self, key: str):
        hit = self.take(key)
        try:
            yield
        except BaseException:
            if hit is not None:
                self._hits[key].remove(hit)
            raise


def per_minute(limit: int | None, what: str) -> RateLimit:
    return RateLimit(limit=limit, period_seconds=60, per="minute", what=what)


def per_hour(limit: int | None, what: str) -> RateLimit:
    return RateLimit(limit=limit, period_seconds=3600, per="hour", what=what)
