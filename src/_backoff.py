"""
Shared exponential-backoff helper.  Imported by resolve_entities.py and
fetch_trends.py so rate-limit logic lives in one place.
"""

import random
import time
import logging
from typing import Callable, TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")


def with_backoff(
    fn: Callable[[], T],
    max_retries: int = 7,
    base_delay: float = 5.0,
    cap: float = 120.0,
) -> T:
    """
    Call fn(); on 429 / TooManyRequests, retry with exponential backoff.
    Raises the last exception if all retries are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as exc:
            msg = str(exc).lower()
            if "429" in msg or "too many requests" in msg or "response code" in msg:
                last_exc = exc
                delay = min(base_delay * (2 ** attempt) + random.uniform(0, 1), cap)
                log.warning(
                    f"Rate limited (attempt {attempt + 1}/{max_retries}), "
                    f"sleeping {delay:.1f}s …"
                )
                time.sleep(delay)
            else:
                raise

    raise RuntimeError(f"Max retries ({max_retries}) exceeded") from last_exc
