"""Shared, environment-only tracing; safe to copy into isolated agent images."""
import atexit
import json
import logging
import os
import re
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)
_SECRET_FIELD = re.compile(r'(api[_-]?key|password|secret|authorization|access[_-]?token)', re.I)
_EMAIL = re.compile(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b')
_TOKEN = re.compile(r'\b(?:sk-lf-|pk-lf-|sk-proj-|sk-)[A-Za-z0-9_-]+|\bAIza[\w-]+')
_trace_attributes = ContextVar('application_trace_attributes', default={})


def _enabled() -> bool:
    return os.getenv('LANGFUSE_TRACING_ENABLED', 'true').lower() not in {'false', '0', 'no'}


def _redact(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        # Tool results often contain JSON serialized inside a message string.
        if value.lstrip().startswith(('{', '[')):
            try:
                parsed = json.loads(value)
            except ValueError:
                pass
            else:
                if isinstance(parsed, (dict, list)):
                    redacted = _redact(parsed, secrets)
                    if redacted != parsed:
                        value = json.dumps(redacted)
        for secret in secrets:
            value = value.replace(secret, '[REDACTED]')
        return _EMAIL.sub('[REDACTED EMAIL]', _TOKEN.sub('[REDACTED]', value))
    if isinstance(value, dict):
        return {key: '[REDACTED]' if _SECRET_FIELD.search(key) else _redact(item, secrets)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, secrets) for item in value]
    return value


def _normalize_tool_calls(value: Any) -> Any:
    """Use OpenAI-shaped tool calls so Langfuse can render conversation cards."""
    if isinstance(value, list):
        return [_normalize_tool_calls(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _normalize_tool_calls(item) for key, item in value.items()}
    if result.get('role') == 'assistant' and isinstance(result.get('tool_calls'), list):
        result['tool_calls'] = [
            {'id': call.get('id'), 'type': 'function',
             'function': {'name': call['name'], 'arguments': json.dumps(call.get('args', {}))}}
            if 'name' in call and 'args' in call else call
            for call in result['tool_calls']
        ]
    return result


def mask_otel_spans(*, params):
    """Mask the final export, including data emitted by LangChain callbacks."""
    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    secrets = tuple(sorted({value for key, value in os.environ.items()
                            if value and any(part in key.upper() for part in
                                             ('KEY', 'TOKEN', 'PASSWORD', 'SECRET'))},
                           key=len, reverse=True))
    capture_content = os.getenv('APP_TRACE_CONTENT_ENABLED', 'false').lower() == 'true'
    patches = {}
    for identifier, span in params.spans.items():
        replacements = {}
        for key, value in span.attributes.items():
            if not capture_content and key in {'langfuse.observation.input',
                                               'langfuse.observation.output',
                                               'langfuse.trace.input', 'langfuse.trace.output'}:
                replacements[key] = '[CONTENT CAPTURE DISABLED]'
                continue
            if (not capture_content and key == 'langfuse.observation.status_message'
                    and isinstance(value, str)
                    and not re.fullmatch(r'[A-Za-z][A-Za-z0-9]*(?:Error|Cancelled)', value)):
                replacements[key] = 'Operation failed (details excluded)'
                continue
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                except (ValueError, TypeError):
                    masked = _redact(value, secrets)
                else:
                    normalized = (_normalize_tool_calls(parsed)
                                  if key in {'langfuse.observation.input',
                                             'langfuse.observation.output'} else parsed)
                    redacted = _redact(normalized, secrets)
                    masked = json.dumps(redacted) if redacted != parsed else value
            else:
                masked = _redact(value, secrets)
            if masked != value:
                replacements[key] = masked
        if replacements:
            patches[identifier] = OtelSpanPatch(set_attributes=replacements)
    return MaskOtelSpansResult(span_patches=patches)


def _privacy_exporter():
    """The SDK masking hook cannot edit OTEL status text or exception events."""
    import base64
    from importlib.metadata import version

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import Status

    class PrivacyExporter(OTLPSpanExporter):
        def export(self, spans):
            clean = [ReadableSpan(
                name=span.name, context=span.context, parent=span.parent,
                resource=span.resource, attributes=span.attributes, links=span.links,
                kind=span.kind, status=Status(span.status.status_code),
                # All useful failure details are in masked observation attributes.
                # Exception events can carry unmaskable messages and stack traces.
                events=(), start_time=span.start_time, end_time=span.end_time,
                instrumentation_scope=span.instrumentation_scope,
            ) for span in spans]
            return super().export(clean)

    auth = base64.b64encode(
        f"{os.environ['LANGFUSE_PUBLIC_KEY']}:{os.environ['LANGFUSE_SECRET_KEY']}".encode()
    ).decode()
    return PrivacyExporter(
        endpoint=os.environ['LANGFUSE_BASE_URL'].rstrip('/') + '/api/public/otel/v1/traces',
        # Match the official SDK's routing/version headers. Do not force a
        # self-hosted project's ingestion migration through a custom header.
        headers={'Authorization': f'Basic {auth}', 'x-langfuse-sdk-name': 'python',
                 'x-langfuse-sdk-version': version('langfuse'),
                 'x-langfuse-public-key': os.environ['LANGFUSE_PUBLIC_KEY']},
        timeout=10,
    )


# The MCP SDK traces every tools/call it serves to Claude, without arguments or results.
# The Claude plugin already exports those calls, with their content, from the transcript.
_IGNORED_SCOPES = {'mcp-python-sdk'}


def _should_export(span) -> bool:
    from langfuse.span_filter import is_default_export_span

    scope = span.instrumentation_scope
    return (scope is None or scope.name not in _IGNORED_SCOPES) and is_default_export_span(span)


@lru_cache(maxsize=1)
def _client():
    # Lazy initialization is important: host config loads .env first, while
    # containers receive only explicitly allowlisted environment variables.
    from langfuse import Langfuse

    try:
        client = Langfuse(mask_otel_spans=mask_otel_spans,
                          span_exporter=_privacy_exporter(),
                          should_export_span=_should_export,
                          environment=os.getenv('LANGFUSE_TRACING_ENVIRONMENT', 'development'))
    except Exception:
        logger.warning('Langfuse initialization failed; tracing disabled')
        return None
    atexit.register(client.shutdown)
    return client


def tracing_client():
    if not _enabled() or not all(os.getenv(key) for key in
                                ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'LANGFUSE_BASE_URL')):
        return None
    return _client()


def langchain_config(**settings) -> dict:
    """New handler per execution avoids state shared across concurrent turns."""
    client = tracing_client()
    if client is not None:
        from langfuse.langchain import CallbackHandler
        class AgentCallbackHandler(CallbackHandler):
            @property
            def ignore_chain(self) -> bool:
                # Application agent/span roots already own the orchestration.
                # Avoid LangGraph's routing/node wrappers and retain native
                # generation, tool and retriever callbacks under that root.
                return True

            def on_llm_end(self, response, *, run_id, **kwargs):
                # Adapters that price their own requests (Pi) pass the cost alongside
                # the generation; it must land before the handler ends the observation.
                info = response.generations[-1][-1].generation_info or {}
                generation = self._runs.get(run_id)
                if info.get('cost_details') and generation is not None:
                    generation.update(cost_details=info['cost_details'])
                return super().on_llm_end(response, run_id=run_id, **kwargs)

        settings['callbacks'] = [AgentCallbackHandler(public_key=os.environ['LANGFUSE_PUBLIC_KEY'])]
    return settings


@contextmanager
def trace_operation(name: str, *, session_id: str | None, feature: str,
                    input: Any = None, metadata: dict | None = None,
                    as_type: str = 'span', trace_context: dict | None = None):
    client = tracing_client()
    if client is None:
        yield None
        return
    from langfuse import propagate_attributes

    stack = ExitStack()
    try:
        stack.enter_context(propagate_attributes(session_id=session_id, trace_name=name,
                                                tags=[feature], metadata=metadata))
        span = stack.enter_context(client.start_as_current_observation(
            name=name, as_type=as_type, input=input, metadata=metadata,
            trace_context=trace_context,
        ))
    except Exception:
        stack.close()
        logger.warning('Could not start Langfuse observation; continuing without tracing')
        yield None
        return
    try:
        token = _trace_attributes.set({'session_id': session_id, 'feature': feature})
        try:
            yield span
        finally:
            _trace_attributes.reset(token)
    except BaseException as exc:
        # Do not export exception messages/stack traces: provider errors can
        # contain credentials or confidential inputs. Keep the failure type.
        span.update(level='ERROR', status_message=type(exc).__name__)
        raise
    finally:
        # Close normally to avoid automatic unmasked OTEL exception events.
        stack.close()


def container_trace_environment(session_id: str, role: str) -> dict[str, str]:
    """Only agent containers receive telemetry credentials; never built images."""
    client = tracing_client()
    if client is None:
        return {}
    keys = ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'LANGFUSE_BASE_URL',
            'LANGFUSE_TRACING_ENABLED', 'LANGFUSE_TRACING_ENVIRONMENT',
            'LANGFUSE_SAMPLE_RATE', 'LANGFUSE_RELEASE', 'APP_TRACE_CONTENT_ENABLED')
    environment = {key: os.environ[key] for key in keys if key in os.environ}
    environment.update(APP_TRACE_SESSION_ID=session_id, APP_TRACE_AGENT_ROLE=role)
    trace_id = client.get_current_trace_id()
    parent_id = client.get_current_observation_id()
    if trace_id and parent_id:
        environment.update(APP_TRACE_ID=trace_id, APP_TRACE_PARENT_ID=parent_id)
    return environment


