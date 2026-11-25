from app.settings.models import ModelSettings
from typing import Literal
async def get_workflow_settings(workflow_type: Literal['chat', 'requirements', 'requirements_review', 'api_design', 'implementation', 'test_generation', 'diagnosis', 'documentation', 'review', 'routing']) -> ModelSettings:
    """The saved settings of a workflow role; a test fakes this to choose the adapter 'claude'. The names connect and default_model_settings it uses run as they are."""
    ...

