from abc import ABC, abstractmethod
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    Generic,
    List,
    Literal,
    Optional,
    Type,
    TypeVar,
    Union,
    cast,
)

from pydantic import BaseConfig, BaseModel, PrivateAttr, conint, constr

from starlite.connection import ASGIConnection
from starlite.enums import ScopeType
from starlite.middleware.base import AbstractMiddleware, DefineMiddleware
from starlite.types import Scopes
from starlite.utils import get_serializer_from_scope
from starlite.utils.serialization import decode_json, encode_json

if TYPE_CHECKING:
    from starlite.types import ASGIApp, Message, Receive, Scope, ScopeSession, Send

ONE_DAY_IN_SECONDS = 60 * 60 * 24

ConfigT = TypeVar("ConfigT", bound="BaseBackendConfig")
BaseSessionBackendT = TypeVar("BaseSessionBackendT", bound="BaseSessionBackend")


class BaseBackendConfig(BaseModel):
    """Configuration for Session middleware backends."""

    class Config(BaseConfig):
        arbitrary_types_allowed = True

    _backend_class: Type["BaseSessionBackend"] = PrivateAttr()

    key: constr(min_length=1, max_length=256) = "session"  # type: ignore[valid-type]
    """Key to use for the cookie inside the header, e.g. ``session=<data>`` where ``session`` is the cookie key and
    ``<data>`` is the session data.

    Notes:
        - If a session cookie exceeds 4KB in size it is split. In this case the key will be of the format
          ``session-{segment number}``.

    """
    max_age: conint(ge=1) = ONE_DAY_IN_SECONDS * 14  # type: ignore[valid-type]
    """Maximal age of the cookie before its invalidated."""
    scopes: Scopes = {ScopeType.HTTP, ScopeType.WEBSOCKET}
    """Scopes for the middleware - options are ``http`` and ``websocket`` with the default being both"""
    path: str = "/"
    """Path fragment that must exist in the request url for the cookie to be valid.

    Defaults to ``'/'``.
    """
    domain: Optional[str] = None
    """Domain for which the cookie is valid."""
    secure: bool = False
    """Https is required for the cookie."""
    httponly: bool = True
    """Forbids javascript to access the cookie via 'Document.cookie'."""
    samesite: Literal["lax", "strict", "none"] = "lax"
    """Controls whether or not a cookie is sent with cross-site requests.

    Defaults to ``lax``.
    """
    exclude: Optional[Union[str, List[str]]] = None
    """A pattern or list of patterns to skip in the session middleware."""
    exclude_opt_key: str = "skip_session"
    """An identifier to use on routes to disable the session middleware for a particular route."""

    @property
    def middleware(self) -> DefineMiddleware:
        """Use this property to insert the config into a middleware list on one of the application layers.

        Examples:
            .. code-block: python

                from os import urandom

                from starlite import Starlite, Request, get
                from starlite.middleware.sessions.cookie_backend import CookieBackendConfig

                session_config = CookieBackendConfig(secret=urandom(16))


                @get("/")
                def my_handler(request: Request) -> None:
                    ...


                app = Starlite(route_handlers=[my_handler], middleware=[session_config.middleware])


        Returns:
            An instance of DefineMiddleware including ``self`` as the config kwarg value.
        """
        return DefineMiddleware(SessionMiddleware, backend=self._backend_class(config=self))


class BaseSessionBackend(ABC, Generic[ConfigT]):
    """Abstract session backend defining the interface between a storage mechanism and the application
    :class:`SessionMiddleware`.

    This serves as the base class for all client- and server-side backends
    """

    __slots__ = ("config",)

    def __init__(self, config: ConfigT) -> None:
        """Initialize ``BaseSessionBackend``

        Args:
            config: A instance of a subclass of ``BaseBackendConfig``
        """
        self.config = config

    @staticmethod
    def serialize_data(data: "ScopeSession", scope: Optional["Scope"] = None) -> bytes:
        """Serialize data into bytes for storage in the backend.

        Args:
            data: Session data of the current scope.
            scope: A scope, if applicable, from which to extract a serializer.

        Notes:
            - The serializer will be extracted from ``scope`` or fall back
              to :func:`default_serializer <starlite.utils.default_serializer>`

        Returns:
            ``data`` serialized as bytes.
        """
        serializer = get_serializer_from_scope(scope) if scope else None
        return encode_json(data, serializer)

    @staticmethod
    def deserialize_data(data: Any) -> Dict[str, Any]:
        """Deserialize data into a dictionary for use in the application scope.

        Args:
            data: Data to be deserialized

        Returns:
            Deserialized data as a dictionary
        """
        return cast("Dict[str, Any]", decode_json(data))

    @abstractmethod
    async def store_in_message(
        self, scope_session: "ScopeSession", message: "Message", connection: ASGIConnection
    ) -> None:
        """Store the necessary information in the outgoing ``Message``

        Args:
            scope_session: Current session to store
            message: Outgoing send-message
            connection: Originating ASGIConnection containing the scope

        Returns:
            None
        """

    @abstractmethod
    async def load_from_connection(self, connection: ASGIConnection) -> Dict[str, Any]:
        """Load session data from a connection and return it as a dictionary to be used in the current application
        scope.

        Args:
            connection: An ASGIConnection instance

        Returns:
            The session data

        Notes:
            - This should not modify the connection's scope. The data returned by this
              method will be stored in the application scope by the middleware

        """


class SessionMiddleware(AbstractMiddleware, Generic[BaseSessionBackendT]):
    """Starlite session middleware for storing session data."""

    def __init__(self, app: "ASGIApp", backend: BaseSessionBackendT) -> None:
        """Initialize ``SessionMiddleware``

        Args:
            app: An ASGI application
            backend: A :class:`BaseSessionBackend` instance used to store and retrieve session data
        """

        super().__init__(
            app=app,
            exclude=backend.config.exclude,
            exclude_opt_key=backend.config.exclude_opt_key,
            scopes=backend.config.scopes,
        )
        self.backend = backend

    def create_send_wrapper(self, connection: ASGIConnection) -> Callable[["Message"], Awaitable[None]]:
        """Create a wrapper for the ASGI send function, which handles setting the cookies on the outgoing response.

        Args:
            connection: ASGIConnection

        Returns:
            None
        """

        async def wrapped_send(message: "Message") -> None:
            """Wrap the ``send`` function.

            Declared in local scope to make use of closure values.

            Args:
                message: An ASGI message.

            Returns:
                None
            """
            if message["type"] != "http.response.start":
                await connection.send(message)
                return

            scope_session = connection.scope.get("session")

            await self.backend.store_in_message(scope_session, message, connection)
            await connection.send(message)

        return wrapped_send

    async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
        """ASGI-callable.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive function.
            send: The ASGI send function.

        Returns:
            None
        """

        connection = ASGIConnection[Any, Any, Any, Any](scope, receive=receive, send=send)
        scope["session"] = await self.backend.load_from_connection(connection)

        await self.app(scope, receive, self.create_send_wrapper(connection))
