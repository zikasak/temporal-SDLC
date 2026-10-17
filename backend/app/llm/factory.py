"""Provider construction with explicit settings; safe to copy into agent images."""
from urllib.parse import urlparse

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from app.settings.models import ModelSettings


def create_model(*, model_name: str, api_key: str, base_url: str,
                 thinking_level: str = 'default') -> BaseChatModel:
    if urlparse(base_url).hostname == 'generativelanguage.googleapis.com':
        from langchain_google_genai import ChatGoogleGenerativeAI

        if thinking_level == 'default':
            thinking = {}
        elif thinking_level == 'none':
            thinking = {'thinking_budget': 0}
        else:
            thinking = {'thinking_level': 'high' if thinking_level in {'xhigh', 'max'} else thinking_level}
        return ChatGoogleGenerativeAI(model=model_name, api_key=api_key, max_retries=0,
                                      **thinking)
    # Streamed so the read timeout bounds the gap between chunks, not the whole generation.
    return ChatOpenAI(model=model_name, api_key=api_key, base_url=base_url, streaming=True,
                      stream_usage=True,
                      timeout=httpx.Timeout(connect=10, read=120, write=10, pool=10),
                      max_retries=0, use_responses_api=False,
                      **({} if thinking_level == 'default' else
                         {'reasoning_effort': thinking_level}))


def create_adapter(settings: ModelSettings, *, api_key: str, base_url: str,
                   pi_agent_dir: str | None = None) -> BaseChatModel:
    if settings.adapter == 'pi':
        from app.llm.pi import ChatPi

        return ChatPi(model=settings.model, thinking_level=settings.thinking_level,
                      agent_dir=pi_agent_dir)
    if settings.adapter == 'claude':
        from app.llm.claude import ChatClaude

        return ChatClaude(model=settings.model, thinking_level=settings.thinking_level)
    return create_model(model_name=settings.model, api_key=api_key, base_url=base_url,
                        thinking_level=settings.thinking_level)
