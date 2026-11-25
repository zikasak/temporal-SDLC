from typing import Any
from temporalio import activity
from app.temporal.tracing import trace_activity
@activity.defn
@trace_activity('analyze-checks-failure', feature='verification', input_parameter='session_id')
async def analyze_checks_failure(session_id: str, excerpt: str, findings: list[dict[str, Any]] | None=None) -> dict[str, Any]:
    """Diagnoses a verification failure from the output excerpt; falls back when the LLM fails. The call runs through the native Claude adapter (app.llm.claude.ChatClaude, chosen when the role's saved settings, app.llm.model.get_workflow_settings, name adapter 'claude'); a test fakes the settings and app.llm.claude.ClaudeSDKClient and reads the ClaudeAgentOptions.env it is given. The CLI environment must contain FORCE_PROMPT_CACHING_5M=1, whatever the Claude login, so cache writes are 5-minute only. Helpers the function calls (get_message_graph, native_session, langchain_config, final_reply, fallback_name, is_limit_error, retry_after, parse_*) run as they are."""
    ...
