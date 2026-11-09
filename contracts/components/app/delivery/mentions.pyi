from typing import Any


async def recording(session_id: str, comment_type: str, comment_id: int) -> str | None:
    """Which handover recorded the mention of comment `comment_id` of `comment_type` for
    the session, as '<run ID>:<update ID>'; None while it is not recorded."""
    ...


async def history(session_id: str) -> list[dict[str, Any]]:
    """Every mention of the session's delivery, oldest first, with its text."""
    ...
