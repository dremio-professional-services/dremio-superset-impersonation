"""Superset DB_CONNECTION_MUTATOR for Dremio inbound impersonation.

Runs every Dremio Arrow Flight query as the logged-in Superset user via the
Dremio OAuth token exchange. In superset_config.py::

    from dremio_impersonation import dremio_impersonation_mutator
    DB_CONNECTION_MUTATOR = dremio_impersonation_mutator

Configuration (database URI, optional URI parameters, Dremio setup) is
described in README.md, design and enforced rules in ARCHITECTURE.md of
https://github.com/dremio-professional-services/dremio-superset-impersonation.
"""

from __future__ import annotations

import logging
import re
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any, NamedTuple

import requests
from sqlalchemy.engine.url import URL
from sqlalchemy.pool import NullPool
from sqlalchemy_dremio.db import Connection
from sqlalchemy_dremio.flight import DremioDialect_flight
from superset.db_engine_specs.base import GenericDBException

if TYPE_CHECKING:
    from superset.security import SupersetSecurityManager

try:  # the version of the installed wheel, taken from the git tag at build time
    __version__ = version("dremio-superset-impersonation")
except PackageNotFoundError:  # not installed as a package
    __version__ = "0.0.0+unknown"

__all__ = [
    "ImpersonationError",
    "TokenRequest",
    "check_token",
    "dremio_impersonation_mutator",
    "exchange_token",
]

logger = logging.getLogger(__name__)

DRIVERNAME = "dremio+flight"
DEFAULT_FLIGHT_PORT = 32010
DEFAULT_TOKEN_PORT = 9047
DEFAULT_TIMEOUT = 10.0
MAX_TIMEOUT = 300.0
MIN_TIMEOUT = 0.001
MAX_PORT = 65535
# create_engine() arguments that conflict with our creator/NullPool.
_REPLACED_PARAMS = frozenset(
    {
        "creator",
        "poolclass",
        "pool",
        "connect_args",
        "pool_size",
        "max_overflow",
        "pool_timeout",
        "pool_use_lifo",
    },
)
# Visible ASCII without ';' (the dialect's connection-string separator), bounded.
_TOKEN_RE = re.compile(r"[!-:<-~]{1,16384}")
_warned: set[tuple[str, str]] = set()


class ImpersonationError(GenericDBException):
    """The query must not run.

    Derives from Superset's GenericDBException (an alias of Exception in the
    tested Superset version), so Superset paths that tolerate unreachable
    databases, such as the catalog migration in ``superset db upgrade``, treat
    it as "cannot connect".
    """


class TokenRequest(NamedTuple):
    """Where and how to request the user's token."""

    url: str
    verify: bool | str
    timeout: float


def _warn_once(problem: str, host: str, message: str) -> None:
    """Log a warning once per process, problem and host (not once per query)."""
    if (problem, host) not in _warned:
        _warned.add((problem, host))
        logger.warning("Dremio impersonation for %s: %s", host, message)


def _number(
    query: dict[str, str],
    name: str,
    default: float,
    bounds: tuple[float, float],
) -> float:
    """Return a numeric URI parameter within ``bounds``, or the default."""
    raw = query.get(name.lower())
    if raw is None:
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    low, high = bounds
    if not low <= value <= high:
        msg = f"Dremio URI parameter {name} must be between {low:g} and {high:g}"
        raise ImpersonationError(msg)
    return value


def _single_values(url: URL) -> dict[str, str]:
    """Return the URI query as ``{name: value}``.

    Repeated identical values are accepted, also with different name case;
    conflicting repeats are rejected because they make TLS settings ambiguous.
    ';' is rejected because the dialect joins its connection string with ';'
    and would accept injected options.
    """
    result: dict[str, str] = {}
    seen: dict[str, str] = {}  # lower-case name -> value
    for name, value in url.query.items():
        values = value if isinstance(value, (tuple, list)) else (value,)
        for item in values:
            if not isinstance(item, str) or ";" in name or ";" in item:
                msg = f"Dremio URI has an invalid value for '{name}'"
                raise ImpersonationError(msg)
            if seen.setdefault(name.lower(), item) != item:
                msg = f"Dremio URI has conflicting values for '{name}'"
                raise ImpersonationError(msg)
        result[name] = values[0]
    return result


