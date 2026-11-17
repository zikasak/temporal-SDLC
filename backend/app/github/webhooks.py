"""GitHub webhook deliveries: their signature, and the mentions of the app they carry."""

import asyncio
import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from app.github import client, service, storage
from app.github.models import PullRequestMention

logger = logging.getLogger(__name__)

COMMENT_EVENTS = {'issue_comment': 'conversation', 'pull_request_review_comment': 'review'}
# A mention's text as the delivery keeps it.
BODY_CHARS = 65536
HEX_DIGEST = re.compile(r'[0-9a-f]{64}', re.ASCII)


class InvalidEvent(ValueError):
    """A signed delivery of a comment event without the fields it needs."""


class AlreadyHandled(Exception):
    """The delivery or its comment was handled before, such as on a redelivery."""


def signature_matches(secret: str, payload: bytes, signature: str | None) -> bool:
    """Whether X-Hub-Signature-256 is the HMAC-SHA256 of the raw body with the secret."""
    if not signature or not signature.startswith('sha256='):
        return False
    supplied = signature[len('sha256='):].strip().lower()
    # Only 64 ASCII hex digits can match; anything else is rejected, never compared.
    if not HEX_DIGEST.fullmatch(supplied):
        return False
    expected = hmac.new(secret.encode('utf-8'), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode('ascii'), supplied.encode('ascii'))


def mentions(body: str, slug: str) -> bool:
    """Whether the text mentions @slug or @slug[bot] as a whole word, in any case."""
    pattern = rf'(?<![A-Za-z0-9_@./-])@{re.escape(slug)}(?:\[bot\])?(?![A-Za-z0-9_-])'
    return re.search(pattern, body, re.IGNORECASE) is not None


@dataclass
class Comment:
    comment_type: Literal['conversation', 'review']
    comment_id: int
    body: str
    url: str
    author: str
    full_name: str
    number: int
    state: str
    pull_author: str | None
    # The top-level comment of an inline comment's thread: itself, or the comment a reply
    # in the thread answers.
    thread_id: int


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidEvent(f'The webhook payload has no {name} object.')
    return value


def _login(value: Any) -> str | None:
    login = value.get('login') if isinstance(value, dict) else None
    return login if isinstance(login, str) and login else None


def parse_comment(event: str, payload: dict[str, Any]) -> Comment | None:
    """The comment of an issue_comment or pull_request_review_comment delivery; None when
    the comment is not on a pull request."""
    comment = _object(payload.get('comment'), 'comment')
    repository = _object(payload.get('repository'), 'repository')
    if event == 'issue_comment':
        pull = _object(payload.get('issue'), 'issue')
        if not isinstance(pull.get('pull_request'), dict):
            # A comment on an issue, not on a pull request.
            return None
    else:
        pull = _object(payload.get('pull_request'), 'pull_request')
    comment_id, body, author = comment.get('id'), comment.get('body'), _login(comment.get('user'))
    full_name, number, state = repository.get('full_name'), pull.get('number'), pull.get('state')
    if (type(comment_id) is not int or comment_id <= 0 or not isinstance(body, str)
            or author is None or not isinstance(full_name, str)
            or not client.NAME_PATTERN.fullmatch(full_name)
            or type(number) is not int or number <= 0 or not isinstance(state, str)):
        raise InvalidEvent('The webhook payload lacks the comment, its author, the pull '
                           'request or the repository.')
    url = comment.get('html_url')
    prefix = f'https://github.com/{full_name}/pull/{number}'
    if not isinstance(url, str) or not url.startswith(prefix):
        url = (f'{prefix}#issuecomment-{comment_id}' if event == 'issue_comment'
               else f'{prefix}#discussion_r{comment_id}')
    in_reply_to = comment.get('in_reply_to_id')
    thread_id = (in_reply_to if event == 'pull_request_review_comment'
                 and type(in_reply_to) is int and in_reply_to > 0 else comment_id)
    return Comment(COMMENT_EVENTS[event], comment_id, body[:BODY_CHARS], url, author,
                   full_name, number, state, _login(pull.get('user')), thread_id)


@dataclass
class Mention:
    """An authorised mention to hand to the delivery of `session_id`."""

    session_id: str
    mention: PullRequestMention


async def authorised_mention(connection: dict[str, Any], event: str | None,
                             payload: dict[str, Any]) -> Mention | None:
    """The mention a correctly signed delivery carries, when the app acts on it; None
    when the delivery is ignored."""
    found = await mention_candidate(connection, event, payload)
    return None if found is None else await authorise(connection, *found)


