"""Bounded exponential backoff with read/write outcome classification.

Allow up to two retries after the initial attempt. Read failures can be retried;
a write interrupted after submission may already have taken effect and must be
reconciled. Complete client-error responses can be handled as rejected requests.
Disable SDK retries and use real sleep in production; inject a sleeper in tests."""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, Optional, Tuple, Type, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Retry count excluding the initial attempt.
DEFAULT_RETRIES = 2
# Base delay in seconds.
DEFAULT_BACKOFF_BASE = 0.5


class AmbiguousOutcomeError(Exception):
    """The server may have executed the request without returning a complete response.

    Automatic retry could duplicate a remote side effect."""


class NonRetryableError(Exception):
    """A non-retryable configuration, permission or business error."""


class RetryExhaustedError(Exception):
    """All attempts failed; retain the last underlying exception."""

    def __init__(self, attempts: int, last_error: BaseException) -> None:
        super().__init__(f"Still failed after {attempts} attempts: {last_error!r}")
        self.attempts = attempts
        self.last_error = last_error


# Transport exception names associated with uncertain remote outcomes.
# Google and OpenAI clients expose different timeout and connection exceptions.
# Match names and messages without hard dependencies on optional SDK types.
AMBIGUOUS_ERROR_NAMES = {
    "Timeout",
    "TimeoutError",
    "ReadTimeout",
    "ReadTimeoutError",
    "ConnectTimeout",
    "ConnectTimeoutError",
    "ConnectionError",
    "ConnectionResetError",
    "ConnectionAbortedError",
    "BrokenPipeError",
    "RemoteDisconnected",
    "ServerDisconnectedError",
    "ProtocolError",
    "RetryError",
    "APIConnectionError",
    "APITimeoutError",
    "IncompleteRead",
}
# These messages commonly indicate submission without a complete response.
AMBIGUOUS_MESSAGE_HINTS = (
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "broken pipe",
    "remote end closed",
    "incompleteread",
    "response lost",
    "eof occurred",
)


def is_ambiguous_error(exc: BaseException) -> bool:
    """Identify transport failures that may leave the result uncertain."""
    if isinstance(exc, AmbiguousOutcomeError):
        return True
    current: BaseException | None = exc
    seen = 0
    while current is not None and seen < 5:
        if type(current).__name__ in AMBIGUOUS_ERROR_NAMES:
            return True
        text = str(current).lower()
        if any(hint in text for hint in AMBIGUOUS_MESSAGE_HINTS):
            return True
        current = current.__cause__ or current.__context__
        seen += 1
    return False


def http_status(exc: BaseException) -> int | None:
    """Extract an HTTP status from common SDK exception shapes."""
    for attr in ("status_code", "status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    if isinstance(status, int):
        return status
    return None


def classify_write_error(exc: BaseException, operation: str) -> BaseException:
    """Classify write errors conservatively.

    Preserve explicit non-retryable and ambiguous errors. Transport failures,
    response decode errors and server errors may follow a committed write.
    Complete client-error responses are treated as rejected requests."""
    if isinstance(exc, (AmbiguousOutcomeError, NonRetryableError)):
        return exc
    status = http_status(exc)
    if status is not None and 400 <= status < 500:
        # An explicit client-error response indicates a rejected request.
        return exc
    # A decode error after a successful response may follow a committed remote write.
    return AmbiguousOutcomeError(f"{operation} outcome is uncertain ({type(exc).__name__}: {exc}）")


def call_with_retry(
    func: Callable[[], T],
    *,
    operation: str,
    retryable: Tuple[Type[BaseException], ...] = (Exception,),
    non_retryable: Tuple[Type[BaseException], ...] = (
        AmbiguousOutcomeError,
        NonRetryableError,
    ),
    write: bool = False,
    retries: int = DEFAULT_RETRIES,
    backoff_base: float = DEFAULT_BACKOFF_BASE,
    sleep: Optional[Callable[[float], None]] = None,
) -> T:
    """Call func with exponential backoff for eligible errors.

    Raise non-retryable errors immediately. For write=True, convert uncertain
    transport failures to AmbiguousOutcomeError instead of resubmitting.
    Raise RetryExhaustedError when the bounded retry budget is exhausted."""
    # Use real time.sleep unless tests explicitly inject a replacement.
    wait = time.sleep if sleep is None else sleep
    attempt = 0
    last_error: BaseException | None = None
    while attempt <= retries:
        try:
            return func()
        except non_retryable:
            # Uncertain outcomes and non-retryable errors propagate immediately.
            raise
        except retryable as exc:  # noqa: PERF203 - handle each attempt separately
            if write:
                classified = classify_write_error(exc, operation)
                if isinstance(classified, AmbiguousOutcomeError):
                    logger.error("%s has an uncertain transport outcome; stopping retries: %s", operation, exc)
                    raise classified from exc
            last_error = exc
            if attempt >= retries:
                break
            # Exponential backoff with a little jitter avoids synchronized retries.
            delay = backoff_base * (2**attempt) + random.uniform(0, backoff_base / 4)
            logger.warning(
                "%s attempt %d failed: %s; retrying in %.2fs", operation, attempt + 1, exc, delay
            )
            wait(delay)
            attempt += 1
    assert last_error is not None
    logger.error("%s exhausted retries: %s", operation, last_error)
    raise RetryExhaustedError(attempt + 1, last_error) from last_error
