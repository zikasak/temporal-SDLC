import logging

from temporalio.client import Client

from app.config import TEMPORAL_ADDRESS

logger = logging.getLogger(__name__)


class TemporalUnavailable(Exception):
    """Temporal could not be reached or failed to answer."""


async def connect_temporal() -> Client:
    try:
        return await Client.connect(TEMPORAL_ADDRESS)
    except Exception as exc:
        logger.error("Could not connect to Temporal at %s: %s", TEMPORAL_ADDRESS, exc)
        raise TemporalUnavailable("Temporal is unavailable") from exc
