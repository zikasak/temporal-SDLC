"""Whether a provider's error says that its limits are spent for now: a rate limit, an
exhausted quota or usage limit, or an overloaded service. Such an error says nothing about
the work, so the work waits for the provider and goes on, instead of failing.

Pure: the coding agents' container runtime and the host both use it."""

import re
import time
from email.utils import parsedate_to_datetime

# The exit status of an agent that stopped because its provider's limits are spent, once
# it saved where it stopped (as a paused run does) and LIMIT_FILENAME beside its project.
PROVIDER_LIMIT_EXIT = 76
# What such an agent tells the host: {"retry_after": seconds or null, "message": text}.
LIMIT_FILENAME = 'provider-limit.json'
# The ApplicationError type of a host activity whose provider's limits are spent.
PROVIDER_LIMIT_ERROR = 'ProviderLimitReached'

_STATUSES = frozenset({429, 529})
_PATTERN = re.compile(
    r'rate[ _-]?limit|too many requests|\b(?:429|529)\b|quota|usage[ _-]?limit|'
    r'limit (?:has been |was )?(?:reached|exceeded)|overloaded|resource[ _-]?exhausted|'
    r'credit balance is too low',
    re.IGNORECASE)
# "try again in 20s", "retry after 1m30s", "Please try again in 2.5 seconds".
_AFTER = re.compile(
    r'(?:try again|retry)(?: after| in)?\s+(?:(\d+(?:\.\d+)?)\s*h(?:ours?)?\s*)?'
    r'(?:(\d+(?:\.\d+)?)\s*m(?:in(?:utes?)?)?\s*)?(?:(\d+(?:\.\d+)?)\s*s(?:ec(?:onds?)?)?)?',
    re.IGNORECASE)
# Claude's "usage limit reached|<epoch seconds>" and "resets at <epoch>".
_RESET_EPOCH = re.compile(r'(?:limit reached|resets?(?: at)?)\|?\s*(\d{10})\b', re.IGNORECASE)
# How much of an error's message a limit report keeps.
MESSAGE_CHARS = 300


def _chain(exc: BaseException | None):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _status(exc: BaseException) -> int | None:
    for name in ('status_code', 'status', 'code', 'http_status'):
        value = getattr(exc, name, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, 'response', None)
    value = getattr(response, 'status_code', None)
    return value if isinstance(value, int) else None


def is_limit_text(text: str) -> bool:
    """Whether a provider's message, or an agent's log, says the provider's limits are
    spent."""
    return bool(text) and bool(_PATTERN.search(text))


def is_limit_error(exc: BaseException) -> bool:
    """Whether `exc`, or an error it was raised from, is a provider's limit: by its HTTP
    status, its class (such as openai.RateLimitError) or its message."""
    for item in _chain(exc):
        if _status(item) in _STATUSES:
            return True
        name = type(item).__name__.lower()
        if 'ratelimit' in name or 'quota' in name or 'overloaded' in name:
            return True
        if is_limit_text(str(item)):
            return True
    return False


def retry_after_text(text: str, now: float | None = None) -> float | None:
    """The seconds a provider's message asks to wait, if it says."""
    if not text:
        return None
    epoch = _RESET_EPOCH.search(text)
    if epoch:
        return max(0.0, int(epoch.group(1)) - (time.time() if now is None else now))
    match = _AFTER.search(text)
    if match and any(match.groups()):
        hours, minutes, seconds = (float(value or 0) for value in match.groups())
        return hours * 3600 + minutes * 60 + seconds
    return None


def retry_after(exc: BaseException) -> float | None:
    """The seconds the provider asked to wait before the next request, if it said: its
    Retry-After header, or its message."""
    for item in _chain(exc):
        headers = getattr(getattr(item, 'response', None), 'headers', None)
        value = headers.get('retry-after') if headers is not None else None
        if value:
            try:
                return max(0.0, float(value))
            except ValueError:
                try:
                    return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
                except (TypeError, ValueError):
                    pass
        waited = retry_after_text(str(item))
        if waited is not None:
            return waited
    return None


def limit_report(exc: BaseException) -> dict:
    """What an agent stopped by a limit tells the host, without credentials: the wait the
    provider asked for and the start of its message."""
    message = ' '.join(str(exc).split())
    message = re.sub(r'(sk-|key-|Bearer\s+)[A-Za-z0-9._-]{8,}', r'\1[redacted]', message)
    return {'retry_after': retry_after(exc), 'message': message[:MESSAGE_CHARS]}
