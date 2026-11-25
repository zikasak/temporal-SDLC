from app.settings.models import ModelSettings
from langchain_core.runnables import Runnable
def create_adapter(settings: ModelSettings, *, api_key: str, base_url: str, pi_agent_dir: str | None=None) -> Runnable:
    ...
