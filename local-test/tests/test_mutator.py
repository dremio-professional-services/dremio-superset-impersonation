"""Tests of the mutator, called the way Superset calls it.

They cover what the browser cannot reach. They run inside the Superset container
against the real Superset app and the real Dremio, with the proxy PAT that is
currently stored (encrypted) in the Superset "Dremio" database. Responses the
real Dremio never gives (redirects, broken JSON) come from a local fake token
endpoint.

Each test checks one behavior, named by the test and its docstring.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import logging
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Any

import pytest
from flask import Flask, g
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from superset.extensions import db, security_manager
from superset.models.core import Database
from typing_extensions import override

import dremio_impersonation as di

if TYPE_CHECKING:
    from collections.abc import Iterator

CA = "/run/dremio/ca.pem"
SECURE = f"TrustedCerts={CA}"
OTHER_CA = "/nonexistent/other.pem"
LOGGER = "dremio_impersonation"
TIMEOUT_TEST_LIMIT_S = 5
TOO_LONG_TOKEN = "x" * 16385
ENGINE_PARAMS: dict[str, Any] = {
    "connect_args": {"x": 1},
    "pool_size": 5,
    "max_overflow": 2,
    "pool_timeout": 3,
    "pool_use_lifo": True,
    "pool_pre_ping": True,
    "echo": False,
}
REPLACED = {
    "connect_args",
    "pool_size",
    "max_overflow",
    "pool_timeout",
    "pool_use_lifo",
}
INSECURE = [
    pytest.param(
        "DisableCertificateVerification=true",
        "verification is DISABLED",
        id="no-verify",
    ),
    pytest.param(
        "UseEncryption=false",
        "Arrow Flight connections are NOT encrypted",
        id="plaintext-flight",
    ),
    pytest.param(
        f"{SECURE}&TokenUrl=http://dremio:9047/oauth/token",
        "WITHOUT TLS",
        id="plaintext-token-endpoint",
    ),
]


# --- Fixtures and helpers -------------------------------------------------------


@pytest.fixture(scope="session")
def stored_url(app: Flask) -> URL:
    """Return the URL with proxy user and current PAT, as Superset stores it."""
    with app.app_context():
        database = db.session.query(Database).filter_by(database_name="Dremio").one()
        return make_url(database.sqlalchemy_uri_decrypted)


@pytest.fixture(autouse=True)
def fresh_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the once-per-process warning memory for each test."""
    monkeypatch.setattr(di, "_warned", set())


def uri(base: URL, query: str, *, with_port: bool = True) -> URL:
    """Return a Dremio URL with the stored proxy credentials and ``query``."""
    port = f":{base.port}" if with_port else ""
    netloc = f"{base.username}:{base.password}@{base.host}{port}"
    return make_url(f"dremio+flight://{netloc}/?{query}")


def mutate(
    app: Flask,
    url: URL,
    user: str = "alice",
    params: dict[str, Any] | None = None,
) -> tuple[URL, dict[str, Any]]:
    """Call the mutator as Superset does for the logged-in ``user``."""
    with app.test_request_context():
        g.user = security_manager.find_user(user)
        engine_params = params if params is not None else {"connect_args": {}}
        return di.dremio_impersonation_mutator(
            url,
            engine_params,
            user,
            security_manager,
            None,
        )


def query_user(url: URL, params: dict[str, Any]) -> str:
    """Connect with the mutated arguments and return QUERY_USER()."""
    with create_engine(url, **params).connect() as conn:
        return conn.execute(text("SELECT QUERY_USER()")).scalar()


def connect_error(url: URL, params: dict[str, Any]) -> str:
    """Connect, expect an ImpersonationError and return its text."""
    with pytest.raises(di.ImpersonationError) as exc:
        query_user(url, params)
    return str(exc.value)


def connect_through_superset(database: Database) -> None:
    """Open a connection through Superset's own engine creation."""
    with database.get_sqla_engine() as engine:
        engine.connect()


def always_guest(*_args: object, **_kwargs: object) -> bool:
    """Pretend the current user is an embedded guest user."""
    return True


