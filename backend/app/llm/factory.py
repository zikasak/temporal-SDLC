"""Provider construction with explicit settings; safe to copy into agent images."""
from urllib.parse import urlparse

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI


def create_model(*, model_name: str, api_key: str, base_url: str) -> BaseChatModel:
    if urlparse(base_url).hostname == 'generativelanguage.googleapis.com':
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(model=model_name, api_key=api_key, max_retries=0)
    return ChatOpenAI(model=model_name, api_key=api_key, base_url=base_url,
                      timeout=120, max_retries=0, use_responses_api=False)
