from langchain_core.runnables import Runnable
from typing import Literal
async def get_model(workflow_type: Literal['chat', 'requirements', 'requirements_review', 'api_design', 'implementation', 'test_generation', 'diagnosis', 'documentation', 'review', 'routing']) -> Runnable:
    """The model of a workflow role built from its saved settings; the claude adapter gives a ChatClaude."""
    ...

from app.config import PI_CREDENTIALS_DIR as PI_CREDENTIALS_DIR
from app.llm.factory import create_adapter as create_adapter
from app.settings.endpoint import get_openai_endpoint as get_openai_endpoint
from app.settings.storage import get_workflow_settings as get_workflow_settings