class FakeTokenEndpoint:
    """Local HTTP server with a fixed answer that records what it receives."""

    def __init__(
        self,
        status: int,
        body: bytes,
        headers: dict[str, str],
        *,
        ipv6: bool = False,
    ) -> None:
        """Start the server on a free port of the loopback interface."""
        self.requests: list[dict[str, list[str]]] = []
        received = self.requests

        class Handler(BaseHTTPRequestHandler):
            @override
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                received.append(urllib.parse.parse_qs(self.rfile.read(length).decode()))
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            @override
            def log_message(self, _fmt: str, *_args: object) -> None:
                """Keep the test output quiet."""

        class Server(HTTPServer):
            address_family = socket.AF_INET6 if ipv6 else socket.AF_INET

        self.server = Server(("::1" if ipv6 else "127.0.0.1", 0), Handler)
        self.port: int = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self) -> str:
        """Return the token URL of this endpoint (plain HTTP)."""
        return f"http://127.0.0.1:{self.port}/oauth/token"

    def close(self) -> None:
        """Stop the server."""
        self.server.shutdown()
        self.server.server_close()


class Endpoints:
    """Starts fake token endpoints and stops them after the test."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self.started: list[FakeTokenEndpoint] = []

    def start(
        self,
        status: int,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        *,
        ipv6: bool = False,
    ) -> FakeTokenEndpoint:
        """Start an endpoint that answers every POST with ``status`` and ``body``."""
        endpoint = FakeTokenEndpoint(status, body, headers or {}, ipv6=ipv6)
        self.started.append(endpoint)
        return endpoint


@pytest.fixture
def endpoints() -> Iterator[Endpoints]:
    """Provide fake token endpoints for the test and stop them afterwards."""
    registry = Endpoints()
    yield registry
    for endpoint in registry.started:
        endpoint.close()


# --- Identity -------------------------------------------------------------------


def test_no_user_context_fails(app: Flask) -> None:
    """Superset app context without a logged-in user (as in CLI): rejected."""
    with app.app_context():
        database = db.session.query(Database).filter_by(database_name="Dremio").one()
        with pytest.raises(di.ImpersonationError, match="logged-in Superset user"):
            connect_through_superset(database)


def test_other_driver_passes_through(app: Flask) -> None:
    """Non-Dremio databases are returned unchanged."""
    url = make_url("postgresql://user:secret@db:5432/x")
    params = {"connect_args": {"sslmode": "require"}, "pool_size": 5}
    assert mutate(app, url, params=params) == (url, params)


@pytest.mark.parametrize(
    "raw",
    [
        "dremio+flight://dremio:32010/",
        "dremio+flight://superset_proxy@dremio:32010/",
        "dremio+flight://:secret@dremio:32010/",
    ],
)
def test_missing_proxy_credentials_rejected(app: Flask, raw: str) -> None:
    """A URI without proxy user or PAT is rejected."""
    with pytest.raises(di.ImpersonationError, match="needs the proxy user and its PAT"):
        mutate(app, make_url(raw))


@pytest.mark.parametrize("param", ["Token=abc", "TOKEN=abc", "token=abc"])
def test_token_in_uri_rejected(app: Flask, stored_url: URL, param: str) -> None:
    """A static Token in the URI (any case) is rejected; it bypasses impersonation."""
    with pytest.raises(di.ImpersonationError, match="must not contain a Token"):
        mutate(app, uri(stored_url, f"{SECURE}&{param}"))


def test_guest_user_rejected(
    app: Flask,
    stored_url: URL,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Embedded guest users are rejected (EMBEDDED_SUPERSET is off, so patched)."""
    monkeypatch.setattr(security_manager, "is_guest_user", always_guest)
    with pytest.raises(di.ImpersonationError, match="embedded guest users"):
        mutate(app, uri(stored_url, SECURE))


@pytest.mark.parametrize("name", ["superset_proxy", "Superset_Proxy", "SUPERSET_PROXY"])
def test_proxy_name_rejected_any_case(app: Flask, stored_url: URL, name: str) -> None:
    """The proxy user name is rejected as target in any letter case."""
    url = uri(stored_url, SECURE)
    with (
        app.test_request_context(),
        pytest.raises(
            di.ImpersonationError,
            match="not an allowed impersonation target",
        ),
    ):
        di.dremio_impersonation_mutator(url, {}, name, security_manager, None)


def test_impersonate_user_flag_rejected(app: Flask) -> None:
    """With 'Impersonate logged in user' on, Superset's rewritten URI is rejected."""
    with app.test_request_context():
        g.user = security_manager.find_user("alice")
        database = db.session.query(Database).filter_by(database_name="Dremio").one()
        database.impersonate_user = True  # not committed
        try:
            with pytest.raises(
                di.ImpersonationError,
                match="'alice' is not an allowed",
            ):
                connect_through_superset(database)
        finally:
            db.session.rollback()