async def mention_candidate(connection: dict[str, Any], event: str | None,
                            payload: dict[str, Any]) -> tuple[Comment, str] | None:
    """The comment mentioning the app and the session of its delivery, from what the
    delivery and this backend tell, before GitHub is asked; None when it is ignored."""
    if event not in COMMENT_EVENTS:
        return None
    if payload.get('action') != 'created':
        # Edited or deleted comments are not acted on.
        return None
    comment = parse_comment(event, payload)
    if comment is None:
        return None
    slug = connection['app_slug']
    bot = f'{slug}[bot]'
    if not mentions(comment.body, slug):
        return None
    where = f'{comment.full_name}#{comment.number} comment {comment.comment_id}'
    if comment.author.lower() == bot.lower():
        logger.info('Ignored the app\'s own comment on %s', where)
        return None
    # A mention on a merged or closed pull request is acted on too: its delivery's flow has
    # closed with it, so a fix it asks for runs as a flow of its own, on a new branch.
    if comment.pull_author is not None and comment.pull_author.lower() != bot.lower():
        logger.info('Ignored a mention on %s, a pull request the app did not open', where)
        return None
    session_id = await storage.pull_request_session(comment.full_name, comment.number)
    if session_id is None:
        logger.info('Ignored a mention on %s, which belongs to no delivery', where)
        return None
    return comment, session_id


async def authorise(connection: dict[str, Any], comment: Comment,
                    session_id: str) -> Mention | None:
    """The mention to hand over when GitHub confirms the pull request exists and is the
    app's, and its author may write to the repository; None when it is ignored. A pull
    request that was merged or closed still takes mentions: its delivery hands their fix
    rounds to a flow of its own, which pushes to a new branch."""
    bot = f"{connection['app_slug']}[bot]"
    where = f'{comment.full_name}#{comment.number} comment {comment.comment_id}'
    token = await service.installation_token(connection)
    current = await client.get_pull_request_state(token, comment.full_name, comment.number)
    if current is None:
        logger.info('Ignored a mention on %s, which no longer exists', where)
        return None
    if current['author'] is not None and current['author'].lower() != bot.lower():
        logger.info('Ignored a mention on %s, a pull request the app did not open', where)
        return None
    if not await client.has_write_access(token, comment.full_name, comment.author):
        logger.info('Ignored a mention on %s by %s, who has no write access', where,
                    comment.author)
        return None
    return Mention(session_id, {
        'comment_id': comment.comment_id, 'comment_type': comment.comment_type,
        'url': comment.url, 'author': comment.author, 'body': comment.body,
        'received_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'classification': 'pending', 'status': 'classifying',
        'repository': comment.full_name, 'pull_number': comment.number,
        'reply_to': comment.thread_id,
    })


PULL_REQUEST_EVENT = 'pull_request'


@dataclass
class ClosedPullRequest:
    """A pull request GitHub reported closed, merged when `merged` is true."""

    full_name: str
    number: int
    merged: bool


def parse_closed_pull_request(payload: dict[str, Any]) -> ClosedPullRequest | None:
    """The pull request a pull_request delivery reports closed; None for other actions."""
    if payload.get('action') != 'closed':
        return None
    pull = _object(payload.get('pull_request'), 'pull_request')
    repository = _object(payload.get('repository'), 'repository')
    full_name, number = repository.get('full_name'), pull.get('number')
    if (not isinstance(full_name, str) or not client.NAME_PATTERN.fullmatch(full_name)
            or type(number) is not int or number <= 0):
        raise InvalidEvent('The webhook payload lacks the pull request or the repository.')
    merged = pull.get('merged') is True or bool(pull.get('merged_at'))
    return ClosedPullRequest(full_name, number, merged)


async def _pull_request_event(delivery_id: str | None, payload: dict[str, Any],
                              report_closed) -> bool:
    """Tell the delivery whose pull request was merged or closed, so that it and its flow
    close; true when one was told. `report_closed(session_id, number, merged)` returns
    false when the session has no delivery that waits for it."""
    if delivery_id and await storage.delivery_handled(delivery_id):
        raise AlreadyHandled('This delivery was already handled.')
    closed = parse_closed_pull_request(payload)
    if closed is None or report_closed is None:
        await _record_ignored(delivery_id)
        return False
    where = f'{closed.full_name}#{closed.number}'
    session_id = await storage.pull_request_session(closed.full_name, closed.number)
    if session_id is None:
        logger.info('Ignored the close of %s, which belongs to no delivery', where)
        await _record_ignored(delivery_id)
        return False
    told = await report_closed(session_id, closed.number, closed.merged)
    logger.info('Pull request %s was %s; delivery of session %s %s', where,
                'merged' if closed.merged else 'closed', session_id,
                'told' if told else 'not waiting')
    await _record_ignored(delivery_id)
    return bool(told)


