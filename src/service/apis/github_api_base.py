# coding: utf-8

from typing import ClassVar, Dict, List, Tuple  # noqa: F401

from pydantic import Field, StrictInt, StrictStr
from typing import Any, Dict, Optional
from typing_extensions import Annotated
from service.models.branch_page import BranchPage
from service.models.error import Error
from service.models.git_hub_app_credentials import GitHubAppCredentials
from service.models.git_hub_connection import GitHubConnection
from service.models.manifest_code import ManifestCode
from service.models.repository_page import RepositoryPage
from service.security_api import get_token_bearer

class BaseGithubApi:
    subclasses: ClassVar[Tuple] = ()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        BaseGithubApi.subclasses = BaseGithubApi.subclasses + (cls,)
    async def get_connectors_github_connection(
        self,
    ) -> GitHubConnection:
        """The shared connection, without its private key."""
        ...


    async def put_connectors_github_connection(
        self,
        git_hub_app_credentials: GitHubAppCredentials,
    ) -> GitHubConnection:
        """Saves the app after GitHub confirms its key and installation. The request must be application/json and come from an allowed origin, if any."""
        ...


    async def delete_connectors_github_connection(
        self,
        body: Dict[str, Any],
    ) -> None:
        """Removes the shared connection. The body is an empty JSON object."""
        ...


    async def post_connectors_github_connection_check(
        self,
        body: Dict[str, Any],
    ) -> GitHubConnection:
        """Validates the saved app against GitHub and picks up its installation. The body is an empty JSON object."""
        ...


    async def post_connectors_github_connection_manifest(
        self,
        manifest_code: ManifestCode,
    ) -> GitHubConnection:
        """Saves the app GitHub created from this instance&#39;s manifest."""
        ...


    async def get_connectors_github_repositories(
        self,
        page: Annotated[Optional[StrictInt], Field(description="Page number, 1 to 10000; 1 by default.")],
    ) -> RepositoryPage:
        """Repositories the installation can access."""
        ...


    async def get_connectors_github_repositories_by_owner_by_repo_branches(
        self,
        owner: Annotated[StrictStr, Field(description="Repository owner.")],
        repo: Annotated[StrictStr, Field(description="Repository name.")],
        page: Annotated[Optional[StrictInt], Field(description="Page number, 1 to 10000; 1 by default.")],
    ) -> BranchPage:
        """Branch names of a repository the installation can access."""
        ...
