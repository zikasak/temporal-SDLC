"""Whether another round of automatic fixes is worth it.

A round of fixes (a repair after failed tests or checks, or a fix of failed CI) costs an
agent run and a verification. The ceiling on rounds is high, so that a long but converging
repair is not cut short; within it, each further round has to be earned by progress. The
decision is made from what the rounds left failing, without the LLM, so it is the same
every time the workflow replays it:

- a round whose failures are exactly those of the rounds before, STALLED_ROUNDS times in
  a row, shows the fixes going round in circles: stop;
- failures that, over the last WINDOW rounds, never went below what they were before
  those rounds, and never changed to other failures, show no progress: stop;
- failures that grew for GROWING_ROUNDS rounds in a row show the fixes making things worse:
  stop;
- anything else, such as fewer failures or other failures than before, is progress: go on,
  up to the ceiling.

Pure and deterministic, for the delivery workflow."""

from typing import Any, TypedDict

STALLED_ROUNDS = 3
WINDOW = 4
GROWING_ROUNDS = 2
# What a round's failures are compared by.
SIGNATURE_ITEMS = 100


class RoundOutcome(TypedDict):
    """What a round left failing: how many, and what (test identities or error lines, with
    what varies between runs taken out)."""
    failures: int
    signature: list[str]


class Decision(TypedDict):
    go: bool
    reason: str


def outcome(failures: int, signature: list[str] | set[str]) -> RoundOutcome:
    return {'failures': max(0, int(failures)),
            'signature': sorted({str(item) for item in signature})[:SIGNATURE_ITEMS]}


def decide(history: list[RoundOutcome] | list[dict[str, Any]], ceiling: int) -> Decision:
    """Whether to start another round after the rounds of `history`, oldest first, the
    failures before the first fix included: a history of n outcomes has had n - 1 rounds."""
    rounds = len(history) - 1
    if not history:
        return {'go': True, 'reason': 'No round has run yet.'}
    if history[-1]['failures'] == 0 and not history[-1]['signature']:
        return {'go': False, 'reason': 'Nothing fails any more.'}
    if rounds >= ceiling:
        return {'go': False, 'reason': f'{ceiling} rounds of fixes ran, the most allowed.'}
    signatures = [tuple(item['signature']) for item in history]
    if (len(history) > STALLED_ROUNDS
            and len(set(signatures[-(STALLED_ROUNDS + 1):])) == 1):
        return {'go': False, 'reason': (f'The same failures remained after {STALLED_ROUNDS} '
                                        'rounds of fixes in a row.')}
    counts = [item['failures'] for item in history]
    if len(history) > GROWING_ROUNDS and all(
            later > earlier for earlier, later in zip(counts[-(GROWING_ROUNDS + 1):-1],
                                                      counts[-GROWING_ROUNDS:])):
        return {'go': False, 'reason': (f'The failures grew in each of the last '
                                        f'{GROWING_ROUNDS} rounds of fixes.')}
    if len(history) > WINDOW:
        before = min(counts[:-WINDOW])
        recent = history[-WINDOW:]
        changed = len({tuple(item['signature']) for item in recent}) > 1
        if min(item['failures'] for item in recent) >= before and not changed:
            return {'go': False, 'reason': (f'The last {WINDOW} rounds of fixes left as many '
                                            'failures as before, and the same ones.')}
        if min(item['failures'] for item in recent) > before:
            return {'go': False, 'reason': (f'The last {WINDOW} rounds of fixes left more '
                                            'failures than there were before them.')}
    return {'go': True, 'reason': 'The last round of fixes changed what fails.'}
