from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired, TypedDict

from app.conversations.models import ConversationMessage, ForkOrigin
from app.diagnosis.models import RepairRound
from app.requirements.models import Requirement, SessionStatus
from app.verification.models import VerificationResult, VerificationStatus

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
    messages: list[ConversationMessage] = field(default_factory=list)
    api: ApiModel = field(default_factory=empty_api)
    status: SessionStatus = "interviewing"
    document: dict[str, Any] | None = None
    error: str | None = None
    # Where CodegenWorkflow wrote the generated code, once it has run.
    code: dict[str, Any] | None = None
    code_error: str | None = None
    implementation: dict[str, Any] | None = None
    implementation_error: str | None = None
    tests: dict[str, Any] | None = None
    tests_error: str | None = None
    verification: VerificationResult | None = None
    verification_error: str | None = None
    verification_status: VerificationStatus = 'not_applicable'
    # One entry per diagnosed verification failure: which side was sent back for repair.
    repairs: list[RepairRound] = field(default_factory=list)
    forked_from: ForkOrigin | None = None
    # Manual generation retries start a new run under the same session ID.
    generation_retry_stage: Literal['server', 'tests'] | None = None
    previous_run_id: str | None = None
