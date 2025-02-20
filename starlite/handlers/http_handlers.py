from __future__ import annotations

from enum import Enum
from functools import lru_cache
from inspect import Signature, isawaitable
from typing import (
    TYPE_CHECKING,
    Any,
    AnyStr,
    Awaitable,
    Callable,
    Mapping,
    Sequence,
    cast,
)

from typing_extensions import get_args

from starlite.constants import REDIRECT_STATUS_CODES
from starlite.datastructures import CacheControlHeader, Cookie, ETag, ResponseHeader
from starlite.dto import DTO
from starlite.enums import HttpMethod, MediaType
from starlite.exceptions import (
    HTTPException,
    ImproperlyConfiguredException,
    ValidationException,
)
from starlite.handlers.base import BaseRouteHandler
from starlite.plugins.base import get_plugin_for_value
from starlite.response import FileResponse, Response
from starlite.response_containers import File, Redirect, ResponseContainer
from starlite.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_204_NO_CONTENT,
    HTTP_304_NOT_MODIFIED,
)
from starlite.types import (
    AfterRequestHookHandler,
    AfterResponseHookHandler,
    ASGIApp,
    BeforeRequestHookHandler,
    CacheKeyBuilder,
    Empty,
    EmptyType,
    ExceptionHandlersMap,
    Guard,
    Method,
    Middleware,
    ResponseCookies,
    ResponseType,
    TypeEncodersMap,
)
from starlite.utils import Ref, annotation_is_iterable_of_type, is_async_callable
from starlite.utils.predicates import is_class_and_subclass
from starlite.utils.sync import AsyncCallable

from .utils import narrow_response_cookies, narrow_response_headers

if TYPE_CHECKING:
    from pydantic_openapi_schema.v3_1_0 import SecurityRequirement

    from starlite.app import Starlite
    from starlite.background_tasks import BackgroundTask, BackgroundTasks
    from starlite.connection import Request
    from starlite.datastructures.headers import Header
    from starlite.di import Provide
    from starlite.openapi.datastructures import ResponseSpec
    from starlite.plugins import SerializationPluginProtocol
    from starlite.types import MaybePartial  # nopycln: import # noqa: F401
    from starlite.types import AnyCallable, AsyncAnyCallable
    from starlite.types.composite_types import ResponseHeaders

MSG_SEMANTIC_ROUTE_HANDLER_WITH_HTTP = "semantic route handlers cannot define http_method"

HTTP_METHOD_NAMES = {m.value for m in HttpMethod}


@lru_cache(1024)
def _filter_cookies(local_cookies: frozenset[Cookie], layered_cookies: frozenset[Cookie]) -> list[Cookie]:
    """Given two sets of cookies, return a unique list of cookies, that are not marked as documentation_only."""
    return [cookie for cookie in {*local_cookies, *layered_cookies} if not cookie.documentation_only]


@lru_cache(1024)
def _normalize_headers(headers: frozenset[ResponseHeader]) -> dict[str, str]:
    """Given a dictionary of ResponseHeader, filter them and return a dictionary of values.

    Args:
        headers: A dictionary of :class:`ResponseHeader <starlite.datastructures.ResponseHeader>` values

    Returns:
        A string keyed dictionary of normalized values
    """
    return {
        header.name: cast("str", header.value)  # we know value to be a string at this point because we validate it
        # that it's not None when initializing a header with documentation_only=True
        for header in headers
        if not header.documentation_only
    }


async def _normalize_response_data(data: Any, plugins: list["SerializationPluginProtocol"]) -> Any:
    """Normalize the response's data by awaiting any async values and resolving plugins.

    Args:
        data: An arbitrary value
        plugins: A list of :class:`plugins <starlite.plugins.base.SerializationPluginProtocol>`

    Returns:
        Value for the response body
    """

    plugin = get_plugin_for_value(value=data, plugins=plugins)
    if not plugin:
        return data

    if is_async_callable(plugin.to_dict):
        if isinstance(data, (list, tuple)):
            return [await plugin.to_dict(datum) for datum in data]
        return await plugin.to_dict(data)

    if isinstance(data, (list, tuple)):
        return [plugin.to_dict(datum) for datum in data]
    return plugin.to_dict(data)


def _create_response_container_handler(
    after_request: AfterRequestHookHandler | None,
    cookies: frozenset[Cookie],
    headers: frozenset[ResponseHeader],
    media_type: str,
    status_code: int,
) -> AsyncAnyCallable:
    """Create a handler function for ResponseContainers."""
    normalized_headers = _normalize_headers(headers)

    async def handler(data: ResponseContainer, app: "Starlite", request: "Request", **kwargs: Any) -> "ASGIApp":
        response = data.to_response(
            app=app,
            headers={**normalized_headers, **data.headers},
            status_code=status_code,
            media_type=data.media_type or media_type,
            request=request,
        )
        response.cookies = _filter_cookies(frozenset(data.cookies), cookies)
        return await after_request(response) if after_request else response  # type: ignore

    return handler


def _create_response_handler(
    after_request: AfterRequestHookHandler | None,
    cookies: frozenset[Cookie],
) -> AsyncAnyCallable:
    """Create a handler function for Starlite Responses."""

    async def handler(data: Response, **kwargs: Any) -> "ASGIApp":
        data.cookies = _filter_cookies(frozenset(data.cookies), cookies)
        return await after_request(data) if after_request else data  # type: ignore

    return handler


def _create_generic_asgi_response_handler(
    after_request: AfterRequestHookHandler | None,
    cookies: frozenset[Cookie],
) -> AsyncAnyCallable:
    """Create a handler function for Responses."""

    async def handler(data: "ASGIApp", **kwargs: Any) -> "ASGIApp":
        if hasattr(data, "set_cookie"):
            for cookie in cookies:
                data.set_cookie(**cookie.dict)
        return await after_request(data) if after_request else data  # type: ignore

    return handler


