# coding: utf-8

from typing import ClassVar, Dict, List, Tuple  # noqa: F401

from typing import Any, List
from service.models.account import Account
from service.models.credentials import Credentials
from service.models.error import Error
from service.models.profile import Profile
from service.models.registration_status import RegistrationStatus
from service.models.session import Session
from service.security_api import get_token_bearer

class BaseAuthApi:
    subclasses: ClassVar[Tuple] = ()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        BaseAuthApi.subclasses = BaseAuthApi.subclasses + (cls,)
    async def post_auth_login(
        self,
        credentials: Credentials,
    ) -> Session:
        """No sign-in required. Returns a session and sets the access_token cookie."""
        ...


    async def post_auth_logout(
        self,
    ) -> None:
        """No sign-in required. Clears the session cookie."""
        ...


    async def get_auth_me(
        self,
    ) -> Profile:
        """The signed-in user."""
        ...


    async def post_auth_register(
        self,
        credentials: Credentials,
    ) -> Session:
        """No sign-in required. Registers and signs in the first account; 403 once any account exists."""
        ...


    async def get_auth_registration(
        self,
    ) -> RegistrationStatus:
        """No sign-in required. Whether the first account can still be registered."""
        ...


    async def get_auth_users(
        self,
    ) -> List[Account]:
        """Administrator only."""
        ...


    async def post_auth_users(
        self,
        credentials: Credentials,
    ) -> Account:
        """Administrator only."""
        ...
