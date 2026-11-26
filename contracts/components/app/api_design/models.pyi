from dataclasses import dataclass
from typing import Any


@dataclass
class ApiDesignState:
    """An API design session of the requirements session `source_session_id`, the root
    of its flow. `messages` is the discussion (a session with no messages first drafts
    the API with an LLM turn); `status` is 'preparing', 'interviewing', 'turn_running',
    'finalizing', 'completed' or 'failed'; `document` is the finalized OpenAPI document
    ({'document_id', ...}), which a completed session hands to its delivery; `repository`
    and `checkout` as for a requirements session."""
    source_session_id: str = ...
    requirements: list[Any] = ...
    requirements_document: str = ...
    change_document: str = ...
    contract: dict[str, Any] | None = ...
    contract_commit: str | None = ...
    kind: str | None = ...
    messages: list[Any] = ...
    api: Any = ...
    language: str | None = ...
    status: str = ...
    document: dict[str, Any] | None = ...
    error: str | None = ...
    forked_from: Any | None = ...
    repository: Any | None = ...
    checkout: Any | None = ...
    architecture_session_id: str | None = ...
    architecture: str | None = ...
    architecture_structure: dict[str, Any] | None = ...
    # The task flow the session belongs to; `pending` while the flow has not started it.
    task_flow_id: str | None = ...
    pending: bool = ...

from typing import Literal
from typing import NotRequired
from typing import TypedDict
class ApiModel(TypedDict):
    info: ApiInfo
    schemas: list[Schema]
    endpoints: list[Endpoint]
class ApiInfo(TypedDict):
    title: str
    version: str
    description: str
    auth: AuthScheme
class Schema(TypedDict):
    name: str
    description: str
    properties: list[Property]
class Endpoint(TypedDict):
    method: HttpMethod
    path: str
    summary: str
    description: str
    tag: str
    parameters: list[Parameter]
    request_schema: str | None
    response_status: int
    response_schema: str | None
    response_array: bool
    error_statuses: list[int]
    requirement_ids: list[str]
AuthScheme = Literal['none', 'bearer', 'api_key', 'basic']
class Property(TypedDict):
    name: str
    type: str
    required: bool
    array: bool
    format: NotRequired[str]
    enum: NotRequired[list[str]]
    description: NotRequired[str]
HttpMethod = Literal['GET', 'POST', 'PUT', 'PATCH', 'DELETE']
class Parameter(TypedDict):
    name: str
    location: ParameterLocation
    type: str
    required: bool
    description: NotRequired[str]
ParameterLocation = Literal['path', 'query', 'header']