def _create_data_handler(
    after_request: AfterRequestHookHandler | None,
    background: BackgroundTask | BackgroundTasks | None,
    cookies: frozenset[Cookie],
    headers: frozenset[ResponseHeader],
    media_type: str,
    response_class: ResponseType,
    return_annotation: Any,
    status_code: int,
    type_encoders: TypeEncodersMap | None,
) -> AsyncAnyCallable:
    """Create a handler function for arbitrary data."""
    normalized_headers = [
        (name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in _normalize_headers(headers).items()
    ]
    cookie_headers = [cookie.to_encoded_header() for cookie in cookies if not cookie.documentation_only]
    raw_headers = [*normalized_headers, *cookie_headers]
    is_dto_annotation = is_class_and_subclass(return_annotation, DTO)
    is_dto_iterable_annotation = annotation_is_iterable_of_type(return_annotation, DTO)

    async def create_response(data: Any) -> "ASGIApp":
        response = response_class(
            background=background,
            content=data,
            media_type=media_type,
            status_code=status_code,
            type_encoders=type_encoders,
        )
        response.raw_headers = raw_headers

        if after_request:
            return await after_request(response)  # type: ignore

        return response

    async def handler(data: Any, plugins: list["SerializationPluginProtocol"], **kwargs: Any) -> "ASGIApp":
        if isawaitable(data):
            data = await data

        if is_dto_annotation and not isinstance(data, DTO):
            data = return_annotation(**data) if isinstance(data, dict) else return_annotation.from_model_instance(data)

        elif is_dto_iterable_annotation and data and not isinstance(data[0], DTO):  # pyright: ignore
            dto_type = cast("type[DTO]", get_args(return_annotation)[0])
            data = [
                dto_type(**datum) if isinstance(datum, dict) else dto_type.from_model_instance(datum) for datum in data
            ]

        elif plugins and not (is_dto_annotation or is_dto_iterable_annotation):
            data = await _normalize_response_data(data=data, plugins=plugins)

        return await create_response(data=data)

    return handler


def _normalize_http_method(http_methods: HttpMethod | Method | Sequence[HttpMethod | Method]) -> set[Method]:
    """Normalize HTTP method(s) into a set of upper-case method names.

    Args:
        http_methods: A value for http method.

    Returns:
        A normalized set of http methods.
    """
    output: set[str] = set()

    if isinstance(http_methods, str):
        http_methods = [http_methods]  # pyright: ignore

    for method in http_methods:
        if isinstance(method, HttpMethod):
            method_name = method.value.upper()
        else:
            method_name = method.upper()
        if method_name not in HTTP_METHOD_NAMES:
            raise ValidationException(f"Invalid HTTP method: {method_name}")
        output.add(method_name)

    return cast("set[Method]", output)


def _get_default_status_code(http_methods: set[Method]) -> int:
    """Return the default status code for a given set of HTTP methods.

    Args:
        http_methods: A set of method strings

    Returns:
        A status code
    """
    if HttpMethod.POST in http_methods:
        return HTTP_201_CREATED
    if HttpMethod.DELETE in http_methods:
        return HTTP_204_NO_CONTENT
    return HTTP_200_OK


class HTTPRouteHandler(BaseRouteHandler["HTTPRouteHandler"]):
    """HTTP Route Decorator.

    Use this decorator to decorate an HTTP handler with multiple methods.
    """

    __slots__ = (
        "_resolved_after_response",
        "_resolved_before_request",
        "_resolved_response_handler",
        "after_request",
        "after_response",
        "background",
        "before_request",
        "cache",
        "cache_control",
        "cache_key_builder",
        "content_encoding",
        "content_media_type",
        "deprecated",
        "description",
        "etag",
        "has_sync_callable",
        "http_methods",
        "include_in_schema",
        "media_type",
        "operation_id",
        "raises",
        "response_class",
        "response_cookies",
        "response_description",
        "response_headers",
        "responses",
        "security",
        "status_code",
        "summary",
        "sync_to_thread",
        "tags",
        "template_name",
    )

    has_sync_callable: bool

    def __init__(
        self,
        path: str | Sequence[str] | None = None,
        *,
        after_request: AfterRequestHookHandler | None = None,
        after_response: AfterResponseHookHandler | None = None,
        background: BackgroundTask | BackgroundTasks | None = None,
        before_request: BeforeRequestHookHandler | None = None,
        cache: bool | int = False,
        cache_control: CacheControlHeader | None = None,
        cache_key_builder: CacheKeyBuilder | None = None,
        dependencies: Mapping[str, Provide] | None = None,
        etag: ETag | None = None,
        exception_handlers: ExceptionHandlersMap | None = None,
        guards: Sequence[Guard] | None = None,
        http_method: HttpMethod | Method | Sequence[HttpMethod | Method],
        media_type: MediaType | str | None = None,
        middleware: Sequence[Middleware] | None = None,
        name: str | None = None,
        opt: Mapping[str, Any] | None = None,
        response_class: ResponseType | None = None,
        response_cookies: ResponseCookies | None = None,
        response_headers: ResponseHeaders | None = None,
        status_code: int | None = None,
        sync_to_thread: bool = False,
        # OpenAPI related attributes
        content_encoding: str | None = None,
        content_media_type: str | None = None,
        deprecated: bool = False,
        description: str | None = None,
        include_in_schema: bool = True,
        operation_id: str | None = None,
        raises: Sequence[type[HTTPException]] | None = None,
        response_description: str | None = None,
        responses: Mapping[int, ResponseSpec] | None = None,
        security: Sequence[SecurityRequirement] | None = None,
        summary: str | None = None,
        tags: Sequence[str] | None = None,
        type_encoders: TypeEncodersMap | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize ``HTTPRouteHandler``.

        Args:
            path: A path fragment for the route handler function or a sequence of path fragments.
                If not given defaults to ``'/'``
            after_request: A sync or async function executed before a :class:`Request <starlite.connection.Request>` is passed
                to any route handler. If this function returns a value, the request will not reach the route handler,
                and instead this value will be used.
            after_response: A sync or async function called after the response has been awaited. It receives the
                :class:`Request <starlite.connection.Request>` object and should not return any values.
            background: A :class:`BackgroundTask <starlite.datastructures.BackgroundTask>` instance or
                :class:`BackgroundTasks <starlite.datastructures.BackgroundTasks>` to execute after the response is finished.
                Defaults to ``None``.
            before_request: A sync or async function called immediately before calling the route handler. Receives
                the `starlite.connection.Request`` instance and any non-``None`` return value is used for the response,
                bypassing the route handler.
            cache: Enables response caching if configured on the application level. Valid values are ``True`` or a number
                of seconds (e.g. ``120``) to cache the response.
            cache_control: A ``cache-control`` header of type
                :class:`CacheControlHeader <starlite.datastructures.CacheControlHeader>` that will be added to the response.
            cache_key_builder: A :class:`cache-key builder function <starlite.types.CacheKeyBuilder>`. Allows for customization
                of the cache key if caching is configured on the application level.
            dependencies: A string keyed mapping of dependency :class:`Provider <starlite.datastructures.Provide>` instances.
            etag: An ``etag`` header of type :class:`ETag <starlite.datastructures.ETag>` that will be added to the response.
            exception_handlers: A mapping of status codes and/or exception types to handler functions.
            guards: A sequence of :class:`Guard <starlite.types.Guard>` callables.
            http_method: An :class:`http method string <starlite.types.Method>`, a member of the enum
                :class:`HttpMethod <starlite.enums.HttpMethod>` or a list of these that correlates to the methods the
                route handler function should handle.
            media_type: A member of the :class:`MediaType <starlite.enums.MediaType>` enum or a string with a
                valid IANA Media-Type.
            middleware: A sequence of :class:`Middleware <starlite.types.Middleware>`.
            name: A string identifying the route handler.
            opt: A string keyed mapping of arbitrary values that can be accessed in :class:`Guards <starlite.types.Guard>` or
                wherever you have access to :class:`Request <starlite.connection.request.Request>` or :class:`ASGI Scope <starlite.types.Scope>`.
            response_class: A custom subclass of :class:`Response <starlite.response.Response>` to be used as route handler's
                default response.
            response_cookies: A sequence of :class:`Cookie <starlite.datastructures.Cookie>` instances.
            response_headers: A string keyed mapping of :class:`ResponseHeader <starlite.datastructures.ResponseHeader>`
                instances.
            responses: A mapping of additional status codes and a description of their expected content.
                This information will be included in the OpenAPI schema
            status_code: An http status code for the response. Defaults to ``200`` for mixed method or ``GET``, ``PUT`` and
                ``PATCH``, ``201`` for ``POST`` and ``204`` for ``DELETE``.
            sync_to_thread: A boolean dictating whether the handler function will be executed in a worker thread or the
                main event loop. This has an effect only for sync handler functions. See using sync handler functions.
            content_encoding: A string describing the encoding of the content, e.g. ``"base64"``.
            content_media_type: A string designating the media-type of the content, e.g. ``"image/png"``.
            deprecated:  A boolean dictating whether this route should be marked as deprecated in the OpenAPI schema.
            description: Text used for the route's schema description section.
            include_in_schema: A boolean flag dictating whether  the route handler should be documented in the OpenAPI schema.
            operation_id: An identifier used for the route's schema operationId. Defaults to the ``__name__`` of the wrapped function.
            raises:  A list of exception classes extending from starlite.HttpException that is used for the OpenAPI documentation.
                This list should describe all exceptions raised within the route handler's function/method. The Starlite
                ValidationException will be added automatically for the schema if any validation is involved.
            response_description: Text used for the route's response schema description section.
            security: A sequence of dictionaries that contain information about which security scheme can be used on the endpoint.
            summary: Text used for the route's schema summary section.
            tags: A sequence of string tags that will be appended to the OpenAPI schema.
            type_encoders: A mapping of types to callables that transform them into types supported for serialization.
            **kwargs: Any additional kwarg - will be set in the opt dictionary.
        """
        if not http_method:
            raise ImproperlyConfiguredException("An http_method kwarg is required")

        self.http_methods = _normalize_http_method(http_methods=http_method)
        self.status_code = status_code or _get_default_status_code(http_methods=self.http_methods)

        super().__init__(
            path,
            dependencies=dependencies,
            exception_handlers=exception_handlers,
            guards=guards,
            middleware=middleware,
            name=name,
            opt=opt,
            type_encoders=type_encoders,
            **kwargs,
        )

        self.after_request = AsyncCallable(after_request) if after_request else None  # type: ignore[arg-type]
        self.after_response = AsyncCallable(after_response) if after_response else None
        self.background = background
        self.before_request = AsyncCallable(before_request) if before_request else None
        self.cache = cache
        self.cache_control = cache_control
        self.cache_key_builder = cache_key_builder
        self.etag = etag
        self.media_type: MediaType | str = media_type or ""
        self.response_class = response_class

        self.response_cookies: Sequence[Cookie] | None = narrow_response_cookies(response_cookies)
        self.response_headers: Sequence[ResponseHeader] | None = narrow_response_headers(response_headers)

        self.sync_to_thread = sync_to_thread
        # OpenAPI related attributes
        self.content_encoding = content_encoding
        self.content_media_type = content_media_type
        self.deprecated = deprecated
        self.description = description
        self.include_in_schema = include_in_schema
        self.operation_id = operation_id
        self.raises = raises
        self.response_description = response_description
        self.summary = summary
        self.tags = tags
        self.security = security
        self.responses = responses
        # memoized attributes, defaulted to Empty
        self._resolved_after_response: AfterResponseHookHandler | None | EmptyType = Empty
        self._resolved_before_request: BeforeRequestHookHandler | None | EmptyType = Empty
        self._resolved_response_handler: Callable[[Any], Awaitable[ASGIApp]] | EmptyType = Empty

    def __call__(self, fn: AnyCallable) -> HTTPRouteHandler:
        """Replace a function with itself."""
        self.fn = Ref["MaybePartial[AnyCallable]"](fn)
        self.signature = Signature.from_callable(fn)
        self._validate_handler_function()

        if not self.media_type:
            if self.signature.return_annotation in {str, bytes, AnyStr, Redirect, File} or any(
                is_class_and_subclass(self.signature.return_annotation, t_type) for t_type in (str, bytes)  # type: ignore
            ):
                self.media_type = MediaType.TEXT
            else:
                self.media_type = MediaType.JSON

        return self

    def resolve_response_class(self) -> type[Response]:
        """Return the closest custom Response class in the owner graph or the default Response class.

        This method is memoized so the computation occurs only once.

        Returns:
            The default :class:`Response <starlite.response.Response>` class for the route handler.
        """
        for layer in list(reversed(self.ownership_layers)):
            if layer.response_class is not None:
                return layer.response_class
        return Response

    def resolve_response_headers(self) -> frozenset[ResponseHeader]:
        """Return all header parameters in the scope of the handler function.

        Returns:
            A dictionary mapping keys to :class:`ResponseHeader <starlite.datastructures.ResponseHeader>` instances.
        """
        resolved_response_headers: dict[str, ResponseHeader] = {}

        for layer in self.ownership_layers:
            if layer_response_headers := layer.response_headers:
                if isinstance(layer_response_headers, Mapping):
                    # this can't happen unless you manually set response_headers on an instance, which would result in a
                    # type-checking error on everything but the controller. We cover this case nevertheless
                    resolved_response_headers.update(
                        {name: ResponseHeader(name=name, value=value) for name, value in layer_response_headers.items()}
                    )
                else:
                    resolved_response_headers.update({h.name: h for h in layer_response_headers})
            for extra_header in ("cache_control", "etag"):
                header_model: Header | None = getattr(layer, extra_header, None)
                if header_model:
                    resolved_response_headers[header_model.HEADER_NAME] = ResponseHeader(
                        name=header_model.HEADER_NAME,
                        value=header_model.to_header(),
                        documentation_only=header_model.documentation_only,
                    )

        return frozenset(resolved_response_headers.values())

    def resolve_response_cookies(self) -> frozenset[Cookie]:
        """Return a list of Cookie instances. Filters the list to ensure each cookie key is unique.

        Returns:
            A list of :class:`Cookie <starlite.datastructures.Cookie>` instances.
        """
        response_cookies: set[Cookie] = set()
        for layer in reversed(self.ownership_layers):
            if layer_response_cookies := layer.response_cookies:
                if isinstance(layer_response_cookies, Mapping):
                    # this can't happen unless you manually set response_cookies on an instance, which would result in a
                    # type-checking error on everything but the controller. We cover this case nevertheless
                    response_cookies.update(
                        {Cookie(key=key, value=value) for key, value in layer_response_cookies.items()}
                    )
                else:
                    response_cookies.update(layer_response_cookies)
        return frozenset(response_cookies)

    def resolve_before_request(self) -> BeforeRequestHookHandler | None:
        """Resolve the before_handler handler by starting from the route handler and moving up.

        If a handler is found it is returned, otherwise None is set.
        This method is memoized so the computation occurs only once.

        Returns:
            An optional :class:`before request lifecycle hook handler <starlite.types.BeforeRequestHookHandler>`
        """
        if self._resolved_before_request is Empty:
            before_request_handlers: list[AsyncCallable] = [
                layer.before_request for layer in self.ownership_layers if layer.before_request  # type: ignore[misc]
            ]
            self._resolved_before_request = cast(
                "BeforeRequestHookHandler | None",
                before_request_handlers[-1] if before_request_handlers else None,
            )
        return self._resolved_before_request

    def resolve_after_response(self) -> AfterResponseHookHandler | None:
        """Resolve the after_response handler by starting from the route handler and moving up.

        If a handler is found it is returned, otherwise None is set.
        This method is memoized so the computation occurs only once.

        Returns:
            An optional :class:`after response lifecycle hook handler <starlite.types.AfterResponseHookHandler>`
        """
        if self._resolved_after_response is Empty:
            after_response_handlers: list[AsyncCallable] = [
                layer.after_response for layer in self.ownership_layers if layer.after_response  # type: ignore[misc]
            ]
            self._resolved_after_response = cast(
                "AfterResponseHookHandler | None",
                after_response_handlers[-1] if after_response_handlers else None,
            )

        return cast("AfterResponseHookHandler | None", self._resolved_after_response)

    def resolve_response_handler(
        self,
    ) -> Callable[[Any], Awaitable[ASGIApp]]:
        """Resolve the response_handler function for the route handler.

        This method is memoized so the computation occurs only once.

        Returns:
            Async Callable to handle an HTTP Request
        """
        if self._resolved_response_handler is Empty:
            after_request_handlers: list[AsyncCallable] = [
                layer.after_request for layer in self.ownership_layers if layer.after_request  # type: ignore[misc]
            ]
            after_request = cast(
                "AfterRequestHookHandler | None",
                after_request_handlers[-1] if after_request_handlers else None,
            )

            media_type = self.media_type.value if isinstance(self.media_type, Enum) else self.media_type
            response_class = self.resolve_response_class()
            headers = self.resolve_response_headers()
            cookies = self.resolve_response_cookies()
            type_encoders = self.resolve_type_encoders()

            if is_class_and_subclass(self.signature.return_annotation, ResponseContainer):  # type: ignore
                handler = _create_response_container_handler(
                    after_request=after_request,
                    cookies=cookies,
                    headers=headers,
                    media_type=media_type,
                    status_code=self.status_code,
                )

            elif is_class_and_subclass(self.signature.return_annotation, Response):
                handler = _create_response_handler(cookies=cookies, after_request=after_request)

            elif is_async_callable(self.signature.return_annotation) or self.signature.return_annotation in {
                ASGIApp,
                "ASGIApp",
            }:
                handler = _create_generic_asgi_response_handler(cookies=cookies, after_request=after_request)

            else:
                handler = _create_data_handler(
                    after_request=after_request,
                    background=self.background,
                    cookies=cookies,
                    headers=headers,
                    media_type=media_type,
                    response_class=response_class,
                    return_annotation=self.signature.return_annotation,
                    status_code=self.status_code,
                    type_encoders=type_encoders,
                )

            self._resolved_response_handler = handler
        return self._resolved_response_handler  # type:ignore[return-value]

    async def to_response(
        self, app: "Starlite", data: Any, plugins: list["SerializationPluginProtocol"], request: "Request"
    ) -> "ASGIApp":
        """Return a :class:`Response <starlite.Response>` from the handler by resolving and calling it.

        Args:
            app: The :class:`Starlite <starlite.app.Starlite>` app instance
            data: Either an instance of a :class:`ResponseContainer <starlite.datastructures.ResponseContainer>`,
                a Response instance or an arbitrary value.
            plugins: An optional mapping of plugins
            request: A :class:`Request <starlite.connection.request.Request>` instance

        Returns:
            A Response instance
        """
        response_handler = self.resolve_response_handler()
        return await response_handler(app=app, data=data, plugins=plugins, request=request)  # type: ignore

    def _validate_handler_function(self) -> None:
        """Validate the route handler function once it is set by inspecting its return annotations."""
        super()._validate_handler_function()

        if self.signature.return_annotation is Signature.empty:
            raise ImproperlyConfiguredException(
                "A return value of a route handler function should be type annotated."
                "If your function doesn't return a value, annotate it as returning 'None'."
            )

        if (
            self.status_code < 200 or self.status_code in {HTTP_204_NO_CONTENT, HTTP_304_NOT_MODIFIED}
        ) and self.signature.return_annotation not in {None, "None"}:
            raise ImproperlyConfiguredException(
                "A status code 204, 304 or in the range below 200 does not support a response body."
                "If the function should return a value, change the route handler status code to an appropriate value.",
            )

        if (
            is_class_and_subclass(self.signature.return_annotation, Redirect)
            and self.status_code not in REDIRECT_STATUS_CODES
        ):
            raise ValidationException(
                f"Redirect responses should have one of "
                f"the following status codes: {', '.join([str(s) for s in REDIRECT_STATUS_CODES])}"
            )

        if (
            is_class_and_subclass(self.signature.return_annotation, File)
            or is_class_and_subclass(self.signature.return_annotation, FileResponse)
        ) and self.media_type in (
            MediaType.JSON,
            MediaType.HTML,
        ):
            self.media_type = MediaType.TEXT

        if "socket" in self.signature.parameters:
            raise ImproperlyConfiguredException("The 'socket' kwarg is not supported with http handlers")

        if "data" in self.signature.parameters and "GET" in self.http_methods:
            raise ImproperlyConfiguredException("'data' kwarg is unsupported for 'GET' request handlers")


route = HTTPRouteHandler


class get(HTTPRouteHandler):
    """GET Route Decorator.

    Use this decorator to decorate an HTTP handler for GET requests.
    """

    def __init__(
        self,
        path: str | None | list[str] | None = None,
        *,
        after_request: AfterRequestHookHandler | None = None,
        after_response: AfterResponseHookHandler | None = None,
        background: BackgroundTask | BackgroundTasks | None = None,
        before_request: BeforeRequestHookHandler | None = None,
        cache: bool | int = False,
        cache_control: CacheControlHeader | None = None,
        cache_key_builder: CacheKeyBuilder | None = None,
        dependencies: dict[str, Provide] | None = None,
        etag: ETag | None = None,
        exception_handlers: ExceptionHandlersMap | None = None,
        guards: list[Guard] | None = None,
        media_type: MediaType | str | None = None,
        middleware: list[Middleware] | None = None,
        name: str | None = None,
        opt: dict[str, Any] | None = None,
        response_class: ResponseType | None = None,
        response_cookies: ResponseCookies | None = None,
        response_headers: ResponseHeaders | None = None,
        status_code: int | None = None,
        sync_to_thread: bool = False,
        # OpenAPI related attributes
        content_encoding: str | None = None,
        content_media_type: str | None = None,
        deprecated: bool = False,
        description: str | None = None,
        include_in_schema: bool = True,
        operation_id: str | None = None,
        raises: list[type[HTTPException]] | None = None,
        response_description: str | None = None,
        responses: dict[int, ResponseSpec] | None = None,
        security: list[SecurityRequirement] | None = None,
        summary: str | None = None,
        tags: list[str] | None = None,
        type_encoders: TypeEncodersMap | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize ``get``.

        Args:
            path: A path fragment for the route handler function or a sequence of path fragments.
                If not given defaults to ``'/'``
            after_request: A sync or async function executed before a :class:`Request <starlite.connection.Request>` is passed
                to any route handler. If this function returns a value, the request will not reach the route handler,
                and instead this value will be used.
            after_response: A sync or async function called after the response has been awaited. It receives the
                :class:`Request <starlite.connection.Request>` object and should not return any values.
            background: A :class:`BackgroundTask <starlite.datastructures.BackgroundTask>` instance or
                :class:`BackgroundTasks <starlite.datastructures.BackgroundTasks>` to execute after the response is finished.
                Defaults to ``None``.
            before_request: A sync or async function called immediately before calling the route handler. Receives
                the `starlite.connection.Request`` instance and any non-``None`` return value is used for the response,
                bypassing the route handler.
            cache: Enables response caching if configured on the application level. Valid values are ``True`` or a number
                of seconds (e.g. ``120``) to cache the response.
            cache_control: A ``cache-control`` header of type
                :class:`CacheControlHeader <starlite.datastructures.CacheControlHeader>` that will be added to the response.
            cache_key_builder: A :class:`cache-key builder function <starlite.types.CacheKeyBuilder>`. Allows for customization
                of the cache key if caching is configured on the application level.
            dependencies: A string keyed mapping of dependency :class:`Provider <starlite.datastructures.Provide>` instances.
            etag: An ``etag`` header of type :class:`ETag <starlite.datastructures.ETag>` that will be added to the response.
            exception_handlers: A mapping of status codes and/or exception types to handler functions.
            guards: A sequence of :class:`Guard <starlite.types.Guard>` callables.
            http_method: An :class:`http method string <starlite.types.Method>`, a member of the enum
                :class:`HttpMethod <starlite.enums.HttpMethod>` or a list of these that correlates to the methods the
                route handler function should handle.
            media_type: A member of the :class:`MediaType <starlite.enums.MediaType>` enum or a string with a
                valid IANA Media-Type.
            middleware: A sequence of :class:`Middleware <starlite.types.Middleware>`.
            name: A string identifying the route handler.
            opt: A string keyed mapping of arbitrary values that can be accessed in :class:`Guards <starlite.types.Guard>` or
                wherever you have access to :class:`Request <starlite.connection.request.Request>` or :class:`ASGI Scope <starlite.types.Scope>`.
            response_class: A custom subclass of :class:`Response <starlite.response.Response>` to be used as route handler's
                default response.
            response_cookies: A sequence of :class:`Cookie <starlite.datastructures.Cookie>` instances.
            response_headers: A string keyed mapping of :class:`ResponseHeader <starlite.datastructures.ResponseHeader>`
                instances.
            responses: A mapping of additional status codes and a description of their expected content.
                This information will be included in the OpenAPI schema
            status_code: An http status code for the response. Defaults to ``200`` for mixed method or ``GET``, ``PUT`` and
                ``PATCH``, ``201`` for ``POST`` and ``204`` for ``DELETE``.
            sync_to_thread: A boolean dictating whether the handler function will be executed in a worker thread or the
                main event loop. This has an effect only for sync handler functions. See using sync handler functions.
            content_encoding: A string describing the encoding of the content, e.g. ``"base64"``.
            content_media_type: A string designating the media-type of the content, e.g. ``"image/png"``.
            deprecated:  A boolean dictating whether this route should be marked as deprecated in the OpenAPI schema.
            description: Text used for the route's schema description section.
            include_in_schema: A boolean flag dictating whether  the route handler should be documented in the OpenAPI schema.
            operation_id: An identifier used for the route's schema operationId. Defaults to the ``__name__`` of the wrapped function.
            raises:  A list of exception classes extending from starlite.HttpException that is used for the OpenAPI documentation.
                This list should describe all exceptions raised within the route handler's function/method. The Starlite
                ValidationException will be added automatically for the schema if any validation is involved.
            response_description: Text used for the route's response schema description section.
            security: A sequence of dictionaries that contain information about which security scheme can be used on the endpoint.
            summary: Text used for the route's schema summary section.
            tags: A sequence of string tags that will be appended to the OpenAPI schema.
            type_encoders: A mapping of types to callables that transform them into types supported for serialization.
            **kwargs: Any additional kwarg - will be set in the opt dictionary.
        """
        if "http_method" in kwargs:
            raise ImproperlyConfiguredException(MSG_SEMANTIC_ROUTE_HANDLER_WITH_HTTP)

        super().__init__(
            after_request=after_request,
            after_response=after_response,
            background=background,
            before_request=before_request,
            cache=cache,
            cache_control=cache_control,
            cache_key_builder=cache_key_builder,
            content_encoding=content_encoding,
            content_media_type=content_media_type,
            dependencies=dependencies,
            deprecated=deprecated,
            description=description,
            etag=etag,
            exception_handlers=exception_handlers,
            guards=guards,
            http_method=HttpMethod.GET,
            include_in_schema=include_in_schema,
            media_type=media_type,
            middleware=middleware,
            name=name,
            operation_id=operation_id,
            opt=opt,
            path=path,
            raises=raises,
            response_class=response_class,
            response_cookies=response_cookies,
            response_description=response_description,
            response_headers=response_headers,
            responses=responses,
            security=security,
            status_code=status_code,
            summary=summary,
            sync_to_thread=sync_to_thread,
            tags=tags,
            type_encoders=type_encoders,
            **kwargs,
        )


class head(HTTPRouteHandler):
    """HEAD Route Decorator.

    Use this decorator to decorate an HTTP handler for HEAD requests.
    """

    def __init__(
        self,
        path: str | None | list[str] | None = None,
        *,
        after_request: AfterRequestHookHandler | None = None,
        after_response: AfterResponseHookHandler | None = None,
        background: BackgroundTask | BackgroundTasks | None = None,
        before_request: BeforeRequestHookHandler | None = None,
        cache: bool | int = False,
        cache_control: CacheControlHeader | None = None,
        cache_key_builder: CacheKeyBuilder | None = None,
        dependencies: dict[str, Provide] | None = None,
        etag: ETag | None = None,
        exception_handlers: ExceptionHandlersMap | None = None,
        guards: list[Guard] | None = None,
        media_type: MediaType | str | None = None,
        middleware: list[Middleware] | None = None,
        name: str | None = None,
        opt: dict[str, Any] | None = None,
        response_class: ResponseType | None = None,
        response_cookies: ResponseCookies | None = None,
        response_headers: ResponseHeaders | None = None,
        status_code: int | None = None,
        sync_to_thread: bool = False,
        # OpenAPI related attributes
        content_encoding: str | None = None,
        content_media_type: str | None = None,
        deprecated: bool = False,
        description: str | None = None,
        include_in_schema: bool = True,
        operation_id: str | None = None,
        raises: list[type[HTTPException]] | None = None,
        response_description: str | None = None,
        responses: dict[int, ResponseSpec] | None = None,
        security: list[SecurityRequirement] | None = None,
        summary: str | None = None,
        tags: list[str] | None = None,
        type_encoders: TypeEncodersMap | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize ``head``.

        Notes:
            - A response to a head request cannot include a body.
                See: [MDN](https://developer.mozilla.org/en-US/docs/Web/HTTP/Methods/HEAD).

        Args:
            path: A path fragment for the route handler function or a sequence of path fragments.
                If not given defaults to ``'/'``
            after_request: A sync or async function executed before a :class:`Request <starlite.connection.Request>` is passed
                to any route handler. If this function returns a value, the request will not reach the route handler,
                and instead this value will be used.
            after_response: A sync or async function called after the response has been awaited. It receives the
                :class:`Request <starlite.connection.Request>` object and should not return any values.
            background: A :class:`BackgroundTask <starlite.datastructures.BackgroundTask>` instance or
                :class:`BackgroundTasks <starlite.datastructures.BackgroundTasks>` to execute after the response is finished.
                Defaults to ``None``.
            before_request: A sync or async function called immediately before calling the route handler. Receives
                the `starlite.connection.Request`` instance and any non-``None`` return value is used for the response,
                bypassing the route handler.
            cache: Enables response caching if configured on the application level. Valid values are ``True`` or a number
                of seconds (e.g. ``120``) to cache the response.
            cache_control: A ``cache-control`` header of type
                :class:`CacheControlHeader <starlite.datastructures.CacheControlHeader>` that will be added to the response.
            cache_key_builder: A :class:`cache-key builder function <starlite.types.CacheKeyBuilder>`. Allows for customization
                of the cache key if caching is configured on the application level.
            dependencies: A string keyed mapping of dependency :class:`Provider <starlite.datastructures.Provide>` instances.
            etag: An ``etag`` header of type :class:`ETag <starlite.datastructures.ETag>` that will be added to the response.
            exception_handlers: A mapping of status codes and/or exception types to handler functions.
            guards: A sequence of :class:`Guard <starlite.types.Guard>` callables.
            http_method: An :class:`http method string <starlite.types.Method>`, a member of the enum
                :class:`HttpMethod <starlite.enums.HttpMethod>` or a list of these that correlates to the methods the
                route handler function should handle.
            media_type: A member of the :class:`MediaType <starlite.enums.MediaType>` enum or a string with a
                valid IANA Media-Type.
            middleware: A sequence of :class:`Middleware <starlite.types.Middleware>`.
            name: A string identifying the route handler.
            opt: A string keyed mapping of arbitrary values that can be accessed in :class:`Guards <starlite.types.Guard>` or
                wherever you have access to :class:`Request <starlite.connection.request.Request>` or :class:`ASGI Scope <starlite.types.Scope>`.
            response_class: A custom subclass of :class:`Response <starlite.response.Response>` to be used as route handler's
                default response.
            response_cookies: A sequence of :class:`Cookie <starlite.datastructures.Cookie>` instances.
            response_headers: A string keyed mapping of :class:`ResponseHeader <starlite.datastructures.ResponseHeader>`
                instances.
            responses: A mapping of additional status codes and a description of their expected content.
                This information will be included in the OpenAPI schema
            status_code: An http status code for the response. Defaults to ``200`` for mixed method or ``GET``, ``PUT`` and
                ``PATCH``, ``201`` for ``POST`` and ``204`` for ``DELETE``.
            sync_to_thread: A boolean dictating whether the handler function will be executed in a worker thread or the
                main event loop. This has an effect only for sync handler functions. See using sync handler functions.
            content_encoding: A string describing the encoding of the content, e.g. ``"base64"``.
            content_media_type: A string designating the media-type of the content, e.g. ``"image/png"``.
            deprecated:  A boolean dictating whether this route should be marked as deprecated in the OpenAPI schema.
            description: Text used for the route's schema description section.
            include_in_schema: A boolean flag dictating whether  the route handler should be documented in the OpenAPI schema.
            operation_id: An identifier used for the route's schema operationId. Defaults to the ``__name__`` of the wrapped function.
            raises:  A list of exception classes extending from starlite.HttpException that is used for the OpenAPI documentation.
                This list should describe all exceptions raised within the route handler's function/method. The Starlite
                ValidationException will be added automatically for the schema if any validation is involved.
            response_description: Text used for the route's response schema description section.
            security: A sequence of dictionaries that contain information about which security scheme can be used on the endpoint.
            summary: Text used for the route's schema summary section.
            tags: A sequence of string tags that will be appended to the OpenAPI schema.
            type_encoders: A mapping of types to callables that transform them into types supported for serialization.
            **kwargs: Any additional kwarg - will be set in the opt dictionary.
        """
        if "http_method" in kwargs:
            raise ImproperlyConfiguredException(MSG_SEMANTIC_ROUTE_HANDLER_WITH_HTTP)

        super().__init__(
            after_request=after_request,
            after_response=after_response,
            background=background,
            before_request=before_request,
            cache=cache,
            cache_control=cache_control,
            cache_key_builder=cache_key_builder,
            content_encoding=content_encoding,
            content_media_type=content_media_type,
            dependencies=dependencies,
            deprecated=deprecated,
            description=description,
            etag=etag,
            exception_handlers=exception_handlers,
            guards=guards,
            http_method=HttpMethod.HEAD,
            include_in_schema=include_in_schema,
            media_type=media_type,
            middleware=middleware,
            name=name,
            operation_id=operation_id,
            opt=opt,
            path=path,
            raises=raises,
            response_class=response_class,
            response_cookies=response_cookies,
            response_description=response_description,
            response_headers=response_headers,
            responses=responses,
            security=security,
            status_code=status_code,
            summary=summary,
            sync_to_thread=sync_to_thread,
            tags=tags,
            type_encoders=type_encoders,
            **kwargs,
        )

    def _validate_handler_function(self) -> None:
        """Validate the route handler function once it is set by inspecting its return annotations."""
        super()._validate_handler_function()

        # we allow here File and FileResponse because these have special setting for head responses
        if not (
            self.signature.return_annotation in {None, "None", "FileResponse", "File"}
            or is_class_and_subclass(self.signature.return_annotation, File)
            or is_class_and_subclass(self.signature.return_annotation, FileResponse)
        ):
            raise ImproperlyConfiguredException(
                "A response to a head request should not have a body",
            )


class post(HTTPRouteHandler):
    """POST Route Decorator.

    Use this decorator to decorate an HTTP handler for POST requests.
    """

    def __init__(
        self,
        path: str | None | list[str] | None = None,
        *,
        after_request: AfterRequestHookHandler | None = None,
        after_response: AfterResponseHookHandler | None = None,
        background: BackgroundTask | BackgroundTasks | None = None,
        before_request: BeforeRequestHookHandler | None = None,
        cache: bool | int = False,
        cache_control: CacheControlHeader | None = None,
        cache_key_builder: CacheKeyBuilder | None = None,
        dependencies: dict[str, Provide] | None = None,
        etag: ETag | None = None,
        exception_handlers: ExceptionHandlersMap | None = None,
        guards: list[Guard] | None = None,
        media_type: MediaType | str | None = None,
        middleware: list[Middleware] | None = None,
        name: str | None = None,
        opt: dict[str, Any] | None = None,
        response_class: ResponseType | None = None,
        response_cookies: ResponseCookies | None = None,
        response_headers: ResponseHeaders | None = None,
        status_code: int | None = None,
        sync_to_thread: bool = False,
        # OpenAPI related attributes
        content_encoding: str | None = None,
        content_media_type: str | None = None,
        deprecated: bool = False,
        description: str | None = None,
        include_in_schema: bool = True,
        operation_id: str | None = None,
        raises: list[type[HTTPException]] | None = None,
        response_description: str | None = None,
        responses: dict[int, ResponseSpec] | None = None,
        security: list[SecurityRequirement] | None = None,
        summary: str | None = None,
        tags: list[str] | None = None,
        type_encoders: TypeEncodersMap | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize ``post``

        Args:
            path: A path fragment for the route handler function or a sequence of path fragments.
                If not given defaults to ``'/'``
            after_request: A sync or async function executed before a :class:`Request <starlite.connection.Request>` is passed
                to any route handler. If this function returns a value, the request will not reach the route handler,
                and instead this value will be used.
            after_response: A sync or async function called after the response has been awaited. It receives the
                :class:`Request <starlite.connection.Request>` object and should not return any values.
            background: A :class:`BackgroundTask <starlite.datastructures.BackgroundTask>` instance or
                :class:`BackgroundTasks <starlite.datastructures.BackgroundTasks>` to execute after the response is finished.
                Defaults to ``None``.
            before_request: A sync or async function called immediately before calling the route handler. Receives
                the `starlite.connection.Request`` instance and any non-``None`` return value is used for the response,
                bypassing the route handler.
            cache: Enables response caching if configured on the application level. Valid values are ``True`` or a number
                of seconds (e.g. ``120``) to cache the response.
            cache_control: A ``cache-control`` header of type
                :class:`CacheControlHeader <starlite.datastructures.CacheControlHeader>` that will be added to the response.
            cache_key_builder: A :class:`cache-key builder function <starlite.types.CacheKeyBuilder>`. Allows for customization
                of the cache key if caching is configured on the application level.
            dependencies: A string keyed mapping of dependency :class:`Provider <starlite.datastructures.Provide>` instances.
            etag: An ``etag`` header of type :class:`ETag <starlite.datastructures.ETag>` that will be added to the response.
            exception_handlers: A mapping of status codes and/or exception types to handler functions.
            guards: A sequence of :class:`Guard <starlite.types.Guard>` callables.
            http_method: An :class:`http method string <starlite.types.Method>`, a member of the enum
                :class:`HttpMethod <starlite.enums.HttpMethod>` or a list of these that correlates to the methods the
                route handler function should handle.
            media_type: A member of the :class:`MediaType <starlite.enums.MediaType>` enum or a string with a
                valid IANA Media-Type.
            middleware: A sequence of :class:`Middleware <starlite.types.Middleware>`.
            name: A string identifying the route handler.
            opt: A string keyed mapping of arbitrary values that can be accessed in :class:`Guards <starlite.types.Guard>` or
                wherever you have access to :class:`Request <starlite.connection.request.Request>` or :class:`ASGI Scope <starlite.types.Scope>`.
            response_class: A custom subclass of :class:`Response <starlite.response.Response>` to be used as route handler's
                default response.
            response_cookies: A sequence of :class:`Cookie <starlite.datastructures.Cookie>` instances.
            response_headers: A string keyed mapping of :class:`ResponseHeader <starlite.datastructures.ResponseHeader>`
                instances.
            responses: A mapping of additional status codes and a description of their expected content.
                This information will be included in the OpenAPI schema
            status_code: An http status code for the response. Defaults to ``200`` for mixed method or ``GET``, ``PUT`` and
                ``PATCH``, ``201`` for ``POST`` and ``204`` for ``DELETE``.
            sync_to_thread: A boolean dictating whether the handler function will be executed in a worker thread or the
                main event loop. This has an effect only for sync handler functions. See using sync handler functions.
            content_encoding: A string describing the encoding of the content, e.g. ``"base64"``.
            content_media_type: A string designating the media-type of the content, e.g. ``"image/png"``.
            deprecated:  A boolean dictating whether this route should be marked as deprecated in the OpenAPI schema.
            description: Text used for the route's schema description section.
            include_in_schema: A boolean flag dictating whether  the route handler should be documented in the OpenAPI schema.
            operation_id: An identifier used for the route's schema operationId. Defaults to the ``__name__`` of the wrapped function.
            raises:  A list of exception classes extending from starlite.HttpException that is used for the OpenAPI documentation.
                This list should describe all exceptions raised within the route handler's function/method. The Starlite
                ValidationException will be added automatically for the schema if any validation is involved.
            response_description: Text used for the route's response schema description section.
            security: A sequence of dictionaries that contain information about which security scheme can be used on the endpoint.
            summary: Text used for the route's schema summary section.
            tags: A sequence of string tags that will be appended to the OpenAPI schema.
            type_encoders: A mapping of types to callables that transform them into types supported for serialization.
            **kwargs: Any additional kwarg - will be set in the opt dictionary.
        """
        if "http_method" in kwargs:
            raise ImproperlyConfiguredException(MSG_SEMANTIC_ROUTE_HANDLER_WITH_HTTP)
        super().__init__(
            after_request=after_request,
            after_response=after_response,
            background=background,
            before_request=before_request,
            cache=cache,
            cache_control=cache_control,
            cache_key_builder=cache_key_builder,
            content_encoding=content_encoding,
            content_media_type=content_media_type,
            dependencies=dependencies,
            deprecated=deprecated,
            description=description,
            exception_handlers=exception_handlers,
            etag=etag,
            guards=guards,
            http_method=HttpMethod.POST,
            include_in_schema=include_in_schema,
            media_type=media_type,
            middleware=middleware,
            name=name,
            operation_id=operation_id,
            opt=opt,
            path=path,
            raises=raises,
            response_class=response_class,
            response_cookies=response_cookies,
            response_description=response_description,
            response_headers=response_headers,
            responses=responses,
            security=security,
            status_code=status_code,
            summary=summary,
            sync_to_thread=sync_to_thread,
            tags=tags,
            type_encoders=type_encoders,
            **kwargs,
        )


class put(HTTPRouteHandler):
    """PUT Route Decorator.

    Use this decorator to decorate an HTTP handler for PUT requests.
    """

    def __init__(
        self,
        path: str | None | list[str] | None = None,
        *,
        after_request: AfterRequestHookHandler | None = None,
        after_response: AfterResponseHookHandler | None = None,
        background: BackgroundTask | BackgroundTasks | None = None,
        before_request: BeforeRequestHookHandler | None = None,
        cache: bool | int = False,
        cache_control: CacheControlHeader | None = None,
        cache_key_builder: CacheKeyBuilder | None = None,
        dependencies: dict[str, Provide] | None = None,
        etag: ETag | None = None,
        exception_handlers: ExceptionHandlersMap | None = None,
        guards: list[Guard] | None = None,
        media_type: MediaType | str | None = None,
        middleware: list[Middleware] | None = None,
        name: str | None = None,
        opt: dict[str, Any] | None = None,
        response_class: ResponseType | None = None,
        response_cookies: ResponseCookies | None = None,
        response_headers: ResponseHeaders | None = None,
        status_code: int | None = None,
        sync_to_thread: bool = False,
        # OpenAPI related attributes
        content_encoding: str | None = None,
        content_media_type: str | None = None,
        deprecated: bool = False,
        description: str | None = None,
        include_in_schema: bool = True,
        operation_id: str | None = None,
        raises: list[type[HTTPException]] | None = None,
        response_description: str | None = None,
        responses: dict[int, ResponseSpec] | None = None,
        security: list[SecurityRequirement] | None = None,
        summary: str | None = None,
        tags: list[str] | None = None,
        type_encoders: TypeEncodersMap | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize ``put``

        Args:
            path: A path fragment for the route handler function or a sequence of path fragments.
                If not given defaults to ``'/'``
            after_request: A sync or async function executed before a :class:`Request <starlite.connection.Request>` is passed
                to any route handler. If this function returns a value, the request will not reach the route handler,
                and instead this value will be used.
            after_response: A sync or async function called after the response has been awaited. It receives the
                :class:`Request <starlite.connection.Request>` object and should not return any values.
            background: A :class:`BackgroundTask <starlite.datastructures.BackgroundTask>` instance or
                :class:`BackgroundTasks <starlite.datastructures.BackgroundTasks>` to execute after the response is finished.
                Defaults to ``None``.
            before_request: A sync or async function called immediately before calling the route handler. Receives
                the `starlite.connection.Request`` instance and any non-``None`` return value is used for the response,
                bypassing the route handler.
            cache: Enables response caching if configured on the application level. Valid values are ``True`` or a number
                of seconds (e.g. ``120``) to cache the response.
            cache_control: A ``cache-control`` header of type
                :class:`CacheControlHeader <starlite.datastructures.CacheControlHeader>` that will be added to the response.
            cache_key_builder: A :class:`cache-key builder function <starlite.types.CacheKeyBuilder>`. Allows for customization
                of the cache key if caching is configured on the application level.
            dependencies: A string keyed mapping of dependency :class:`Provider <starlite.datastructures.Provide>` instances.
            etag: An ``etag`` header of type :class:`ETag <starlite.datastructures.ETag>` that will be added to the response.
            exception_handlers: A mapping of status codes and/or exception types to handler functions.
            guards: A sequence of :class:`Guard <starlite.types.Guard>` callables.
            http_method: An :class:`http method string <starlite.types.Method>`, a member of the enum
                :class:`HttpMethod <starlite.enums.HttpMethod>` or a list of these that correlates to the methods the
                route handler function should handle.
            media_type: A member of the :class:`MediaType <starlite.enums.MediaType>` enum or a string with a
                valid IANA Media-Type.
            middleware: A sequence of :class:`Middleware <starlite.types.Middleware>`.
            name: A string identifying the route handler.
            opt: A string keyed mapping of arbitrary values that can be accessed in :class:`Guards <starlite.types.Guard>` or
                wherever you have access to :class:`Request <starlite.connection.request.Request>` or :class:`ASGI Scope <starlite.types.Scope>`.
            response_class: A custom subclass of :class:`Response <starlite.response.Response>` to be used as route handler's
                default response.
            response_cookies: A sequence of :class:`Cookie <starlite.datastructures.Cookie>` instances.
            response_headers: A string keyed mapping of :class:`ResponseHeader <starlite.datastructures.ResponseHeader>`
                instances.
            responses: A mapping of additional status codes and a description of their expected content.
                This information will be included in the OpenAPI schema
            status_code: An http status code for the response. Defaults to ``200`` for mixed method or ``GET``, ``PUT`` and
                ``PATCH``, ``201`` for ``POST`` and ``204`` for ``DELETE``.
            sync_to_thread: A boolean dictating whether the handler function will be executed in a worker thread or the
                main event loop. This has an effect only for sync handler functions. See using sync handler functions.
            content_encoding: A string describing the encoding of the content, e.g. ``"base64"``.
            content_media_type: A string designating the media-type of the content, e.g. ``"image/png"``.
            deprecated:  A boolean dictating whether this route should be marked as deprecated in the OpenAPI schema.
            description: Text used for the route's schema description section.
            include_in_schema: A boolean flag dictating whether  the route handler should be documented in the OpenAPI schema.
            operation_id: An identifier used for the route's schema operationId. Defaults to the ``__name__`` of the wrapped function.
            raises:  A list of exception classes extending from starlite.HttpException that is used for the OpenAPI documentation.
                This list should describe all exceptions raised within the route handler's function/method. The Starlite
                ValidationException will be added automatically for the schema if any validation is involved.
            response_description: Text used for the route's response schema description section.
            security: A sequence of dictionaries that contain information about which security scheme can be used on the endpoint.
            summary: Text used for the route's schema summary section.
            tags: A sequence of string tags that will be appended to the OpenAPI schema.
            type_encoders: A mapping of types to callables that transform them into types supported for serialization.
            **kwargs: Any additional kwarg - will be set in the opt dictionary.
        """
        if "http_method" in kwargs:
            raise ImproperlyConfiguredException(MSG_SEMANTIC_ROUTE_HANDLER_WITH_HTTP)
        super().__init__(
            after_request=after_request,
            after_response=after_response,
            background=background,
            before_request=before_request,
            cache=cache,
            cache_control=cache_control,
            cache_key_builder=cache_key_builder,
            content_encoding=content_encoding,
            content_media_type=content_media_type,
            dependencies=dependencies,
            deprecated=deprecated,
            description=description,
            exception_handlers=exception_handlers,
            etag=etag,
            guards=guards,
            http_method=HttpMethod.PUT,
            include_in_schema=include_in_schema,
            media_type=media_type,
            middleware=middleware,
            name=name,
            operation_id=operation_id,
            opt=opt,
            path=path,
            raises=raises,
            response_class=response_class,
            response_cookies=response_cookies,
            response_description=response_description,
            response_headers=response_headers,
            responses=responses,
            security=security,
            status_code=status_code,
            summary=summary,
            sync_to_thread=sync_to_thread,
            tags=tags,
            type_encoders=type_encoders,
            **kwargs,
        )


class patch(HTTPRouteHandler):
    """PATCH Route Decorator.

    Use this decorator to decorate an HTTP handler for PATCH requests.
    """

    def __init__(
        self,
        path: str | None | list[str] | None = None,
        *,
        after_request: AfterRequestHookHandler | None = None,
        after_response: AfterResponseHookHandler | None = None,
        background: BackgroundTask | BackgroundTasks | None = None,
        before_request: BeforeRequestHookHandler | None = None,
        cache: bool | int = False,
        cache_control: CacheControlHeader | None = None,
        cache_key_builder: CacheKeyBuilder | None = None,
        dependencies: dict[str, Provide] | None = None,
        etag: ETag | None = None,
        exception_handlers: ExceptionHandlersMap | None = None,
        guards: list[Guard] | None = None,
        media_type: MediaType | str | None = None,
        middleware: list[Middleware] | None = None,
        name: str | None = None,
        opt: dict[str, Any] | None = None,
        response_class: ResponseType | None = None,
        response_cookies: ResponseCookies | None = None,
        response_headers: ResponseHeaders | None = None,
        status_code: int | None = None,
        sync_to_thread: bool = False,
        # OpenAPI related attributes
        content_encoding: str | None = None,
        content_media_type: str | None = None,
        deprecated: bool = False,
        description: str | None = None,
        include_in_schema: bool = True,
        operation_id: str | None = None,
        raises: list[type[HTTPException]] | None = None,
        response_description: str | None = None,
        responses: dict[int, ResponseSpec] | None = None,
        security: list[SecurityRequirement] | None = None,
        summary: str | None = None,
        tags: list[str] | None = None,
        type_encoders: TypeEncodersMap | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize ``patch``.

        Args:
            path: A path fragment for the route handler function or a sequence of path fragments.
                If not given defaults to ``'/'``
            after_request: A sync or async function executed before a :class:`Request <starlite.connection.Request>` is passed
                to any route handler. If this function returns a value, the request will not reach the route handler,
                and instead this value will be used.
            after_response: A sync or async function called after the response has been awaited. It receives the
                :class:`Request <starlite.connection.Request>` object and should not return any values.
            background: A :class:`BackgroundTask <starlite.datastructures.BackgroundTask>` instance or
                :class:`BackgroundTasks <starlite.datastructures.BackgroundTasks>` to execute after the response is finished.
                Defaults to ``None``.
            before_request: A sync or async function called immediately before calling the route handler. Receives
                the `starlite.connection.Request`` instance and any non-``None`` return value is used for the response,
                bypassing the route handler.
            cache: Enables response caching if configured on the application level. Valid values are ``True`` or a number
                of seconds (e.g. ``120``) to cache the response.
            cache_control: A ``cache-control`` header of type
                :class:`CacheControlHeader <starlite.datastructures.CacheControlHeader>` that will be added to the response.
            cache_key_builder: A :class:`cache-key builder function <starlite.types.CacheKeyBuilder>`. Allows for customization
                of the cache key if caching is configured on the application level.
            dependencies: A string keyed mapping of dependency :class:`Provider <starlite.datastructures.Provide>` instances.
            etag: An ``etag`` header of type :class:`ETag <starlite.datastructures.ETag>` that will be added to the response.
            exception_handlers: A mapping of status codes and/or exception types to handler functions.
            guards: A sequence of :class:`Guard <starlite.types.Guard>` callables.
            http_method: An :class:`http method string <starlite.types.Method>`, a member of the enum
                :class:`HttpMethod <starlite.enums.HttpMethod>` or a list of these that correlates to the methods the
                route handler function should handle.
            media_type: A member of the :class:`MediaType <starlite.enums.MediaType>` enum or a string with a
                valid IANA Media-Type.
            middleware: A sequence of :class:`Middleware <starlite.types.Middleware>`.
            name: A string identifying the route handler.
            opt: A string keyed mapping of arbitrary values that can be accessed in :class:`Guards <starlite.types.Guard>` or
                wherever you have access to :class:`Request <starlite.connection.request.Request>` or :class:`ASGI Scope <starlite.types.Scope>`.
            response_class: A custom subclass of :class:`Response <starlite.response.Response>` to be used as route handler's
                default response.
            response_cookies: A sequence of :class:`Cookie <starlite.datastructures.Cookie>` instances.
            response_headers: A string keyed mapping of :class:`ResponseHeader <starlite.datastructures.ResponseHeader>`
                instances.
            responses: A mapping of additional status codes and a description of their expected content.
                This information will be included in the OpenAPI schema
            status_code: An http status code for the response. Defaults to ``200`` for mixed method or ``GET``, ``PUT`` and
                ``PATCH``, ``201`` for ``POST`` and ``204`` for ``DELETE``.
            sync_to_thread: A boolean dictating whether the handler function will be executed in a worker thread or the
                main event loop. This has an effect only for sync handler functions. See using sync handler functions.
            content_encoding: A string describing the encoding of the content, e.g. ``"base64"``.
            content_media_type: A string designating the media-type of the content, e.g. ``"image/png"``.
            deprecated:  A boolean dictating whether this route should be marked as deprecated in the OpenAPI schema.
            description: Text used for the route's schema description section.
            include_in_schema: A boolean flag dictating whether  the route handler should be documented in the OpenAPI schema.
            operation_id: An identifier used for the route's schema operationId. Defaults to the ``__name__`` of the wrapped function.
            raises:  A list of exception classes extending from starlite.HttpException that is used for the OpenAPI documentation.
                This list should describe all exceptions raised within the route handler's function/method. The Starlite
                ValidationException will be added automatically for the schema if any validation is involved.
            response_description: Text used for the route's response schema description section.
            security: A sequence of dictionaries that contain information about which security scheme can be used on the endpoint.
            summary: Text used for the route's schema summary section.
            tags: A sequence of string tags that will be appended to the OpenAPI schema.
            type_encoders: A mapping of types to callables that transform them into types supported for serialization.
            **kwargs: Any additional kwarg - will be set in the opt dictionary.
        """
        if "http_method" in kwargs:
            raise ImproperlyConfiguredException(MSG_SEMANTIC_ROUTE_HANDLER_WITH_HTTP)
        super().__init__(
            after_request=after_request,
            after_response=after_response,
            background=background,
            before_request=before_request,
            cache=cache,
            cache_control=cache_control,
            cache_key_builder=cache_key_builder,
            content_encoding=content_encoding,
            content_media_type=content_media_type,
            dependencies=dependencies,
            deprecated=deprecated,
            description=description,
            etag=etag,
            exception_handlers=exception_handlers,
            guards=guards,
            http_method=HttpMethod.PATCH,
            include_in_schema=include_in_schema,
            media_type=media_type,
            middleware=middleware,
            name=name,
            operation_id=operation_id,
            opt=opt,
            path=path,
            raises=raises,
            response_class=response_class,
            response_cookies=response_cookies,
            response_description=response_description,
            response_headers=response_headers,
            responses=responses,
            security=security,
            status_code=status_code,
            summary=summary,
            sync_to_thread=sync_to_thread,
            tags=tags,
            type_encoders=type_encoders,
            **kwargs,
        )


class delete(HTTPRouteHandler):
    """DELETE Route Decorator.

    Use this decorator to decorate an HTTP handler for DELETE requests.
    """

    def __init__(
        self,
        path: str | None | list[str] | None = None,
        *,
        after_request: AfterRequestHookHandler | None = None,
        after_response: AfterResponseHookHandler | None = None,
        background: BackgroundTask | BackgroundTasks | None = None,
        before_request: BeforeRequestHookHandler | None = None,
        cache: bool | int = False,
        cache_control: CacheControlHeader | None = None,
        cache_key_builder: CacheKeyBuilder | None = None,
        dependencies: dict[str, Provide] | None = None,
        etag: ETag | None = None,
        exception_handlers: ExceptionHandlersMap | None = None,
        guards: list[Guard] | None = None,
        media_type: MediaType | str | None = None,
        middleware: list[Middleware] | None = None,
        name: str | None = None,
        opt: dict[str, Any] | None = None,
        response_class: ResponseType | None = None,
        response_cookies: ResponseCookies | None = None,
        response_headers: ResponseHeaders | None = None,
        status_code: int | None = None,
        sync_to_thread: bool = False,
        # OpenAPI related attributes
        content_encoding: str | None = None,
        content_media_type: str | None = None,
        deprecated: bool = False,
        description: str | None = None,
        include_in_schema: bool = True,
        operation_id: str | None = None,
        raises: list[type[HTTPException]] | None = None,
        response_description: str | None = None,
        responses: dict[int, ResponseSpec] | None = None,
        security: list[SecurityRequirement] | None = None,
        summary: str | None = None,
        tags: list[str] | None = None,
        type_encoders: TypeEncodersMap | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize ``delete``

        Args:
            path: A path fragment for the route handler function or a sequence of path fragments.
                If not given defaults to ``'/'``
            after_request: A sync or async function executed before a :class:`Request <starlite.connection.Request>` is passed
                to any route handler. If this function returns a value, the request will not reach the route handler,
                and instead this value will be used.
            after_response: A sync or async function called after the response has been awaited. It receives the
                :class:`Request <starlite.connection.Request>` object and should not return any values.
            background: A :class:`BackgroundTask <starlite.datastructures.BackgroundTask>` instance or
                :class:`BackgroundTasks <starlite.datastructures.BackgroundTasks>` to execute after the response is finished.
                Defaults to ``None``.
            before_request: A sync or async function called immediately before calling the route handler. Receives
                the `starlite.connection.Request`` instance and any non-``None`` return value is used for the response,
                bypassing the route handler.
            cache: Enables response caching if configured on the application level. Valid values are ``True`` or a number
                of seconds (e.g. ``120``) to cache the response.
            cache_control: A ``cache-control`` header of type
                :class:`CacheControlHeader <starlite.datastructures.CacheControlHeader>` that will be added to the response.
            cache_key_builder: A :class:`cache-key builder function <starlite.types.CacheKeyBuilder>`. Allows for customization
                of the cache key if caching is configured on the application level.
            dependencies: A string keyed mapping of dependency :class:`Provider <starlite.datastructures.Provide>` instances.
            etag: An ``etag`` header of type :class:`ETag <starlite.datastructures.ETag>` that will be added to the response.
            exception_handlers: A mapping of status codes and/or exception types to handler functions.
            guards: A sequence of :class:`Guard <starlite.types.Guard>` callables.
            http_method: An :class:`http method string <starlite.types.Method>`, a member of the enum
                :class:`HttpMethod <starlite.enums.HttpMethod>` or a list of these that correlates to the methods the
                route handler function should handle.
            media_type: A member of the :class:`MediaType <starlite.enums.MediaType>` enum or a string with a
                valid IANA Media-Type.
            middleware: A sequence of :class:`Middleware <starlite.types.Middleware>`.
            name: A string identifying the route handler.
            opt: A string keyed mapping of arbitrary values that can be accessed in :class:`Guards <starlite.types.Guard>` or
                wherever you have access to :class:`Request <starlite.connection.request.Request>` or :class:`ASGI Scope <starlite.types.Scope>`.
            response_class: A custom subclass of :class:`Response <starlite.response.Response>` to be used as route handler's
                default response.
            response_cookies: A sequence of :class:`Cookie <starlite.datastructures.Cookie>` instances.
            response_headers: A string keyed mapping of :class:`ResponseHeader <starlite.datastructures.ResponseHeader>`
                instances.
            responses: A mapping of additional status codes and a description of their expected content.
                This information will be included in the OpenAPI schema
            status_code: An http status code for the response. Defaults to ``200`` for mixed method or ``GET``, ``PUT`` and
                ``PATCH``, ``201`` for ``POST`` and ``204`` for ``DELETE``.
            sync_to_thread: A boolean dictating whether the handler function will be executed in a worker thread or the
                main event loop. This has an effect only for sync handler functions. See using sync handler functions.
            content_encoding: A string describing the encoding of the content, e.g. ``"base64"``.
            content_media_type: A string designating the media-type of the content, e.g. ``"image/png"``.
            deprecated:  A boolean dictating whether this route should be marked as deprecated in the OpenAPI schema.
            description: Text used for the route's schema description section.
            include_in_schema: A boolean flag dictating whether  the route handler should be documented in the OpenAPI schema.
            operation_id: An identifier used for the route's schema operationId. Defaults to the ``__name__`` of the wrapped function.
            raises:  A list of exception classes extending from starlite.HttpException that is used for the OpenAPI documentation.
                This list should describe all exceptions raised within the route handler's function/method. The Starlite
                ValidationException will be added automatically for the schema if any validation is involved.
            response_description: Text used for the route's response schema description section.
            security: A sequence of dictionaries that contain information about which security scheme can be used on the endpoint.
            summary: Text used for the route's schema summary section.
            tags: A sequence of string tags that will be appended to the OpenAPI schema.
            type_encoders: A mapping of types to callables that transform them into types supported for serialization.
            **kwargs: Any additional kwarg - will be set in the opt dictionary.
        """
        if "http_method" in kwargs:
            raise ImproperlyConfiguredException(MSG_SEMANTIC_ROUTE_HANDLER_WITH_HTTP)
        super().__init__(
            after_request=after_request,
            after_response=after_response,
            background=background,
            before_request=before_request,
            cache=cache,
            cache_control=cache_control,
            cache_key_builder=cache_key_builder,
            content_encoding=content_encoding,
            content_media_type=content_media_type,
            dependencies=dependencies,
            deprecated=deprecated,
            description=description,
            etag=etag,
            exception_handlers=exception_handlers,
            guards=guards,
            http_method=HttpMethod.DELETE,
            include_in_schema=include_in_schema,
            media_type=media_type,
            middleware=middleware,
            name=name,
            operation_id=operation_id,
            opt=opt,
            path=path,
            raises=raises,
            response_class=response_class,
            response_cookies=response_cookies,
            response_description=response_description,
            response_headers=response_headers,
            responses=responses,
            security=security,
            status_code=status_code,
            summary=summary,
            sync_to_thread=sync_to_thread,
            tags=tags,
            type_encoders=type_encoders,
            **kwargs,
        )
