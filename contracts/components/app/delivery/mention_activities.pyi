from typing import Any
from temporalio import activity
async def classify(session_id: str, body: str) -> str:
    """Whether a pull request mention asks for a fix, a clarification or is unclear. The call runs through the native Claude adapter (app.llm.claude.ChatClaude, chosen when the role's saved settings, app.llm.model.get_workflow_settings, name adapter 'claude'); a test fakes the settings and app.llm.claude.ClaudeSDKClient and reads the ClaudeAgentOptions.env it is given. The CLI environment must contain FORCE_PROMPT_CACHING_5M=1, whatever the Claude login, so cache writes are 5-minute only. Helpers the function calls (get_message_graph, native_session, langchain_config, final_reply, fallback_name, is_limit_error, retry_after, parse_*) run as they are."""
    ...
async def answer(session_id: str, body: str, context: dict[str, Any]) -> str:
    """Answers a pull request mention; Repository checkout and tools (app.github.checkout.snapshot, app.tools.repository.build, run_sync, async_context) run as they are. the context has no publication, so no repository tools are used. The call runs through the native Claude adapter (app.llm.claude.ChatClaude, chosen when the role's saved settings, app.llm.model.get_workflow_settings, name adapter 'claude'); a test fakes the settings and app.llm.claude.ClaudeSDKClient and reads the ClaudeAgentOptions.env it is given. The CLI environment must contain FORCE_PROMPT_CACHING_5M=1, whatever the Claude login, so cache writes are 5-minute only. Helpers the function calls (get_message_graph, native_session, langchain_config, final_reply, fallback_name, is_limit_error, retry_after, parse_*) run as they are."""
    ...
@activity.defn
async def classify_mention(session_id: str, body: str) -> str:
    """Activity wrapper of classify. The call runs through the native Claude adapter (app.llm.claude.ChatClaude, chosen when the role's saved settings, app.llm.model.get_workflow_settings, name adapter 'claude'); a test fakes the settings and app.llm.claude.ClaudeSDKClient and reads the ClaudeAgentOptions.env it is given. The CLI environment must contain FORCE_PROMPT_CACHING_5M=1, whatever the Claude login, so cache writes are 5-minute only. Helpers the function calls (get_message_graph, native_session, langchain_config, final_reply, fallback_name, is_limit_error, retry_after, parse_*) run as they are."""
    ...
@activity.defn
async def answer_mention(session_id: str, body: str, context: dict[str, Any]) -> str:
    """Activity wrapper of answer. The call runs through the native Claude adapter (app.llm.claude.ChatClaude, chosen when the role's saved settings, app.llm.model.get_workflow_settings, name adapter 'claude'); a test fakes the settings and app.llm.claude.ClaudeSDKClient and reads the ClaudeAgentOptions.env it is given. The CLI environment must contain FORCE_PROMPT_CACHING_5M=1, whatever the Claude login, so cache writes are 5-minute only. Helpers the function calls (get_message_graph, native_session, langchain_config, final_reply, fallback_name, is_limit_error, retry_after, parse_*) run as they are."""
    ...

from app.config import GENERATED_CODE_DIR as GENERATED_CODE_DIR
from app.temporal.activities import _github_token as _github_token
from app.llm.model import get_model as get_model
