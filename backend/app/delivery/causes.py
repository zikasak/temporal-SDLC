"""Why a stage failed, in a few words the user may be shown: the cause Temporal carried up
from the activity or child workflow that failed, instead of a bare "could not".

Pure and deterministic, for the delivery workflow. A cause is cut to its first line and
the last line of the output it quotes that reads as an error, and anything that looks like
a credential is masked: an agent's log is redacted when it is read, but a provider's own
message may still carry one."""

import re

CAUSE_CHARS = 400
# Lines of quoted output that say what went wrong.
_ERRORISH = re.compile(
    r'error|exception|failed|failure|denied|refused|not found|unavailable|invalid|'
    r'timed? ?out|cannot|could not|unable|missing|traceback|killed|exceeded', re.IGNORECASE)
_SECRETS = re.compile(
    r'(sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|xox[a-z]-[A-Za-z0-9-]{8,}|'
    r'(?:Bearer|Basic|token)\s+[A-Za-z0-9._~+/=-]{12,}|'
    r'(?:api[_-]?key|password|secret|token)["\']?\s*[:=]\s*["\']?[^\s"\']{6,})',
    re.IGNORECASE)
# Wrappers that say only that something below failed.
_WRAPPERS = re.compile(r'^(Activity task failed|Child Workflow execution failed|'
                       r'Workflow execution failed|activity error|child workflow error)$',
                       re.IGNORECASE)


def _messages(exc: BaseException | None) -> list[str]:
    """The messages of `exc` and the failures it was caused by, outermost first."""
    seen, found = set(), []
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        message = getattr(exc, 'message', None)
        text = str(message if isinstance(message, str) else exc).strip()
        if text and not _WRAPPERS.match(text):
            found.append(text)
        exc = getattr(exc, 'cause', None) or exc.__cause__
    return found


def mask(text: str) -> str:
    return _SECRETS.sub('[redacted]', text)


def summarize(text: str, limit: int = CAUSE_CHARS) -> str:
    """The first line of a failure's message, and the last line of the output it quotes
    that reads as an error, masked and bounded."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ''
    first = lines[0]
    detail = next((line for line in reversed(lines[1:]) if _ERRORISH.search(line)), '')
    summary = first if not detail or detail in first else f'{first} … {detail}'
    summary = mask(' '.join(summary.split()))
    return summary if len(summary) <= limit else summary[:limit - 1] + '…'


def failure_cause(exc: BaseException | None, limit: int = CAUSE_CHARS) -> str:
    """The innermost cause of a failure, summarized; empty when it says nothing."""
    messages = _messages(exc)
    return summarize(messages[-1], limit) if messages else ''


def with_cause(message: str, exc: BaseException | None) -> str:
    """`message`, followed by the failure's cause when it has one."""
    cause = failure_cause(exc)
    if not cause:
        return message
    return f'{message} Cause: {cause}{"" if cause.endswith((".", "…", ")")) else "."}'
