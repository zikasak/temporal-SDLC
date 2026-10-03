from functools import lru_cache

from langchain_core.language_models.chat_models import BaseChatModel

from app.config import OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL
from app.llm.factory import create_model


@lru_cache(maxsize=1)
def get_model() -> BaseChatModel:
    return create_model(model_name=OPENAI_MODEL, api_key=OPENAI_API_KEY,
                        base_url=OPENAI_BASE_URL)
