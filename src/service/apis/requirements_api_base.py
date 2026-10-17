# coding: utf-8

from typing import ClassVar, Dict, List, Tuple  # noqa: F401

from pydantic import Field, StrictStr
from typing import Any
from typing_extensions import Annotated
from service.models.agent_answers_input import AgentAnswersInput
from service.models.answers_input import AnswersInput
from service.models.create_requirements_session import CreateRequirementsSession
from service.models.error import Error
from service.models.findings_instructions_input import FindingsInstructionsInput
from service.models.fork_input import ForkInput
from service.models.intake import Intake
from service.models.message_input import MessageInput
from service.models.requirements_session import RequirementsSession
from service.models.retry_stage_input import RetryStageInput
from service.models.session_created import SessionCreated
from service.models.session_status_change import SessionStatusChange
from service.security_api import get_token_bearer

class BaseRequirementsApi:
    subclasses: ClassVar[Tuple] = ()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        BaseRequirementsApi.subclasses = BaseRequirementsApi.subclasses + (cls,)
    async def get_requirements_intake(
        self,
    ) -> Intake:
        """The questions a requirements session starts with."""
        ...


    async def post_requirements_sessions(
        self,
        create_requirements_session: CreateRequirementsSession,
    ) -> SessionCreated:
        """Starts the interview in the repository from the intake&#39;s answers."""
        ...


    async def get_requirements_sessions_by_session_id(
        self,
        session_id: Annotated[StrictStr, Field(description="requirements-session- followed by 32 hex digits.")],
    ) -> RequirementsSession:
        """The session&#39;s conversation, state and delivery progress."""
        ...


    async def post_requirements_sessions_by_session_id_answers(
        self,
        session_id: Annotated[StrictStr, Field(description="requirements-session- followed by 32 hex digits.")],
        answers_input: AnswersInput,
    ) -> SessionStatusChange:
        """Answers the questions of the last assistant message and starts a turn."""
        ...


    async def post_requirements_sessions_by_session_id_checkout(
        self,
        session_id: Annotated[StrictStr, Field(description="requirements-session- followed by 32 hex digits.")],
    ) -> SessionStatusChange:
        """Checks the session&#39;s branch out again at its latest commit; the session is preparing until it is. Refused while a message is unanswered or once the session is finalized."""
        ...


    async def post_requirements_sessions_by_session_id_delivery_answers(
        self,
        session_id: Annotated[StrictStr, Field(description="requirements-session- followed by 32 hex digits.")],
        agent_answers_input: AgentAnswersInput,
    ) -> SessionCreated:
        """Resumes the paused coding agent of the named stage workflow."""
        ...


    async def post_requirements_sessions_by_session_id_delivery_findings_accept(
        self,
        session_id: Annotated[StrictStr, Field(description="requirements-session- followed by 32 hex digits.")],
    ) -> SessionStatusChange:
        """Clears the open-findings flag, keeps the pushed delivery as it is and records findings_acceptance (when and by whom) in the delivery progress. The publication stays marked as unverified. 409 while no flag is raised."""
        ...


    async def post_requirements_sessions_by_session_id_delivery_findings_instructions(
        self,
        session_id: Annotated[StrictStr, Field(description="requirements-session- followed by 32 hex digits.")],
        findings_instructions_input: FindingsInstructionsInput,
    ) -> SessionStatusChange:
        """Starts a fix round from the instructions, then verification and code review, pushing the result to the same branch. The instructions are recorded on the review round they start. If verification fails for good, verification_status becomes error, the branch keeps the earlier unverified push and the flag stays raised. 409 while no flag is raised."""
        ...


    async def get_requirements_sessions_by_session_id_document(
        self,
        session_id: Annotated[StrictStr, Field(description="requirements-session- followed by 32 hex digits.")],
    ) -> None:
        """The requirements document as a file attachment; 404 until it is ready."""
        ...


    async def post_requirements_sessions_by_session_id_finalize(
        self,
        session_id: Annotated[StrictStr, Field(description="requirements-session- followed by 32 hex digits.")],
    ) -> SessionStatusChange:
        """Writes the requirements document; the session is completed once it is ready."""
        ...


    async def post_requirements_sessions_by_session_id_fork(
        self,
        session_id: Annotated[StrictStr, Field(description="requirements-session- followed by 32 hex digits.")],
        fork_input: ForkInput,
    ) -> SessionCreated:
        """Starts a new session with the conversation up to the given message."""
        ...


    async def post_requirements_sessions_by_session_id_generation_retry(
        self,
        session_id: Annotated[StrictStr, Field(description="requirements-session- followed by 32 hex digits.")],
        retry_stage_input: RetryStageInput,
    ) -> SessionStatusChange:
        """Starts the named stage of the delivery over. The review stage runs a failed code review again; 409 when the delivery has no failed review."""
        ...


    async def post_requirements_sessions_by_session_id_messages(
        self,
        session_id: Annotated[StrictStr, Field(description="requirements-session- followed by 32 hex digits.")],
        message_input: MessageInput,
    ) -> SessionStatusChange:
        """Starts the interviewer&#39;s turn on the message."""
        ...


    async def post_requirements_sessions_by_session_id_retry(
        self,
        session_id: Annotated[StrictStr, Field(description="requirements-session- followed by 32 hex digits.")],
    ) -> SessionStatusChange:
        """Runs the failed interviewer turn again."""
        ...
