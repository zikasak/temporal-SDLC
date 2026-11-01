from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired, TypedDict

from app.conversations.models import ConversationMessage, ForkOrigin
from app.github.models import RepositoryCheckout, RepositorySelection
from app.requirements.models import Requirement, SessionStatus, TaskKind

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
ParameterLocation = Literal["path", "query", "header"]
AuthScheme = Literal["none", "bearer", "api_key", "basic"]

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
PARAMETER_LOCATIONS = ("path", "query", "header")
AUTH_SCHEMES = ("none", "bearer", "api_key", "basic")
PRIMITIVE_TYPES = ("string", "integer", "number", "boolean", "object")


class Property(TypedDict):
    name: str
    # A primitive type or the name of another schema.
    type: str
    required: bool
    array: bool
    format: NotRequired[str]
    enum: NotRequired[list[str]]
    description: NotRequired[str]


class Schema(TypedDict):
    name: str
    description: str
    properties: list[Property]


class Parameter(TypedDict):
    name: str
    location: ParameterLocation
    type: str
    required: bool
    description: NotRequired[str]


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


class ApiInfo(TypedDict):
    title: str
    version: str
    description: str
    auth: AuthScheme


class ApiModel(TypedDict):
    info: ApiInfo
    schemas: list[Schema]
    endpoints: list[Endpoint]


def empty_api() -> ApiModel:
    return {
        "info": {"title": "API", "version": "1.0.0", "description": "", "auth": "none"},
        "schemas": [],
        "endpoints": [],
    }


@dataclass
class ApiDesignState:
    """Workflow input and query result; carried over by continue-as-new."""

    source_session_id: str = ""
    requirements: list[Requirement] = field(default_factory=list)
    requirements_document: str = ""
    # The requirements document of a change to an existing service. The fields above are
    # then that service's contract with the change's requirements added, and `api` and
    # `language` start as the contract's.
    change_document: str = ""
    # The contract that change was specified against, and the commit it was last seen
    # unchanged at. A session clones the repository for itself, so both are compared
    # with what its own checkout holds before the discussion starts.
    contract: dict[str, Any] | None = None
    contract_commit: str | None = None
    # The kind of task the requirements session recorded.
    kind: TaskKind | None = None
    messages: list[ConversationMessage] = field(default_factory=list)
    api: ApiModel = field(default_factory=empty_api)
    # Name of the registered language the server code is generated in, once chosen.
    language: str | None = None
    status: SessionStatus = "interviewing"
    document: dict[str, Any] | None = None
    error: str | None = None
    forked_from: ForkOrigin | None = None
    repository: RepositorySelection | None = None
    # Set once the repository is on the worker's disk; forks check out their own.
    checkout: RepositoryCheckout | None = None
    # The architecture session whose finalizing or skip started this design, and the
    # architecture it hands on, as Markdown and in machine-readable form: the finalized
    # one, or the repository's existing one when an API change skipped the stage.
    architecture_session_id: str | None = None
    architecture: str | None = None
    architecture_structure: dict[str, Any] | None = None
