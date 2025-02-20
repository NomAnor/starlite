from logging import INFO
from typing import TYPE_CHECKING, Dict

import pytest
from structlog.testing import capture_logs

from starlite import Response, get, post
from starlite.config.compression import CompressionConfig
from starlite.config.logging import LoggingConfig, StructLoggingConfig
from starlite.connection import Request
from starlite.datastructures import Cookie
from starlite.middleware.logging import LoggingMiddlewareConfig
from starlite.status_codes import HTTP_200_OK, HTTP_201_CREATED
from starlite.testing import create_test_client

if TYPE_CHECKING:
    from _pytest.logging import LogCaptureFixture

    from starlite.middleware.session.server_side import ServerSideSessionConfig
    from starlite.types.callable_types import GetLogger


@get("/")
def handler() -> Response:
    return Response(
        content={"hello": "world"},
        headers={"token": "123", "regular": "abc"},
        cookies=[Cookie(key="first-cookie", value="abc"), Cookie(key="second-cookie", value="xxx")],
    )


def test_logging_middleware_regular_logger(get_logger: "GetLogger", caplog: "LogCaptureFixture") -> None:
    with create_test_client(
        route_handlers=[handler], middleware=[LoggingMiddlewareConfig().middleware]
    ) as client, caplog.at_level(INFO):
        # Set cookies on the client to avoid warnings about per-request cookies.
        client.app.get_logger = get_logger
        client.cookies = {"request-cookie": "abc"}  # type: ignore
        response = client.get("/", headers={"request-header": "1"})
        assert response.status_code == HTTP_200_OK
        assert len(caplog.messages) == 2

        assert (
            caplog.messages[0] == 'HTTP Request: path=/, method=GET, content_type=["",{}], '
            'headers={"host":"testserver.local","accept":"*/*","accept-encoding":"gzip, '
            'deflate, br","connection":"keep-alive","user-agent":"testclient",'
            '"request-header":"1","cookie":"request-cookie=abc"}, '
            'cookies={"request-cookie":"abc"}, query={}, path_params={}, body=None'
        )
        assert (
            caplog.messages[1] == 'HTTP Response: status_code=200, cookies={"first-cookie":"abc","Path":"/","SameSite":'
            '"lax","second-cookie":"xxx"}, headers={"token":"123","regular":"abc","content-type":'
            '"application/json","content-length":"17"}, body={"hello":"world"}'
        )


def test_logging_middleware_struct_logger() -> None:
    with create_test_client(
        route_handlers=[handler],
        middleware=[LoggingMiddlewareConfig().middleware],
        logging_config=StructLoggingConfig(),
    ) as client, capture_logs() as cap_logs:
        # Set cookies on the client to avoid warnings about per-request cookies.
        client.cookies = {"request-cookie": "abc"}  # type: ignore
        response = client.get("/", headers={"request-header": "1"})
        assert response.status_code == HTTP_200_OK
        assert len(cap_logs) == 2
        assert cap_logs[0] == {
            "path": "/",
            "method": "GET",
            "body": None,
            "content_type": ("", {}),
            "headers": {
                "host": "testserver.local",
                "accept": "*/*",
                "accept-encoding": "gzip, deflate, br",
                "connection": "keep-alive",
                "user-agent": "testclient",
                "request-header": "1",
                "cookie": "request-cookie=abc",
            },
            "cookies": {"request-cookie": "abc"},
            "query": {},
            "path_params": {},
            "event": "HTTP Request",
            "log_level": "info",
        }
        assert cap_logs[1] == {
            "status_code": 200,
            "cookies": {"first-cookie": "abc", "Path": "/", "SameSite": "lax", "second-cookie": "xxx"},
            "headers": {"token": "123", "regular": "abc", "content-length": "17", "content-type": "application/json"},
            "body": '{"hello":"world"}',
            "event": "HTTP Response",
            "log_level": "info",
        }


def test_logging_middleware_exclude_pattern(get_logger: "GetLogger", caplog: "LogCaptureFixture") -> None:
    @get("/exclude")
    def handler2() -> None:
        return None

    config = LoggingMiddlewareConfig(exclude=["^/exclude"])
    with create_test_client(
        route_handlers=[handler, handler2], middleware=[config.middleware]
    ) as client, caplog.at_level(INFO):
        # Set cookies on the client to avoid warnings about per-request cookies.
        client.cookies = {"request-cookie": "abc"}  # type: ignore
        client.app.get_logger = get_logger

        response = client.get("/exclude")
        assert response.status_code == HTTP_200_OK
        assert len(caplog.messages) == 0

        response = client.get("/")
        assert response.status_code == HTTP_200_OK
        assert len(caplog.messages) == 2


