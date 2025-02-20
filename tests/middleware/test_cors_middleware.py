from typing import Any, Dict, List, Mapping, Optional, cast

import pytest
from hypothesis import given, settings
from hypothesis.strategies import booleans, lists, none, one_of, sampled_from

from starlite import get
from starlite.config.cors import CORSConfig
from starlite.middleware.cors import CORSMiddleware
from starlite.status_codes import HTTP_200_OK, HTTP_404_NOT_FOUND
from starlite.testing import create_test_client


def test_setting_cors_middleware() -> None:
    cors_config = CORSConfig()  # pyright: ignore
    assert cors_config.allow_credentials is False
    assert cors_config.allow_headers == ["*"]
    assert cors_config.allow_methods == ["*"]
    assert cors_config.allow_origins == ["*"]
    assert cors_config.allow_origin_regex is None
    assert cors_config.max_age == 600
    assert cors_config.expose_headers == []

    with create_test_client(cors_config=cors_config) as client:
        unpacked_middleware = []
        cur = client.app.asgi_handler
        while hasattr(cur, "app"):
            unpacked_middleware.append(cur)
            cur = cast("Any", cur.app)
        else:
            unpacked_middleware.append(cur)
        assert len(unpacked_middleware) == 4
        cors_middleware = cast("Any", unpacked_middleware[1])
        assert isinstance(cors_middleware, CORSMiddleware)
        assert cors_middleware.config.allow_headers == ["*"]
        assert cors_middleware.config.allow_methods == ["*"]
        assert cors_middleware.config.allow_origins == cors_config.allow_origins
        assert cors_middleware.config.allow_origin_regex == cors_config.allow_origin_regex


@given(
    origin=one_of(none(), sampled_from(["http://www.example.com", "https://moishe.zuchmir.com"])),
    allow_origins=lists(sampled_from(["*", "http://www.example.com", "https://moishe.zuchmir.com"])),
    allow_credentials=booleans(),
    expose_headers=lists(sampled_from(["X-First-Header", "SomeOtherHeader", "X-Second-Header"])),
)
@settings(deadline=None)
def test_cors_simple_response(
    origin: Optional[str], allow_origins: List[str], allow_credentials: bool, expose_headers: List[str]
) -> None:
    @get("/")
    def handler() -> Dict[str, str]:
        return {"hello": "world"}

    cors_config = CORSConfig(
        allow_origins=allow_origins, allow_credentials=allow_credentials, expose_headers=expose_headers
    )

    with create_test_client(handler, cors_config=cors_config) as client:
        headers: Mapping[str, str] = {"Origin": origin} if origin else {}
        response = client.get("/", headers=headers)
        assert response.status_code == HTTP_200_OK
        assert response.json() == {"hello": "world"}
        assert cors_config.expose_headers == expose_headers
        assert cors_config.allow_origins == allow_origins
        assert cors_config.allow_credentials == allow_credentials

        if origin:
            if cors_config.is_allow_all_origins:
                assert response.headers.get("Access-Control-Allow-Origin") == "*"
            if cors_config.allow_credentials:
                assert response.headers.get("Access-Control-Allow-Credentials") == "true"
            if cors_config.expose_headers:
                assert response.headers.get("Access-Control-Expose-Headers") == ", ".join(
                    sorted(set(cors_config.expose_headers))
                )
        else:
            assert "Access-Control-Allow-Origin" not in response.headers
            assert "Access-Control-Allow-Credentials" not in response.headers
            assert "Access-Control-Expose-Headers" not in response.headers


@pytest.mark.parametrize("origin, should_apply_cors", (("http://www.example.com", True), (None, False)))
def test_cors_applied_on_exception_response_if_origin_is_present(
    origin: Optional[str], should_apply_cors: bool
) -> None:
    @get("/")
    def handler() -> Dict[str, str]:
        return {"hello": "world"}

    cors_config = CORSConfig(allow_origins=["http://www.example.com"])

    with create_test_client(handler, cors_config=cors_config) as client:
        headers: Mapping[str, str] = {"Origin": origin} if origin else {}
        response = client.get("/abc", headers=headers)
        assert response.status_code == HTTP_404_NOT_FOUND
        if should_apply_cors:
            assert response.headers.get("Access-Control-Allow-Origin") == origin
        else:
            assert not response.headers.get("Access-Control-Allow-Origin")
