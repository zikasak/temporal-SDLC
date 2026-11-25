"""Provider construction with explicit settings; safe to copy into agent images."""
from langchain_core.runnables import Runnable
from pydantic import SecretStr

from app.llm.pi import NO_API_KEY, ChatPi, PiEndpoint
from app.settings.models import ModelSettings

__all__ = ['NO_API_KEY', 'create_adapter']


def create_adapter(settings: ModelSettings, *, api_key: str, base_url: str,
                   pi_agent_dir: str | None = None) -> Runnable:
    """The settings' adapter. The openai adapter is Pi calling the OpenAI-compatible
    endpoint at `base_url` with `api_key`, in place of Pi's own providers."""
    if settings.adapter == 'claude':
        from app.llm.claude import ChatClaude

        return ChatClaude(model=settings.model, thinking_level=settings.thinking_level)
    endpoint = (PiEndpoint(base_url=base_url, api_key=SecretStr(api_key))
                if settings.adapter == 'openai' else None)
    return ChatPi(model=settings.model, thinking_level=settings.thinking_level,
                  agent_dir=pi_agent_dir, endpoint=endpoint,
                  compaction_tokens=settings.compaction_tokens)