def test_logging_middleware_exclude_opt_key(get_logger: "GetLogger", caplog: "LogCaptureFixture") -> None:
    @get("/exclude", skip_logging=True)
    def handler2() -> None:
        return None

    config = LoggingMiddlewareConfig(exclude_opt_key="skip_logging")
    with create_test_client(
        route_handlers=[handler, handler2], middleware=[config.middleware]
    ) as client, caplog.at_level(INFO):
        # Set cookies on the client to avoid warnings about per-request cookies.
        client.cookies = {"request-cookie": "abc"}  # type: ignore
        client.app.get_logger = get_logger

        response = client.get("/exclude")
        assert response.status_code == HTTP_200_OK
        assert len(caplog.messages) == 0

        response = client.get("/")
        assert response.status_code == HTTP_200_OK
        assert len(caplog.messages) == 2


@pytest.mark.parametrize("include", [True, False])
def test_logging_middleware_compressed_response_body(
    get_logger: "GetLogger", include: bool, caplog: "LogCaptureFixture"
) -> None:
    with create_test_client(
        route_handlers=[handler],
        compression_config=CompressionConfig(backend="gzip", minimum_size=1),
        middleware=[LoggingMiddlewareConfig(include_compressed_body=include).middleware],
    ) as client, caplog.at_level(INFO):
        # Set cookies on the client to avoid warnings about per-request cookies.
        client.cookies = {"request-cookie": "abc"}  # type: ignore
        client.app.get_logger = get_logger
        response = client.get("/", headers={"request-header": "1"})
        assert response.status_code == HTTP_200_OK
        assert len(caplog.messages) == 2
        if include:
            assert "body=" in caplog.messages[1]
        else:
            assert "body=" not in caplog.messages[1]


def test_logging_middleware_post_body() -> None:
    @post("/")
    def post_handler(data: Dict[str, str]) -> Dict[str, str]:
        return data

    with create_test_client(
        route_handlers=[post_handler], middleware=[LoggingMiddlewareConfig().middleware], logging_config=LoggingConfig()
    ) as client:
        res = client.post("/", json={"foo": "bar"})
        assert res.status_code == 201
        assert res.json() == {"foo": "bar"}


@pytest.mark.parametrize("logger_name", ("starlite", "other"))
def test_logging_messages_are_not_doubled(
    get_logger: "GetLogger", logger_name: str, caplog: "LogCaptureFixture"
) -> None:
    # https://github.com/starlite-api/starlite/issues/896

    @get("/")
    async def hello_world_handler() -> Dict[str, str]:
        return {"hello": "world"}

    logging_middleware_config = LoggingMiddlewareConfig(logger_name=logger_name)

    with create_test_client(
        hello_world_handler,
        logging_config=LoggingConfig(),
        middleware=[logging_middleware_config.middleware],
    ) as client, caplog.at_level(INFO):
        client.app.get_logger = get_logger
        response = client.get("/")
        assert response.status_code == HTTP_200_OK
        assert len(caplog.messages) == 2


def test_logging_middleware_log_fields(get_logger: "GetLogger", caplog: "LogCaptureFixture") -> None:
    with create_test_client(
        route_handlers=[handler],
        middleware=[
            LoggingMiddlewareConfig(response_log_fields=["status_code"], request_log_fields=["path"]).middleware
        ],
    ) as client, caplog.at_level(INFO):
        # Set cookies on the client to avoid warnings about per-request cookies.
        client.app.get_logger = get_logger
        client.cookies = {"request-cookie": "abc"}  # type: ignore
        response = client.get("/", headers={"request-header": "1"})
        assert response.status_code == HTTP_200_OK
        assert len(caplog.messages) == 2

        assert caplog.messages[0] == "HTTP Request: path=/"
        assert caplog.messages[1] == "HTTP Response: status_code=200"


def test_logging_middleware_with_session_middleware(session_backend_config_memory: "ServerSideSessionConfig") -> None:
    # https://github.com/starlite-api/starlite/issues/1228

    @post("/")
    async def set_session(request: Request) -> None:
        request.set_session({"hello": "world"})

    @get("/")
    async def get_session() -> None:
        pass

    logging_middleware_config = LoggingMiddlewareConfig()

    with create_test_client(
        [set_session, get_session],
        logging_config=LoggingConfig(),
        middleware=[logging_middleware_config.middleware, session_backend_config_memory.middleware],
    ) as client:
        response = client.post("/")
        assert response.status_code == HTTP_201_CREATED
        assert "session" in client.cookies
        assert client.cookies["session"] != "*****"
        session_id = client.cookies["session"]

        response = client.get("/")
        assert response.status_code == HTTP_200_OK
        assert "session" in client.cookies
        assert client.cookies["session"] == session_id
