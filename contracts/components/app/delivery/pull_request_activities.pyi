from typing import Any
from app.delivery.pull_request import PullRequestText
from temporalio import activity
@activity.defn
async def write_pull_request_text(session_id: str, publication: dict[str, Any], change: dict[str, Any]) -> PullRequestText:
    """Writes a pull request The diff (app.github.checkout.delivery_diff) is read as it is; a test makes it work or fakes it.'s title and description (the publication has a 'commit', the checkout diff may be unavailable, raising ApplicationError). The call runs through the native Claude adapter (app.llm.claude.ChatClaude, chosen when the role's saved settings, app.llm.model.get_workflow_settings, name adapter 'claude'); a test fakes the settings and app.llm.claude.ClaudeSDKClient and reads the ClaudeAgentOptions.env it is given. The CLI environment must contain FORCE_PROMPT_CACHING_5M=1, whatever the Claude login, so cache writes are 5-minute only. Helpers the function calls (get_message_graph, native_session, langchain_config, final_reply, fallback_name, is_limit_error, retry_after, parse_*) run as they are."""
    ...

from app.config import GENERATED_CODE_DIR as GENERATED_CODE_DIR
from app.llm.model import get_model as get_model
from app.delivery.pull_request import parse_text as parse_text
