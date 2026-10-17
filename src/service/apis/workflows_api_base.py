# coding: utf-8

from typing import ClassVar, Dict, List, Tuple  # noqa: F401

from pydantic import Field, StrictStr
from typing import Optional
from typing_extensions import Annotated
from service.models.error import Error
from service.models.workflow_page import WorkflowPage
from service.models.workflow_result import WorkflowResult
from service.security_api import get_token_bearer

class BaseWorkflowsApi:
    subclasses: ClassVar[Tuple] = ()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        BaseWorkflowsApi.subclasses = BaseWorkflowsApi.subclasses + (cls,)
    async def post_start(
        self,
    ) -> WorkflowResult:
        """Runs the sample greeting workflow and returns its result."""
        ...


    async def get_workflows(
        self,
        type: Annotated[Optional[StrictStr], Field(description="A workflow type to list only.")],
        page_token: Annotated[Optional[StrictStr], Field(description="next_page_token of the last page.")],
    ) -> WorkflowPage:
        """Newest first, 50 per page."""
        ...