# --- Token handling ---------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        "",
        "a b",
        "a;b",
        "a\nb",
        "a\tb",
        "ä",
        pytest.param(TOO_LONG_TOKEN, id="too-long"),
        None,
        42,
    ],
)
def test_bad_tokens_rejected(token: object) -> None:
    """Tokens that are unsafe for the Flight connection string are rejected."""
    with pytest.raises(di.ImpersonationError, match="no usable token"):
        di.check_token(token)


def test_real_dremio_token_accepted(app: Flask, stored_url: URL) -> None:
    """A real Dremio token passes the check; the query runs as alice."""
    url, params = mutate(app, uri(stored_url, SECURE))
    assert query_user(url, params) == "alice"


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b"{}",
        b'{"access_token": null}',
        b'{"access_token": 42}',
        b'{"access_token": ""}',
        b'{"access_token": "a b"}',
        b'{"access_token": "a;Token=evil"}',
    ],
)
def test_unusable_token_response_rejected(
    app: Flask,
    stored_url: URL,
    endpoints: Endpoints,
    body: bytes,
) -> None:
    """HTTP 200 without a usable token is rejected."""
    endpoint = endpoints.start(200, body, {"Content-Type": "application/json"})
    url, params = mutate(
        app,
        uri(stored_url, f"TokenUrl={endpoint.url()}&UseEncryption=false"),
    )
    assert "no usable token" in connect_error(url, params)


def test_redirect_not_followed(
    app: Flask,
    stored_url: URL,
    endpoints: Endpoints,
) -> None:
    """A redirect from the token endpoint is not followed; its target gets no PAT."""
    target = endpoints.start(200, b'{"access_token":"x"}')
    redirect = endpoints.start(302, headers={"Location": target.url()})
    url, params = mutate(
        app,
        uri(stored_url, f"TokenUrl={redirect.url()}&UseEncryption=false"),
    )
    connect_error(url, params)
    assert target.requests == []


def test_redirect_reported_as_rejected(
    app: Flask,
    stored_url: URL,
    endpoints: Endpoints,
) -> None:
    """A redirect from the token endpoint is reported as rejected exchange."""
    redirect = endpoints.start(302, headers={"Location": "http://127.0.0.1:1/"})
    url, params = mutate(
        app,
        uri(stored_url, f"TokenUrl={redirect.url()}&UseEncryption=false"),
    )
    assert "rejected (HTTP 302)" in connect_error(url, params)


# --- URI parameters ----------------------------------------------------------------


def test_repeated_identical_params_work(app: Flask, stored_url: URL) -> None:
    """A query parameter repeated with the same value is accepted."""
    url, params = mutate(app, uri(stored_url, f"{SECURE}&{SECURE}"))
    assert query_user(url, params) == "alice"


def test_repeated_conflicting_params_rejected(app: Flask, stored_url: URL) -> None:
    """A query parameter repeated with different values is rejected."""
    query = f"{SECURE}&TrustedCerts={OTHER_CA}"
    with pytest.raises(
        di.ImpersonationError,
        match="conflicting values for 'TrustedCerts'",
    ):
        mutate(app, uri(stored_url, query))


def test_conflict_detected_across_name_case(app: Flask, stored_url: URL) -> None:
    """TrustedCerts and trustedcerts with different values are a conflict."""
    query = f"{SECURE}&trustedcerts={OTHER_CA}"
    with pytest.raises(di.ImpersonationError, match="conflicting values"):
        mutate(app, uri(stored_url, query))


def test_same_value_across_name_case_works(app: Flask, stored_url: URL) -> None:
    """TrustedCerts and trustedcerts with the same value are accepted."""
    url, params = mutate(app, uri(stored_url, f"{SECURE}&trustedcerts={CA}"))
    assert query_user(url, params) == "alice"


@pytest.mark.parametrize(
    "tail",
    [
        pytest.param(f"/x;UseEncryption=false?{SECURE}", id="schema"),
        pytest.param(
            f"/?{SECURE}&routing_queue=q;DisableCertificateVerification=true",
            id="value",
        ),
        pytest.param(
            f"/?{SECURE}%3BDisableCertificateVerification%3Dtrue",
            id="encoded-value",
        ),
    ],
)
def test_semicolon_injection_rejected(app: Flask, stored_url: URL, tail: str) -> None:
    """';' in the schema or a parameter cannot inject connection options."""
    credentials = f"{stored_url.username}:{stored_url.password}"
    raw = f"dremio+flight://{credentials}@dremio:32010{tail}"
    with pytest.raises(di.ImpersonationError, match="invalid"):
        mutate(app, make_url(raw))


