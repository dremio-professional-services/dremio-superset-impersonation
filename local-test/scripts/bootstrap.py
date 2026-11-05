"""Seed the local test stack. Runs inside the Superset container.

Dremio (REST API): accept the EULA, admin, users alice/bob/charlie/admin, proxy user
superset_proxy, view demo.sales (SELECT for alice only), inbound impersonation
policy superset_proxy -> alice, bob, admin (charlie is deliberately not a
target), and a fresh proxy PAT.

Superset (Python API): users admin/alice/bob/charlie, a user named like the
proxy (must be rejected), and the database "Dremio" with
dremio+flight://superset_proxy:<PAT>@dremio:32010 (PAT = encrypted password).

Generated passwords and the PAT go to /state/credentials.json (host:
local-test/.state/secrets/, gitignored). Nothing secret is logged.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any

import requests
from superset.app import create_app
from superset.extensions import db, security_manager
from superset.utils.database import get_or_create_db

logger = logging.getLogger("bootstrap")

DREMIO = "https://dremio:9047"
CA = "/run/dremio/ca.pem"
STATE = Path("/state/credentials.json")
USERS = ["alice", "bob", "charlie"]
PROXY = "superset_proxy"
DREMIO_ADMIN = "dremio_admin"
TIMEOUT = 60
PAT_LIFETIME_MS = 7 * 24 * 3600 * 1000
Credentials = dict[str, str]
Json = dict[str, Any]


class BootstrapError(RuntimeError):
    """A seeding step failed."""


def new_password() -> str:
    """Return a random password that satisfies Dremio's password rules."""
    return secrets.token_urlsafe(16) + "aA1!"


def load_or_create_credentials() -> Credentials:
    """Load the generated credentials, or create them on the first run."""
    if STATE.exists():
        return json.loads(STATE.read_text())
    creds = {u: new_password() for u in [DREMIO_ADMIN, PROXY, *USERS, "admin"]}
    save(creds)
    return creds


def save(creds: Credentials) -> None:
    """Write the credentials, readable only by the owner."""
    STATE.touch(mode=0o600, exist_ok=True)
    STATE.write_text(json.dumps(creds))


class Dremio:
    """Minimal Dremio REST client for seeding."""

    def __init__(self) -> None:
        """Create an unauthenticated session that trusts the local test CA."""
        self.session = requests.Session()
        self.session.verify = CA

    def send(
        self,
        method: str,
        path: str,
        body: Json,
        headers: dict[str, str] | None = None,
    ) -> int:
        """Send a request without checking the status; return the HTTP status."""
        resp = self.session.request(
            method,
            DREMIO + path,
            json=body,
            headers=headers,
            timeout=TIMEOUT,
        )
        return resp.status_code

    def call(self, method: str, path: str, body: Json | None = None) -> Json:
        """Send a request that must succeed; return the JSON body."""
        resp = self.session.request(method, DREMIO + path, json=body, timeout=TIMEOUT)
        if resp.status_code not in (requests.codes.ok, requests.codes.no_content):
            msg = f"{method} {path} -> HTTP {resp.status_code}"
            raise BootstrapError(msg)
        return resp.json() if resp.content else {}

    def login(self, user: str, password: str) -> None:
        """Log in and use the session token for further requests."""
        body = {"userName": user, "password": password}
        token = self.call("POST", "/apiv2/login", body)["token"]
        self.session.headers["Authorization"] = f"_dremio{token}"

    def sql(self, statement: str) -> None:
        """Run a SQL statement and wait until it completed."""
        job = self.call("POST", "/api/v3/sql", {"sql": statement})["id"]
        while True:
            state = self.call("GET", f"/api/v3/job/{job}")["jobState"]
            if state == "COMPLETED":
                return
            if state in ("FAILED", "CANCELED"):
                msg = f"SQL failed: {statement[:60]}"
                raise BootstrapError(msg)
            time.sleep(0.5)


