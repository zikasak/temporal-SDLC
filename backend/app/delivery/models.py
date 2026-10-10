"""What the delivery pipeline is asked to build and how far it has come."""
from dataclasses import dataclass, field
from typing import Any, Literal

from app.api_design.models import ApiModel
from app.diagnosis.models import RepairRound
from app.github.models import (
    PublicationStatus, RepositoryCheckout, RepositoryPublication, RepositorySelection,
)
from app.requirements.models import Requirement
from app.verification.models import VerificationResult, VerificationStatus

RetryStage = Literal['server', 'tests', 'repository']
RETRY_STAGES = ('server', 'tests', 'repository')


def delivery_id(session_id: str) -> str:
    """The workflow that delivers a session's task; manual retries are new runs of it."""
    return f'delivery-{session_id}'


@dataclass
class TaskSpec:
    """The task one delivery builds, as the session that specified it left it."""

    # Names the artifacts, the repository checkout and the published branch.
    session_id: str
    requirements: list[Requirement]
    requirements_document: str
    api: ApiModel
    # The stored OpenAPI document the server code is generated from.
    document_id: str
    # Missing when the session expired before a language was chosen.
    language: str | None = None
    repository: RepositorySelection | None = None
    checkout: RepositoryCheckout | None = None


@dataclass
class DeliveryProgress:
    """Each stage's result or error, as the session API reports them."""

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
    # The new branch holding the sources that passed verification.
    publication: RepositoryPublication | None = None
    publication_error: str | None = None
    publication_status: PublicationStatus = 'not_applicable'


@dataclass
class DeliveryState:
    """Workflow input and query result; a manual retry starts a new run from it."""

    spec: TaskSpec
    progress: DeliveryProgress = field(default_factory=DeliveryProgress)
    retry_stage: RetryStage | None = None
