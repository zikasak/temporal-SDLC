"""Trace activity executions, never deterministic/replayed workflow code."""
from functools import wraps
from inspect import signature
from typing import TYPE_CHECKING

from temporalio import activity

from app.tracing import trace_operation

if TYPE_CHECKING:
    from langfuse._client.constants import ObservationTypeLiteralNoEvent


def activity_metadata() -> dict[str, str | None]:
    if not activity.in_activity():
        return {}
    info = activity.info()
    return {'temporal_workflow_id': info.workflow_id,
            'temporal_run_id': info.workflow_run_id,
            'temporal_activity_id': info.activity_id,
            'temporal_attempt': str(info.attempt)}


def trace_activity(name: str, *, feature: str,
                   as_type: 'ObservationTypeLiteralNoEvent' = 'span',
                   input_parameter: str | None = None):
    def decorate(fn):
        parameters = signature(fn)

        @wraps(fn)
        async def execute(*args, **kwargs):
            bound = parameters.bind(*args, **kwargs)
            bound.apply_defaults()
            values = bound.arguments
            info = activity.info() if activity.in_activity() else None
            session_id = values.get('session_id') or (info.workflow_id if info else None)
            metadata = activity_metadata()
            source_session = getattr(values.get('state'), 'source_session_id', '')
            if source_session:
                metadata['source_session_id'] = source_session
            if input_parameter:
                trace_input = values.get(input_parameter)
            else:
                state = values.get('state')
                messages = values.get('messages', getattr(state, 'messages', []))
                trace_input = messages if isinstance(messages, str) else next(
                    (item.get('content', '') for item in reversed(messages)
                     if item.get('role') == 'user'), None,
                )
            with trace_operation(name, session_id=session_id, feature=feature,
                                 as_type=as_type, input=trace_input, metadata=metadata) as span:
                result = await fn(*args, **kwargs)
                if span is not None:
                    output = result.get('reply', result) if isinstance(result, dict) else result
                    span.update(output=output)
                return result

        return execute
    return decorate