async def handle(connection: dict[str, Any], event: str | None, delivery_id: str | None,
                 payload: dict[str, Any], hand_over, report_closed=None) -> bool:
    """Act on a correctly signed delivery: true when it was accepted and acted on, false
    when it is ignored. `hand_over(session_id, mention)` gives a mention to its delivery,
    returns false when the session has none and raises AlreadyHandled when the delivery
    already has it.

    A mention is claimed, with what is handed over, before it is handed over, and the
    claim stays pending until its delivery has it: a claim whose handover was interrupted,
    such as by the API process stopping, is handed over again by GitHub's redelivery or
    by recover_pending, never suppressed.

    Mentions are handed to their delivery in the order they arrived in: each is admitted,
    taking the next number of that order, before GitHub is asked about it, and handed
    over only after every earlier mention of the delivery was handed over or turned out
    to be ignored. A delivery queues fix requests in the order it is handed them, so a
    later fix request never overtakes an earlier one.

    A pull_request delivery that reports a pull request of a delivery closed, merged or
    not, is passed on with `report_closed(session_id, number, merged)`."""
    if event == PULL_REQUEST_EVENT:
        return await _pull_request_event(delivery_id, payload, report_closed)
    if delivery_id:
        pending = await storage.pending_delivery(delivery_id)
        if pending is not None:
            return await _resume(pending, hand_over)
        if await storage.delivery_handled(delivery_id):
            raise AlreadyHandled('This delivery was already handled.')
    candidate = await mention_candidate(connection, event, payload)
    if candidate is None:
        await _record_ignored(delivery_id)
        return False
    comment, session_id = candidate
    seq, admitted = await storage.admit_mention(session_id, comment.comment_type,
                                                comment.comment_id)
    claimed = False
    try:
        found = await authorise(connection, comment, session_id)
        if found is None:
            await _record_ignored(delivery_id)
            return False
        mention = found.mention
        # The comment and the delivery are claimed together: a storage failure leaves
        # neither claimed, so that GitHub's redelivery is handled.
        conflict = await storage.claim_mention(mention['comment_type'],
                                               mention['comment_id'], delivery_id,
                                               found.session_id, mention, seq)
        claimed = conflict is None
    finally:
        if admitted and not claimed and seq is not None:
            await _release(seq)
    if conflict == 'comment':
        pending = await storage.pending_comment(mention['comment_type'],
                                                mention['comment_id'])
        if pending is not None:
            return await _resume(pending, hand_over)
        raise AlreadyHandled('This comment was already handled.')
    if conflict == 'delivery':
        raise AlreadyHandled('This delivery was already handled.')
    return await _hand_over_in_order(storage.PendingMention(
        mention['comment_type'], mention['comment_id'], found.session_id, mention, seq),
        hand_over)


async def _record_ignored(delivery_id: str | None) -> None:
    # The insert decides atomically: a concurrent copy of this delivery that recorded it
    # first makes this one a redelivery.
    if delivery_id and not await storage.record_delivery(delivery_id):
        raise AlreadyHandled('This delivery was already handled.')


async def _release(seq: int) -> None:
    """Let the mentions admitted after this one go on without it."""
    try:
        await storage.release_admission(seq)
    except Exception as exc:  # noqa: BLE001 - expires after ADMISSION_AGE_SECONDS
        logger.warning('Admission %d not released: %s', seq, exc)


async def _resume(pending: storage.PendingMention, hand_over) -> bool:
    """Hand over a claim this request found pending but did not make, such as a
    redelivery's: when another request hands it over first, this one is a duplicate."""
    logger.info('Handing over the pending mention %d again', pending.comment_id)
    return await _hand_over_in_order(pending, hand_over, resumed=True)


# How long a mention's request waits for the lock that orders its delivery's handovers,
# and for earlier mentions still being authorised; a mention still waiting then stays
# claimed and is handed over, in order, by the request ahead of it or by recovery.
ORDER_WAIT_SECONDS = 20
# An admission this old belongs to a request that stopped before it claimed its mention.
ADMISSION_AGE_SECONDS = 120


def _key(pending: storage.PendingMention) -> tuple[str, int]:
    return pending.comment_type, pending.comment_id


