from langchain_core.runnables import Runnable

from app.config import OPENAI_API_KEY, OPENAI_BASE_URL, PI_CREDENTIALS_DIR
from app.llm.factory import create_adapter
from app.settings.models import WorkflowType
from app.settings.storage import get_workflow_settings


async def get_model(workflow_type: WorkflowType) -> Runnable:
    return create_adapter(await get_workflow_settings(workflow_type),
                          api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL,
                          pi_agent_dir=str(PI_CREDENTIALS_DIR))
