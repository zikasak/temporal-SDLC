from langchain_core.runnables import Runnable

from app.config import PI_CREDENTIALS_DIR
from app.llm.factory import create_adapter
from app.settings.endpoint import get_openai_endpoint
from app.settings.models import WORKFLOW_LABELS, WorkflowType
from app.settings.storage import get_workflow_settings


class OpenAIEndpointMissing(ValueError):
    """The workflow type's saved adapter is openai, but no OpenAI endpoint is saved."""


def missing_endpoint_message(workflow_type: str, model: str) -> str:
    label = WORKFLOW_LABELS.get(workflow_type, workflow_type.replace('_', ' ').capitalize())
    return (f'The selected model ({model}) of the {label} stage uses the openai adapter, but '
            'no OpenAI endpoint is set up. Set it up in Settings, or choose another model '
            f'for the {label} stage there, then try again.')


async def get_model(workflow_type: WorkflowType) -> Runnable:
    settings = await get_workflow_settings(workflow_type)
    api_key = base_url = ''
    if settings.adapter == 'openai':
        endpoint = await get_openai_endpoint()
        if endpoint is None:
            raise OpenAIEndpointMissing(missing_endpoint_message(workflow_type, settings.model))
        api_key, base_url = endpoint.api_key, endpoint.base_url
    return create_adapter(settings, api_key=api_key, base_url=base_url,
                          pi_agent_dir=str(PI_CREDENTIALS_DIR))
