"""Export the Langfuse Pi plugin's observations with the application's privacy policy.

The plugin (@langfuse/pi-observability-plugin) runs inside the `pi` process and exports
OTLP to whatever Langfuse URL it is given. ChatPi gives it this relay, served beside its
MCP tools on loopback: the spans are masked as the application's own exporter masks them
(app.tracing), labelled with the application's session and feature, and only then sent
to Langfuse with the application's credentials, which Pi never sees."""
import base64
import gzip
import logging
from typing import Any

from app.tracing import _content_captured, _secrets, mask_attribute, otlp_endpoint, otlp_headers

logger = logging.getLogger(__name__)
# Where the plugin's exporter posts below the base URL it is given.
OTLP_PATH = '/api/public/otel/v1/traces'
EXPORT_TIMEOUT = 10
# Langfuse's trace attributes, as the Python SDK's propagate_attributes sets them.
SESSION_ATTRIBUTE = 'session.id'
TAGS_ATTRIBUTE = 'langfuse.trace.tags'


def _hex_ids_to_base64(item: dict[str, Any]) -> None:
    """OTLP/JSON spells IDs in hex, where protobuf's JSON mapping expects base64."""
    for key in ('traceId', 'spanId', 'parentSpanId'):
        value = item.get(key)
        if isinstance(value, str):
            item[key] = base64.b64encode(bytes.fromhex(value)).decode() if value else ''


def _decode(body: bytes, content_type: str, encoding: str):
    from google.protobuf import json_format
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    if encoding == 'gzip':
        body = gzip.decompress(body)
    request = ExportTraceServiceRequest()
    if 'json' not in content_type:
        request.ParseFromString(body)
        return request
    import json

    payload = json.loads(body)
    for resource in payload.get('resourceSpans', []):
        for scope in resource.get('scopeSpans', []):
            for span in scope.get('spans', []):
                _hex_ids_to_base64(span)
                for link in span.get('links', []):
                    _hex_ids_to_base64(link)
    return json_format.ParseDict(payload, request, ignore_unknown_fields=True)


def _attribute(attributes, key: str):
    for item in attributes:
        if item.key == key:
            return item.value
    return attributes.add(key=key).value


def masked_export(body: bytes, *, content_type: str = 'application/json',
                  encoding: str = '', session_id: str = '', feature: str = '') -> bytes:
    """The plugin's OTLP export as protobuf, masked and labelled for the application."""
    request = _decode(body, content_type, encoding)
    secrets = _secrets()
    capture_content = _content_captured()
    for resource in request.resource_spans:
        for scope in resource.scope_spans:
            for span in scope.spans:
                for item in span.attributes:
                    value = item.value
                    if value.HasField('string_value'):
                        value.string_value = mask_attribute(
                            item.key, value.string_value, secrets, capture_content)
                    elif value.HasField('array_value'):
                        for element in value.array_value.values:
                            if element.HasField('string_value'):
                                element.string_value = mask_attribute(
                                    item.key, element.string_value, secrets, capture_content)
                # Exception events and status text can carry unmaskable messages and stack
                # traces; the masked observation attributes keep what a failure needs.
                del span.events[:]
                span.status.message = ''
                # The application's session and feature, as its own observations have.
                if session_id:
                    _attribute(span.attributes, SESSION_ATTRIBUTE).string_value = session_id
                if feature:
                    tags = _attribute(span.attributes, TAGS_ATTRIBUTE).array_value.values
                    if feature not in [tag.string_value for tag in tags]:
                        tags.add().string_value = feature
    return request.SerializeToString()


async def forward(body: bytes) -> int:
    """Send a masked export to Langfuse; the status it answered with."""
    import httpx

    async with httpx.AsyncClient(timeout=EXPORT_TIMEOUT) as client:
        response = await client.post(
            otlp_endpoint(), content=body,
            headers={**otlp_headers(), 'Content-Type': 'application/x-protobuf'})
    if response.status_code >= 300:
        logger.warning('Langfuse refused the Pi session\'s observations (HTTP %s)',
                       response.status_code)
    return response.status_code
