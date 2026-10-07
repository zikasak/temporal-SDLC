from functools import lru_cache

from langchain_core.language_models.chat_models import BaseChatModel

from app.config import (
    CLAUDE_MODEL, LLM_HARNESS, OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL,
    PI_CREDENTIALS_DIR, PI_MODEL,
)
from app.llm.factory import create_model


@lru_cache(maxsize=1)
def get_model() -> BaseChatModel:
    if LLM_HARNESS == 'pi':
        from app.llm.pi import ChatPi

        return ChatPi(model=PI_MODEL, agent_dir=str(PI_CREDENTIALS_DIR))
    if LLM_HARNESS == 'claude':
        from app.llm.claude import ChatClaude

        return ChatClaude(model=CLAUDE_MODEL)
    return create_model(model_name=OPENAI_MODEL, api_key=OPENAI_API_KEY,
                        base_url=OPENAI_BASE_URL)
