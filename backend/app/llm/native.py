"""What native agents (ChatClaude, ChatPi) share: each owns its history, its model/tool
loop over the flow's MCP tools (app.llm.mcp) and its compaction, so the application only
sends it new messages."""
import json

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from app.llm.limits import AGENT_RECURSION_LIMIT


class AgentTurnLimit(RuntimeError):
    """The native agent used the allowed number of model turns."""


def first_prompt(messages: list[BaseMessage]) -> str:
    """The prompt that starts a native session from the application's messages."""
    history = [message for message in messages if not isinstance(message, SystemMessage)]
    if len(history) == 1 and isinstance(history[0], HumanMessage):
        return history[0].text
    # Application conversations are context for a new native session, never forged
    # transcript rows. Subsequent requests use the native session itself.
    return 'Conversation context:\n' + json.dumps([
        {'role': message.type, 'content': message.content} for message in history
    ], ensure_ascii=False) + '\n\nContinue the conversation and complete the latest user request.'


def continuation(added: list[BaseMessage]) -> str:
    """The prompt that appends `added` to a native session: the user's text, or the
    whole exchange when it also holds replies the session has not seen."""
    if all(isinstance(message, HumanMessage) for message in added):
        return '\n\n'.join(message.text for message in added)
    return 'Conversation since your last reply:\n' + json.dumps([
        {'role': message.type, 'content': message.content} for message in added
    ], ensure_ascii=False) + '\n\nContinue the conversation and complete the latest user request.'


class NativeGraph:
    """The flow's invocation interface, with the native agent owning all model/tool
    turns."""

    def __init__(self, model, tools, until, after_model_request=None):
        self.model, self.tools, self.until = model, tools, until
        self.after_model_request = after_model_request

    async def ainvoke(self, state, config=None):
        try:
            reply = await self.model.ainvoke(
                state['messages'], tools=self.tools, until=self.until,
                after_model_request=self.after_model_request,
                max_turns=max(1, (config or {}).get('recursion_limit', AGENT_RECURSION_LIMIT)),
            )
        except AgentTurnLimit as exc:
            from langgraph.errors import GraphRecursionError
            raise GraphRecursionError(str(exc)) from exc
        return {'messages': [*state['messages'], reply]}
