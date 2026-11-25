from typing import Any
from temporalio import activity
from app.temporal.tracing import trace_activity
@activity.defn
@trace_activity('name-task-flow', feature='tasks', input_parameter='task')
async def name_task_flow(task: str, requirements: list[dict[str, Any]]) -> str:
    """Names a task flow from the task text or its requirements. The call runs through the native Claude adapter (app.llm.claude.ChatClaude, chosen when the role's saved settings, app.llm.model.get_workflow_settings, name adapter 'claude'); a test fakes the settings and app.llm.claude.ClaudeSDKClient and reads the ClaudeAgentOptions.env it is given. The CLI environment must contain FORCE_PROMPT_CACHING_5M=1, whatever the Claude login, so cache writes are 5-minute only. Helpers the function calls (get_message_graph, native_session, langchain_config, final_reply, fallback_name, is_limit_error, retry_after, parse_*) run as they are."""
    ...

from app.llm.model import get_model as get_model