def _target_user(
    proxy_user: str,
    username: str | None,
    security_manager: SupersetSecurityManager,
) -> str:
    """Return the Dremio user to impersonate, or raise.

    ``username`` is Superset's current user (``g.user`` via ``get_username()``);
    it is None without a logged-in user (CLI, anonymous). With
    ``impersonate_user`` enabled, Superset falls back to or rewrites the URI
    user, so it equals the proxy or ``url.username`` and is rejected here.
    """
    if not username:
        msg = "Dremio impersonation requires a logged-in Superset user"
        raise ImpersonationError(msg)
    if security_manager.is_guest_user():
        msg = "Dremio impersonation does not support embedded guest users"
        raise ImpersonationError(msg)
    if username.lower() == proxy_user.lower():
        msg = f"'{username}' is not an allowed impersonation target"
        raise ImpersonationError(msg)
    return username


def _connection_string(url: URL, values: dict[str, str]) -> tuple[URL, str]:
    """Return the URL without credentials and the dialect's connection string."""
    if url.database and ";" in url.database:
        msg = "Dremio URI has an invalid database/schema name"
        raise ImpersonationError(msg)
    clean = URL.create(
        drivername=url.drivername,
        host=url.host,
        port=url.port or DEFAULT_FLIGHT_PORT,
        database=url.database,
        query=values,
    )
    (base,), _ = DremioDialect_flight().create_connect_args(clean)
    return clean, base


def _token_request(url: URL, query: dict[str, str]) -> TokenRequest:
    """Derive token endpoint, certificate verification and timeout from the URI."""
    tls = query.get("useencryption", "true").lower() != "false"
    no_verify = query.get("disablecertificateverification", "false").lower() == "true"
    verify: bool | str = query.get("trustedcerts") or not no_verify
    port = _number(query, "TokenPort", DEFAULT_TOKEN_PORT, (1, MAX_PORT))
    if not port.is_integer():
        msg = "Dremio URI parameter TokenPort must be an integer"
        raise ImpersonationError(msg)
    host = f"[{url.host}]" if ":" in (url.host or "") else url.host  # IPv6
    scheme = "https" if tls else "http"
    token_url = query.get("tokenurl") or f"{scheme}://{host}:{int(port)}/oauth/token"
    timeout = _number(
        query,
        "TokenTimeout",
        DEFAULT_TIMEOUT,
        (MIN_TIMEOUT, MAX_TIMEOUT),
    )
    return TokenRequest(token_url, verify, timeout)


def _warn_insecure(
    host: str,
    request: TokenRequest,
    query: dict[str, str],
    params: dict[str, Any],
) -> None:
    """Log insecure TLS settings and ignored engine parameters, once per host."""
    if not request.url.lower().startswith("https://"):
        _warn_once(
            "plaintext-token",
            host,
            "the proxy PAT is sent to the token endpoint WITHOUT TLS "
            "(UseEncryption=false or http TokenUrl). Use only for local tests.",
        )
    if query.get("useencryption", "true").lower() == "false":
        _warn_once(
            "plaintext-flight",
            host,
            "Arrow Flight connections are NOT encrypted (UseEncryption=false). "
            "Use only for local tests.",
        )
    if request.verify is False:
        _warn_once(
            "no-verify",
            host,
            "TLS certificate verification is DISABLED "
            "(DisableCertificateVerification=true); the proxy PAT can be "
            "intercepted. Use only for local tests.",
        )
    if params.get("connect_args"):
        _warn_once(
            "connect-args",
            host,
            "engine_params.connect_args are ignored; "
            "set connection options in the URI.",
        )


