"""Settings contracts shared by the API, workers and isolated agents."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WorkflowType = Literal[
    'chat', 'requirements', 'api_design', 'implementation', 'test_generation',
    'diagnosis', 'documentation', 'review',
]
Adapter = Literal['pi', 'openai', 'claude']
WORKFLOW_LABELS: dict[str, str] = {
    'chat': 'Chat', 'requirements': 'Requirements', 'api_design': 'API design',
    'implementation': 'Implementation', 'test_generation': 'Test generation',
    'diagnosis': 'Diagnosis', 'documentation': 'Documentation', 'review': 'Review',
}
SETTINGS_FILENAME = 'llm-settings.json'


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    adapter: Adapter = 'openai'
    model: str = Field(default='ddalcu/Qwen3.8-27B-MLX-Serve-4bit', min_length=1,
                       max_length=300)
    thinking_level: str = Field(default='default', min_length=1, max_length=100)

    @field_validator('model')
    @classmethod
    def valid_model(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError('Model must not contain control characters')
        return value


class WorkflowSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')

    workflows: dict[WorkflowType, ModelSettings]

    @model_validator(mode='after')
    def all_workflows(self):
        if set(self.workflows) != set(WORKFLOW_LABELS):
            raise ValueError('Settings must include every LLM workflow type')
        return self