async def _hand_over_in_order(own: storage.PendingMention, hand_over,
                              resumed: bool = False) -> bool:
    """Hand over, under the delivery's ordering lock, its pending mentions in the order
    they arrived, up to and past `own` while none is held back by an earlier mention that
    is still being authorised. True when `own` was accepted or stays claimed for a later
    handover, the result of handing it over otherwise. `resumed` tells that this request
    found the claim pending rather than making it: should another request have handed
    it over meanwhile, AlreadyHandled is raised."""
    result, _ = await _hand_over_session(own.session_id, own, hand_over, resumed)
    return result


async def _hand_over_session(session_id: str, own: storage.PendingMention | None,
                             hand_over, resumed: bool = False) -> tuple[bool, int]:
    """The result for `own` and the number of mentions handed over."""
    async with storage.mention_order_lock(session_id, ORDER_WAIT_SECONDS) as locked:
        if not locked:
            logger.info('Mentions of session %s are being handed over; left pending',
                        session_id)
            return True, 0
        # Only the holder of the lock hands over, so the claim is rechecked now: another
        # request may have handed it over while this one waited for the lock.
        if own is not None and await storage.pending_comment(
                own.comment_type, own.comment_id) is None:
            if resumed:
                # A duplicate of the request that handed it over, such as a redelivery.
                raise AlreadyHandled('This delivery was already handled.')
            # This request's own claim, handed over in order by another request.
            return True, 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ORDER_WAIT_SECONDS
        while True:
            _, earliest = await storage.pending_with_barrier(session_id,
                                                             ADMISSION_AGE_SECONDS)
            if (own is None or own.seq is None or earliest is None or earliest > own.seq
                    or loop.time() >= deadline):
                break
            # An earlier mention is still being authorised: it goes first.
            await asyncio.sleep(0.5)
        result, own_done, count = True, False, 0
        while True:
            # Each step decides from the pending mentions and their admission barrier as
            # read together from one snapshot, taken anew after every handover, so that a
            # mention admitted or claimed meanwhile is ordered with the others: an earlier
            # mention that is not claimed yet always holds back the later ones.
            pendings, earliest = await storage.pending_with_barrier(
                session_id, ADMISSION_AGE_SECONDS)
            if not pendings:
                break
            pending = pendings[0]
            if pending.seq is not None and earliest is not None and pending.seq > earliest:
                # Held back by an earlier mention that is not claimed yet.
                if own is not None and not own_done:
                    logger.info('Mention %d waits for an earlier mention of session %s',
                                own.comment_id, session_id)
                break
            is_own = own is not None and _key(pending) == _key(own)
            try:
                handed = await _hand_over(pending, hand_over)
            except AlreadyHandled:
                if is_own:
                    raise
                continue
            except Exception as exc:  # noqa: BLE001 - stays pending, in order
                if own is not None and not own_done:
                    raise
                logger.warning('Pending mention %d not handed over yet: %s',
                               pending.comment_id, exc)
                break
            count += 1
            if is_own:
                result, own_done = handed, True
        return result, count


async def _hand_over(pending: storage.PendingMention, hand_over) -> bool:
    """Hand the claimed mention to its delivery and record that it has it. Handing over is
    idempotent: the delivery rejects a comment it already has, which also completes the
    claim. Any other failure leaves the claim pending, to be handed over again."""
    try:
        handed = await hand_over(pending.session_id, pending.mention)
    except AlreadyHandled:
        await storage.mark_handed_over(pending.comment_type, pending.comment_id)
        raise
    await storage.mark_handed_over(pending.comment_type, pending.comment_id)
    if not handed:
        logger.info('Ignored a mention for session %s, which has no delivery',
                    pending.session_id)
    return handed


# A claim this old is no longer being handed over by the request that made it.
RECOVERY_AGE_SECONDS = 120


async def recover_pending(hand_over) -> int:
    """Hand over again the claimed mentions whose handover was interrupted; the number
    handed over. A mention that still cannot be handed over stays pending."""
    await storage.purge_admissions(ADMISSION_AGE_SECONDS)
    recovered = 0
    for session_id in await storage.pending_sessions(RECOVERY_AGE_SECONDS):
        # Each delivery's mentions are handed over in the order they arrived; one that
        # still cannot be handed over holds back those after it until the next recovery.
        try:
            _, count = await _hand_over_session(session_id, None, hand_over)
        except Exception as exc:  # noqa: BLE001 - retried by the next recovery
            logger.warning('Pending mentions of session %s not handed over yet: %s',
                           session_id, exc)
            continue
        recovered += count
    return recovered