def seed_dremio(creds: Credentials) -> None:
    """Create Dremio users, test data, grants, the policy and a fresh PAT."""
    dremio = Dremio()
    # A fresh Dremio instance answers every request with "EULA has not been
    # accepted" until its EULA is accepted (idempotent).
    dremio.call("POST", "/apiv2/eula/accept")
    logger.info("dremio EULA accepted")
    status = dremio.send(
        "PUT",
        "/apiv2/bootstrap/firstuser",
        {
            "userName": DREMIO_ADMIN,
            "firstName": "Dremio",
            "lastName": "Admin",
            "email": "dremio_admin@example.com",
            "createdAt": int(time.time() * 1000),
            "password": creds[DREMIO_ADMIN],
        },
        headers={"Authorization": "_dremionull"},
    )
    logger.info("dremio first user: HTTP %s (400/409 = already done)", status)
    dremio.login(DREMIO_ADMIN, creds[DREMIO_ADMIN])

    # "admin" exists in Dremio (no data privileges) because Superset tests the
    # connection as the editing admin when a database is saved in the UI.
    for user in [PROXY, *USERS, "admin"]:
        body = {
            "name": user,
            "firstName": user,
            "lastName": "Test",
            "email": f"{user}@example.com",
            "password": creds[user],
        }
        status = dremio.send("POST", "/api/v3/user", body)
        logger.info("dremio user %s: HTTP %s (400 = exists)", user, status)
    status = dremio.send(
        "POST",
        "/api/v3/catalog",
        {"entityType": "space", "name": "demo"},
    )
    logger.info("dremio space demo: HTTP %s (409 = exists)", status)

    policy = (
        '[{proxy_principals:{users:["superset_proxy"]},'
        'target_principals:{users:["alice","bob","admin"]}}]'
    )
    for statement in [
        # PATs are disabled by default in Dremio Software.
        'ALTER SYSTEM SET "auth.personal-access-tokens.enabled" = true',
        "CREATE OR REPLACE VIEW demo.sales AS SELECT * FROM "
        "(VALUES ('EU', 100), ('US', 200)) AS t(region, amount)",
        "GRANT SELECT ON VIEW demo.sales TO USER alice",
        f"ALTER SYSTEM SET \"exec.impersonation.inbound_policies\" = '{policy}'",
    ]:
        dremio.sql(statement)
        logger.info("dremio sql ok: %s", statement.split(" AS ")[0][:60])

    # A fresh PAT on every run (the tests revoke PATs).
    proxy = Dremio()
    proxy.login(PROXY, creds[PROXY])
    uid = proxy.call("GET", f"/api/v3/user/by-name/{PROXY}")["id"]
    resp = proxy.session.post(
        f"{DREMIO}/api/v3/user/{uid}/token",
        json={"label": "superset-local-test", "millisecondsToExpire": PAT_LIFETIME_MS},
        timeout=TIMEOUT,
    )
    if resp.status_code != requests.codes.ok:
        msg = f"PAT creation failed: HTTP {resp.status_code}"
        raise BootstrapError(msg)
    creds["proxy_pat"] = resp.text.strip().strip('"')  # plain-text response
    save(creds)
    logger.info("dremio proxy PAT created")


def upsert_superset_user(username: str, password: str) -> None:
    """Create a Superset user with the Admin role, or reset its password."""
    user = security_manager.find_user(username=username)
    if user is None:
        security_manager.add_user(
            username=username,
            first_name=username,
            last_name="Test",
            email=f"{username}@example.com",
            role=security_manager.find_role("Admin"),
            password=password,
        )
    else:
        security_manager.reset_password(user.id, password)
    logger.info("superset user %s: ready", username)


def seed_superset(creds: Credentials) -> None:
    """Create the Superset users and the Dremio database."""
    for user in ["admin", *USERS]:
        upsert_superset_user(user, creds[user])
    # Superset user with the proxy's name: must be rejected (never impersonated).
    creds.setdefault("superset_proxy_ui", new_password())
    save(creds)
    upsert_superset_user(PROXY, creds["superset_proxy_ui"])
    pat = creds["proxy_pat"]
    uri = f"dremio+flight://{PROXY}:{pat}@dremio:32010/?TrustedCerts={CA}"
    get_or_create_db("Dremio", uri)
    db.session.commit()
    logger.info("superset database Dremio: ready")


def main() -> None:
    """Seed Dremio and Superset."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    creds = load_or_create_credentials()
    seed_dremio(creds)
    with create_app().app_context():
        seed_superset(creds)


if __name__ == "__main__":
    main()