def test_missing_port_uses_default(app: Flask, stored_url: URL) -> None:
    """Without a port in the URI, Flight uses the Dremio default 32010."""
    url, _ = mutate(app, uri(stored_url, SECURE, with_port=False))
    assert url.port == di.DEFAULT_FLIGHT_PORT


def test_missing_port_connects(app: Flask, stored_url: URL) -> None:
    """Without a port in the URI, the query runs as alice."""
    url, params = mutate(app, uri(stored_url, SECURE, with_port=False))
    assert query_user(url, params) == "alice"


def test_ipv6_host_token_url(app: Flask, stored_url: URL, endpoints: Endpoints) -> None:
    """IPv6 literal hosts produce a valid token URL (the request arrives)."""
    try:
        endpoint = endpoints.start(401, ipv6=True)
    except OSError:
        pytest.skip("no IPv6 loopback in this container")
    credentials = f"{stored_url.username}:{stored_url.password}"
    raw = (
        f"dremio+flight://{credentials}@[::1]:32010/"
        f"?UseEncryption=false&TokenPort={endpoint.port}"
    )
    url, params = mutate(app, make_url(raw))
    connect_error(url, params)
    assert len(endpoint.requests) == 1


def test_missing_ca_file(app: Flask, stored_url: URL) -> None:
    """A missing TrustedCerts file is reported as ImpersonationError."""
    url, params = mutate(app, uri(stored_url, "TrustedCerts=/nonexistent/ca.pem"))
    assert "token exchange failed (OSError)" in connect_error(url, params)


def test_token_port_parameter_is_used(app: Flask, stored_url: URL) -> None:
    """TokenPort selects the token endpoint port (nothing listens on 9999)."""
    url, params = mutate(app, uri(stored_url, f"{SECURE}&TokenPort=9999"))
    assert "token exchange failed" in connect_error(url, params)


def test_token_timeout_parameter_is_used(app: Flask, stored_url: URL) -> None:
    """TokenTimeout bounds the token request (listener that never answers)."""
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen(1)
        port = silent.getsockname()[1]
        token_url = f"https://127.0.0.1:{port}/oauth/token"
        query = f"{SECURE}&TokenTimeout=1&TokenUrl={token_url}"
        url, params = mutate(app, uri(stored_url, query))
        start = time.monotonic()
        connect_error(url, params)
        assert time.monotonic() - start < TIMEOUT_TEST_LIMIT_S


@pytest.mark.parametrize(
    "param",
    [
        "TokenPort=0",
        "TokenPort=65536",
        "TokenPort=abc",
        "TokenPort=443.5",
        "TokenTimeout=0",
        "TokenTimeout=-1",
        "TokenTimeout=301",
        "TokenTimeout=abc",
    ],
)
def test_invalid_token_parameters_rejected(
    app: Flask,
    stored_url: URL,
    param: str,
) -> None:
    """Out-of-range or non-numeric TokenPort/TokenTimeout values are rejected."""
    name = param.split("=", maxsplit=1)[0]
    with pytest.raises(di.ImpersonationError, match=f"{name} must be"):
        mutate(app, uri(stored_url, f"{SECURE}&{param}"))


# --- Engine parameters ------------------------------------------------------------


def test_other_engine_params_passed_through(app: Flask, stored_url: URL) -> None:
    """Engine parameters the mutator does not replace reach create_engine()."""
    _, params = mutate(app, uri(stored_url, SECURE), params=dict(ENGINE_PARAMS))
    assert (params["pool_pre_ping"], params["echo"]) == (True, False)


def test_conflicting_pool_params_removed(app: Flask, stored_url: URL) -> None:
    """connect_args and QueuePool settings are removed."""
    _, params = mutate(app, uri(stored_url, SECURE), params=dict(ENGINE_PARAMS))
    assert not REPLACED & params.keys()


def test_nullpool_and_creator_set(app: Flask, stored_url: URL) -> None:
    """The mutated arguments use NullPool and the mutator's connection factory."""
    _, params = mutate(app, uri(stored_url, SECURE), params=dict(ENGINE_PARAMS))
    assert (params["poolclass"].__name__, callable(params["creator"])) == (
        "NullPool",
        True,
    )


