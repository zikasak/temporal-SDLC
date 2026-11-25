"""Settings contracts shared by the API, workers and isolated agents."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WorkflowType = Literal[
    'chat', 'requirements', 'requirements_review', 'api_design', 'implementation',
    'test_generation', 'diagnosis', 'documentation', 'review', 'routing',
]
Adapter = Literal['pi', 'openai', 'claude']
WORKFLOW_LABELS: dict[str, str] = {
    'chat': 'Chat', 'requirements': 'Requirements',
    # The requirements reviewer of tasks and onboardings, apart from the code review.
    'requirements_review': 'Requirements review', 'api_design': 'API design',
    'implementation': 'Implementation', 'test_generation': 'Test generation',
    'diagnosis': 'Diagnosis', 'documentation': 'Documentation', 'review': 'Review',
    'routing': 'Routing',
}
SETTINGS_FILENAME = 'llm-settings.json'
# Where Pi compacts a conversation with the openai adapter's endpoint when nothing else is
# saved: 75% of the 64000 tokens assumed as the context window of a model that reports none.
DEFAULT_COMPACTION_TOKENS = 48000
# Fewer tokens leave no room for the system prompt, the contracts and a recent batch.
MIN_COMPACTION_TOKENS = 4000
DEFAULT_CONTRACT_EMBED_CHARS = 32000


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    adapter: Adapter = 'openai'
    model: str = Field(default='ddalcu/Qwen3.8-27B-MLX-Serve-4bit', min_length=1,
                       max_length=300)
    thinking_level: str = Field(default='default', min_length=1, max_length=100)
    # Tokens in use at which Pi compacts a conversation with the openai adapter's endpoint,
    # whose model reports no context window; 0 compacts only once the endpoint refuses a
    # context as too long. Pi's own providers and Claude compact by their models' windows.
    compaction_tokens: int = Field(default=DEFAULT_COMPACTION_TOKENS, ge=0)

    @field_validator('model')
    @classmethod
    def valid_model(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError('Model must not contain control characters')
        return value

    @field_validator('compaction_tokens')
    @classmethod
    def valid_compaction(cls, value: int) -> int:
        if 0 < value < MIN_COMPACTION_TOKENS:
            raise ValueError(f'Compaction must start at {MIN_COMPACTION_TOKENS} tokens or '
                             'more, or be 0 to never compact')
        return value


class AgentSettings(BaseModel):
    """Settings shared by the agents of every workflow type."""
    model_config = ConfigDict(extra='forbid')

    # Contracts whose embedded form is longer reach coding agents as an index plus
    # read-only files they query, instead of whole in every model invocation.
    contract_embed_chars: int = Field(default=DEFAULT_CONTRACT_EMBED_CHARS, ge=1000,
                                      le=10_000_000)


def default_model_settings(openai_ready: bool) -> ModelSettings:
    """The settings of a workflow type with none saved: the openai adapter only while an
    OpenAI endpoint is saved, pi otherwise."""
    return ModelSettings() if openai_ready else ModelSettings(adapter='pi')


class WorkflowSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')

    workflows: dict[WorkflowType, ModelSettings]

    @model_validator(mode='after')
    def all_workflows(self):
        if set(self.workflows) != set(WORKFLOW_LABELS):
            raise ValueError('Settings must include every LLM workflow type')
        return self
