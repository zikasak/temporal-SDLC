from typing import Literal
from pydantic import BaseModel
class ModelSettings(BaseModel):
    adapter: Literal['pi', 'openai', 'claude'] = 'openai'
    model: str
    thinking_level: str
    compaction_tokens: int