def test_ignored_connect_args_logged(
    app: Flask,
    stored_url: URL,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-empty connect_args are logged as ignored."""
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        mutate(app, uri(stored_url, SECURE), params=dict(ENGINE_PARAMS))
    assert "connect_args are ignored" in caplog.text


def test_engine_created_from_mutated_params(app: Flask, stored_url: URL) -> None:
    """create_engine() accepts the mutated parameters; the query runs as alice."""
    url, params = mutate(app, uri(stored_url, SECURE), params=dict(ENGINE_PARAMS))
    assert query_user(url, params) == "alice"


# --- Insecure TLS settings: allowed, but logged ---------------------------------


def test_disabled_verification_works(app: Flask, stored_url: URL) -> None:
    """With DisableCertificateVerification=true, queries still run as the user."""
    url, params = mutate(app, uri(stored_url, "DisableCertificateVerification=true"))
    assert query_user(url, params) == "alice"


@pytest.mark.parametrize(("query", "message"), INSECURE)
def test_insecure_setting_logged_as_warning(
    app: Flask,
    stored_url: URL,
    caplog: pytest.LogCaptureFixture,
    query: str,
    message: str,
) -> None:
    """Each insecure TLS setting logs a WARNING from logger dremio_impersonation."""
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        mutate(app, uri(stored_url, query))
    records = [r for r in caplog.records if message in r.getMessage()]
    assert [(r.name, r.levelname) for r in records] == [(LOGGER, "WARNING")]


@pytest.mark.parametrize(("query", "message"), INSECURE)
def test_insecure_setting_logged_once_per_host(
    app: Flask,
    stored_url: URL,
    caplog: pytest.LogCaptureFixture,
    query: str,
    message: str,
) -> None:
    """Repeated queries with the same insecure setting log the warning only once."""
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for _ in range(3):
            mutate(app, uri(stored_url, query))
    assert caplog.text.count(message) == 1


@pytest.mark.parametrize(("query", "message"), INSECURE)
def test_insecure_setting_warning_contains_no_pat(
    app: Flask,
    stored_url: URL,
    caplog: pytest.LogCaptureFixture,
    query: str,
    message: str,
) -> None:
    """Warnings about insecure settings never contain the PAT."""
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        mutate(app, uri(stored_url, query))
    assert (message in caplog.text, stored_url.password in caplog.text) == (True, False)


def test_secure_settings_log_nothing(
    app: Flask,
    stored_url: URL,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A secure URI produces no warnings."""
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        mutate(app, uri(stored_url, SECURE))
    assert not caplog.text


# --- No PAT in error texts ----------------------------------------------------------


def failing_setup(kind: str, endpoints: Endpoints) -> tuple[str, str]:
    """Return (URI query, user) for a token exchange that fails in a given way."""
    if kind == "redirect":
        redirect = endpoints.start(302, headers={"Location": "http://127.0.0.1:1/"})
        return f"TokenUrl={redirect.url()}&UseEncryption=false", "alice"
    if kind == "broken-json":
        broken = endpoints.start(200, b"not json")
        return f"TokenUrl={broken.url()}&UseEncryption=false", "alice"
    if kind == "missing-ca-file":
        return "TrustedCerts=/nonexistent/ca.pem", "alice"
    if kind == "real-dremio-401":
        return SECURE, "charlie"  # not in Dremio's inbound policy
    return f"{SECURE}&TokenPort=9999", "alice"  # unreachable port


@pytest.mark.parametrize(
    "kind",
    [
        "unreachable-port",
        "missing-ca-file",
        "redirect",
        "broken-json",
        "real-dremio-401",
    ],
)
def test_connect_error_contains_no_pat(
    app: Flask,
    stored_url: URL,
    endpoints: Endpoints,
    kind: str,
) -> None:
    """A failing token exchange produces an error text without the PAT."""
    query, user = failing_setup(kind, endpoints)
    url, params = mutate(app, uri(stored_url, query), user=user)
    assert stored_url.password not in connect_error(url, params)


@pytest.mark.parametrize(
    "query",
    [
        pytest.param(f"{SECURE}&Token=x", id="token-in-uri"),
        pytest.param(f"{SECURE}&TokenPort=0", id="invalid-token-port"),
        pytest.param(f"{SECURE}&TrustedCerts={OTHER_CA}", id="conflicting-parameters"),
    ],
)
def test_uri_error_contains_no_pat(app: Flask, stored_url: URL, query: str) -> None:
    """A rejected URI produces an error text without the PAT."""
    with pytest.raises(di.ImpersonationError) as exc:
        mutate(app, uri(stored_url, query))
    assert stored_url.password not in str(exc.value)


# --- Version ----------------------------------------------------------------------


def test_version_fallback_without_package_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without package metadata (not installed as wheel) the version is a fallback."""

    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    # Load a separate copy, so the module used by the other tests stays unchanged.
    spec = importlib.util.spec_from_file_location("di_copy", di.__file__)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.__version__ == "0.0.0+unknown"
