"""Provider construction with explicit settings; safe to copy into agent images."""
from urllib.parse import urlparse

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI


def create_model(*, model_name: str, api_key: str, base_url: str) -> BaseChatModel:
    if urlparse(base_url).hostname == 'generativelanguage.googleapis.com':
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(model=model_name, api_key=api_key, max_retries=0)
    # Streamed so the read timeout bounds the gap between chunks, not the whole generation.
    return ChatOpenAI(model=model_name, api_key=api_key, base_url=base_url, streaming=True,
                      stream_usage=True,
                      timeout=httpx.Timeout(connect=10, read=120, write=10, pool=10),
                      max_retries=0, use_responses_api=False)
