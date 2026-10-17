# coding: utf-8

from typing import ClassVar, Dict, List, Tuple  # noqa: F401

from pydantic import Field, StrictStr
from typing import Any
from typing_extensions import Annotated
from service.models.error import Error
from service.models.fork_input import ForkInput
from service.models.llm_request import LLMRequest
from service.models.llm_request_input import LLMRequestInput
from service.models.llm_request_started import LLMRequestStarted
from service.security_api import get_token_bearer

class BaseChatApi:
    subclasses: ClassVar[Tuple] = ()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        BaseChatApi.subclasses = BaseChatApi.subclasses + (cls,)
    async def post_llm_requests(
        self,
        llm_request_input: LLMRequestInput,
    ) -> LLMRequestStarted:
        """JSON, or multipart form data with at most one file."""
        ...


    async def get_llm_requests_by_request_id(
        self,
        request_id: Annotated[StrictStr, Field(description="llm-request- followed by 32 hex digits.")],
    ) -> LLMRequest:
        """The request&#39;s status and conversation."""
        ...


    async def get_llm_requests_by_request_id_documents_by_document_id(
        self,
        request_id: Annotated[StrictStr, Field(description="llm-request- followed by 32 hex digits.")],
        document_id: Annotated[StrictStr, Field(description="32 hex digits.")],
    ) -> None:
        """An attachment of the conversation or a document it generated, as a file."""
        ...


    async def post_llm_requests_by_request_id_fork(
        self,
        request_id: Annotated[StrictStr, Field(description="llm-request- followed by 32 hex digits.")],
        fork_input: ForkInput,
    ) -> LLMRequestStarted:
        """Branches after one of the user&#39;s messages and answers it again."""
        ...


    async def post_llm_requests_by_request_id_retry(
        self,
        request_id: Annotated[StrictStr, Field(description="llm-request- followed by 32 hex digits.")],
    ) -> LLMRequestStarted:
        """Answers the failed request again."""
        ...