def check_token(token: object) -> str:
    """Return the token if it is safe to place into the Flight connection string.

    Args:
        token: Value of ``access_token`` from the token endpoint.

    Returns:
        The token.

    Raises:
        ImpersonationError: The token is not a string of 1 to 16384 visible ASCII
            characters without ';'.

    """
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        msg = "Dremio token exchange returned no usable token"
        raise ImpersonationError(msg)
    return token


def exchange_token(pat: str, username: str, request: TokenRequest) -> str:
    """Exchange the proxy PAT for a short-lived token of ``username``.

    Sends ``POST /oauth/token`` (Dremio token exchange) and does not follow
    redirects.

    Args:
        pat: Personal access token of the proxy user.
        username: Dremio user to impersonate.
        request: Token endpoint, certificate verification and timeout.

    Returns:
        The user's access token.

    Raises:
        ImpersonationError: The request failed, Dremio rejected it, or the
            response contains no usable token.

    """
    try:
        resp = requests.post(
            request.url,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "subject_token": username,
                "subject_token_type": "urn:ietf:params:oauth:token-type:dremio:subject",
                "actor_token": pat,
                "actor_token_type": (
                    "urn:ietf:params:oauth:token-type:dremio:personal-access-token"
                ),
                "scope": "dremio.all",
            },
            headers={"Accept": "application/json"},
            verify=request.verify,
            timeout=request.timeout,
            allow_redirects=False,
        )
    except (requests.RequestException, OSError) as exc:  # OSError: missing CA file
        msg = f"Dremio token exchange failed ({type(exc).__name__})"
        raise ImpersonationError(msg) from None
    if resp.status_code != requests.codes.ok:
        logger.warning(
            "Dremio token exchange for '%s' rejected (HTTP %s)",
            username,
            resp.status_code,
        )
        msg = (
            f"Dremio token exchange for '{username}' rejected (HTTP {resp.status_code})"
        )
        raise ImpersonationError(msg)
    try:
        token = resp.json()["access_token"]
    except (ValueError, KeyError, TypeError):
        token = None
    return check_token(token)


def dremio_impersonation_mutator(
    url: URL,
    params: dict[str, Any],
    username: str | None,
    security_manager: SupersetSecurityManager,
    _source: object,
) -> tuple[URL, dict[str, Any]]:
    """Impersonate the logged-in Superset user (``DB_CONNECTION_MUTATOR``).

    Args:
        url: SQLAlchemy URL of the Superset database (with proxy user and PAT).
        params: Keyword arguments Superset passes to ``create_engine()``.
        username: The logged-in Superset user, None without one.
        security_manager: Superset's security manager.
        _source: Superset's query source (unused).

    Returns:
        For ``dremio+flight``: the URL without credentials and
        ``create_engine()`` arguments with a connection factory that exchanges
        a token per connection, and NullPool. Other drivers: ``url`` and
        ``params`` unchanged.

    Raises:
        ImpersonationError: The URI or the user is not allowed. Token errors are
            raised when a connection is opened.

    """
    if url.drivername != DRIVERNAME:
        return url, params

    proxy_user, pat = url.username, url.password
    if not proxy_user or not pat:
        msg = "Dremio URI needs the proxy user and its PAT"
        raise ImpersonationError(msg)
    values = _single_values(url)
    query = {k.lower(): v for k, v in values.items()}
    if "token" in query:
        msg = "Dremio URI must not contain a Token"
        raise ImpersonationError(msg)

    target_user = _target_user(proxy_user, username, security_manager)
    clean_url, base = _connection_string(url, values)
    request = _token_request(clean_url, query)
    _warn_insecure(f"{clean_url.host}:{clean_url.port}", request, query, params)

    def creator() -> Connection:
        # Fresh exchange per connection: PAT rotation/revocation applies at once.
        token = exchange_token(pat, target_user, request)
        return Connection(f"{base};Token={token}")

    mutated = {k: v for k, v in params.items() if k not in _REPLACED_PARAMS}
    mutated.update(creator=creator, poolclass=NullPool)
    return clean_url, mutated