def container_trace_context() -> dict | None:
    trace_id = os.getenv('APP_TRACE_ID', '')
    parent_id = os.getenv('APP_TRACE_PARENT_ID', '')
    if re.fullmatch(r'[0-9a-f]{32}', trace_id) and re.fullmatch(r'[0-9a-f]{16}', parent_id):
        return {'trace_id': trace_id, 'parent_span_id': parent_id}
    return None


def claude_trace_environment() -> dict[str, str]:
    """Explicit cross-process parent context for the Claude observability plugin."""
    environment = {'APP_CLAUDE_TRACING_ENABLED': 'false'}
    client = tracing_client()
    if client is None:
        return environment
    from opentelemetry import trace

    current = trace.get_current_span().get_span_context()
    if current.is_valid and not current.trace_flags.sampled:
        return environment
    attributes = _trace_attributes.get()
    environment.update(
        APP_CLAUDE_TRACING_ENABLED='true',
        CC_LANGFUSE_PARENT_TRACE_ID=client.get_current_trace_id() or '',
        CC_LANGFUSE_PARENT_SPAN_ID=client.get_current_observation_id() or '',
        APP_CLAUDE_TRACE_SESSION_ID=attributes.get('session_id') or '',
        APP_CLAUDE_TRACE_FEATURE=attributes.get('feature') or '',
        # Images would otherwise upload outside the application's span masking.
        CC_LANGFUSE_CAPTURE_IMAGES='false', LANGFUSE_MEDIA_UPLOAD_ENABLED='false',
    )
    return environment


def shutdown_tracing() -> None:
    """Flush after short-lived agents finish and before workers leave their loop."""
    if _client.cache_info().currsize:
        client = _client()
        if client is not None:
            client.shutdown()
            atexit.unregister(client.shutdown)
