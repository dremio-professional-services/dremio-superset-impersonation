"""Browser tests: Superset web UI -> mutator -> Dremio, as a user would click it.

Each test checks one behavior, named by the test and its docstring.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from .helpers import (
    PROXY,
    SUPERSET,
    TIMEOUT_MS,
    dremio_ui_job_user,
    ensure_chart,
    ensure_dashboard,
    login,
    marker,
    new_page,
    new_proxy_pat,
    revoke_proxy_pats,
    run_sql,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

MASK = "XXXXXXXXXX"  # Superset keeps the stored password when the mask is saved
SECURE_TAIL = ":32010/?TrustedCerts=%2Frun%2Fdremio%2Fca.pem"
WHO_SQL = "SELECT QUERY_USER() AS who"
SALES_SQL = "SELECT region, amount FROM demo.sales ORDER BY region"
HTTP_UNAUTHORIZED = 401
EXPIRES_SOON_MS = 30_000
WAIT_FOR_EXPIRY_MS = 35_000
ORDERS = [
    pytest.param("alice", "bob", "a", id="alice-first"),
    pytest.param("bob", "alice", "b", id="bob-first"),
]


def cell(page: Page, value: str) -> Locator:
    """Return the last element whose whole text is ``value``."""
    return page.get_by_text(value, exact=True).last


def open_chart(page: Page, chart_id: int) -> None:
    """Open a saved chart in Explore."""
    page.goto(f"{SUPERSET}/explore/?slice_id={chart_id}")


def open_dashboard(page: Page, dashboard_id: int) -> None:
    """Open a dashboard."""
    page.goto(f"{SUPERSET}/superset/dashboard/{dashboard_id}/")


def who_am_i_chart(suffix: str) -> int:
    """Return the saved chart that shows QUERY_USER()."""
    return ensure_chart(f"who_am_i_{suffix}", WHO_SQL, ["who"])


def edit_database_uri(
    browser: Browser,
    c: dict[str, str],
    pat: str,
    *,
    uri_tail: str = SECURE_TAIL,
    saved: bool = True,
) -> None:
    """Edit the URI as admin in Settings > Database Connections > Edit > Finish.

    Superset tests the connection as the editing admin before it saves; the
    Superset admin is therefore an allowed (data-less) impersonation target.
    """
    page = login(browser, "admin", c["admin"])
    page.goto(f"{SUPERSET}/databaseview/list/")
    row = page.get_by_role("row").filter(has_text="Dremio")
    row.hover()
    row.locator("[data-test=database-edit]").click()
    uri = page.locator("[data-test=sqlalchemy-uri-input]")
    expect(uri).to_have_value(re.compile(rf"{PROXY}:{MASK}@"))  # PAT masked
    uri.fill(f"dremio+flight://{PROXY}:{pat}@dremio{uri_tail}")
    page.get_by_role("button", name="Finish").click()
    outcome = "Database settings updated" if saved else "unable to connect"
    expect(page.get_by_text(outcome).first).to_be_visible(timeout=TIMEOUT_MS)
    page.context.close()


def expect_query_user(page: Page, user: str) -> None:
    """Run QUERY_USER() in SQL Lab and expect ``user`` as result."""
    run_sql(page, WHO_SQL)
    expect(cell(page, user)).to_be_visible(timeout=TIMEOUT_MS)


def expect_token_rejected(page: Page) -> None:
    """Run a query in SQL Lab and expect the token exchange to be rejected."""
    run_sql(page, WHO_SQL)
    rejected = page.get_by_text("rejected (HTTP 401)").first
    expect(rejected).to_be_visible(timeout=TIMEOUT_MS)


@pytest.fixture
def restore_pat(browser: Browser, c: dict[str, str]) -> Iterator[None]:
    """After the test: store a fresh, valid PAT through the UI."""
    yield
    edit_database_uri(browser, c, new_proxy_pat())


@pytest.fixture
def restore_uri(browser: Browser, c: dict[str, str]) -> Iterator[None]:
    """After the test: restore the secure URI, keeping the stored PAT."""
    yield
    edit_database_uri(browser, c, MASK)


# --- Identity ---------------------------------------------------------------


@pytest.mark.parametrize("user", ["alice", "bob"])
def test_query_user_is_logged_in_user(
    browser: Browser,
    c: dict[str, str],
    user: str,
) -> None:
    """SQL Lab: QUERY_USER() returns the logged-in Superset user."""
    expect_query_user(login(browser, user, c[user]), user)


@pytest.mark.parametrize("user", ["alice", "bob"])
def test_dremio_job_owner_is_logged_in_user(
    browser: Browser,
    c: dict[str, str],
    user: str,
) -> None:
    """The job profile in the Dremio UI shows the logged-in user, not the proxy."""
    page = login(browser, user, c[user])
    m = marker()
    run_sql(page, f"SELECT '{m}' AS mark")
    expect(cell(page, m)).to_be_visible(timeout=TIMEOUT_MS)
    assert dremio_ui_job_user(browser, m) == user


# --- Dremio privileges --------------------------------------------------------


def test_permitted_user_reads_table(browser: Browser, c: dict[str, str]) -> None:
    """alice, who has SELECT on demo.sales in Dremio, sees its rows."""
    page = login(browser, "alice", c["alice"])
    run_sql(page, SALES_SQL)
    expect(cell(page, "EU")).to_be_visible(timeout=TIMEOUT_MS)
    expect(cell(page, "US")).to_be_visible()


def test_unpermitted_user_is_denied_by_dremio(
    browser: Browser,
    c: dict[str, str],
) -> None:
    """bob, who has no privileges on demo.sales, gets Dremio's access error."""
    page = login(browser, "bob", c["bob"])
    run_sql(page, SALES_SQL)
    denied = page.get_by_text("does not have access").first
    expect(denied).to_be_visible(timeout=TIMEOUT_MS)
    expect(cell(page, "EU")).to_have_count(0)


# --- Rejected identities ------------------------------------------------------


def test_target_not_in_policy_rejected(browser: Browser, c: dict[str, str]) -> None:
    """Charlie is not in Dremio's inbound policy: token exchange HTTP 401."""
    page = login(browser, "charlie", c["charlie"])
    run_sql(page, WHO_SQL)
    rejected = page.get_by_text("token exchange for 'charlie' rejected (HTTP 401)")
    expect(rejected.first).to_be_visible(timeout=TIMEOUT_MS)


def test_proxy_named_user_rejected(browser: Browser, c: dict[str, str]) -> None:
    """A Superset user with the proxy user's name is never impersonated."""
    page = login(browser, PROXY, c["superset_proxy_ui"])
    run_sql(page, WHO_SQL)
    rejected = page.get_by_text("is not an allowed impersonation target").first
    expect(rejected).to_be_visible(timeout=TIMEOUT_MS)


@pytest.mark.parametrize("target", ["sqllab", "explore"])
def test_anonymous_page_redirects_to_login(browser: Browser, target: str) -> None:
    """Without login, SQL Lab and a saved chart redirect to the login page."""
    if target == "sqllab":
        path = "/sqllab/"
    else:
        path = f"/explore/?slice_id={who_am_i_chart('a')}"
    page = new_page(browser, "anonymous")
    page.goto(SUPERSET + path)
    expect(page).to_have_url(re.compile(r".*/login/.*"))


def test_anonymous_chart_data_api_returns_401(browser: Browser) -> None:
    """Without login, the chart data API refuses before any query runs."""
    page = new_page(browser, "anonymous")
    resp = page.request.post(
        f"{SUPERSET}/api/v1/chart/data",
        data={
            "datasource": {"id": 1, "type": "table"},
            "queries": [{"columns": ["who"]}],
        },
    )
    assert resp.status == HTTP_UNAUTHORIZED


# --- Cache isolation (CACHE_QUERY_BY_USER) --------------------------------------


@pytest.mark.parametrize(("first", "second", "suffix"), ORDERS)
def test_explore_chart_shows_only_own_result(
    browser: Browser,
    c: dict[str, str],
    first: str,
    second: str,
    suffix: str,
) -> None:
    """Same saved chart in Explore, cache on: each user sees only their result."""
    chart = who_am_i_chart(suffix)
    pages = {u: login(browser, u, c[u]) for u in (first, second)}
    for user, other in ((first, second), (second, first), (first, second)):
        open_chart(pages[user], chart)
        expect(cell(pages[user], user)).to_be_visible(timeout=TIMEOUT_MS)
        expect(cell(pages[user], other)).to_have_count(0)


@pytest.mark.parametrize(("first", "second", "suffix"), ORDERS)
def test_dashboard_shows_only_own_result(
    browser: Browser,
    c: dict[str, str],
    first: str,
    second: str,
    suffix: str,
) -> None:
    """Same dashboard, cache on: each user sees only their own result."""
    dashboard = ensure_dashboard(f"Who am I {suffix}", who_am_i_chart(suffix))
    for user, other in ((first, second), (second, first)):
        page = login(browser, user, c[user])
        open_dashboard(page, dashboard)
        expect(cell(page, user)).to_be_visible(timeout=TIMEOUT_MS)
        expect(cell(page, other)).to_have_count(0)


def test_repeated_chart_is_served_from_cache(
    browser: Browser,
    c: dict[str, str],
) -> None:
    """Caching is active: opening the same chart again is answered from the cache."""
    chart = who_am_i_chart("a")
    page = login(browser, "alice", c["alice"])
    for _ in range(2):
        open_chart(page, chart)
        expect(cell(page, "alice")).to_be_visible(timeout=TIMEOUT_MS)
    expect(page.get_by_text("Cached", exact=True).first).to_be_visible()


@pytest.mark.parametrize("first", ["alice", "bob"], ids=["alice-first", "bob-first"])
def test_cached_rows_never_shown_to_unpermitted_user(
    browser: Browser,
    c: dict[str, str],
    first: str,
) -> None:
    """Alice's cached demo.sales rows are never shown to bob, in both orders."""
    chart = ensure_chart(f"sales_{first[0]}", SALES_SQL, ["region", "amount"])
    pages = {u: login(browser, u, c[u]) for u in ("alice", "bob")}
    order = ["alice", "bob", "alice"] if first == "alice" else ["bob", "alice", "bob"]
    for user in order:
        page = pages[user]
        open_chart(page, chart)
        if user == "alice":
            expect(cell(page, "EU")).to_be_visible(timeout=TIMEOUT_MS)
        else:
            denied = page.get_by_text("does not have access").first
            expect(denied).to_be_visible(timeout=TIMEOUT_MS)
            expect(cell(page, "EU")).to_have_count(0)


# --- PAT lifecycle through "Edit database" --------------------------------------


def test_invalid_pat_cannot_be_saved(browser: Browser, c: dict[str, str]) -> None:
    """Edit database refuses an invalid PAT (Superset's connection test fails)."""
    edit_database_uri(browser, c, "invalid-pat-value-1234567890", saved=False)


@pytest.mark.usefixtures("restore_pat")
def test_revoked_pat_fails_queries(browser: Browser, c: dict[str, str]) -> None:
    """After the stored PAT is revoked in Dremio, queries fail."""
    edit_database_uri(browser, c, new_proxy_pat())
    revoke_proxy_pats()
    expect_token_rejected(login(browser, "alice", c["alice"]))


@pytest.mark.usefixtures("restore_pat")
def test_expired_pat_fails_queries(browser: Browser, c: dict[str, str]) -> None:
    """After the stored PAT expires, queries fail."""
    edit_database_uri(browser, c, new_proxy_pat(ms_to_expire=EXPIRES_SOON_MS))
    page = login(browser, "alice", c["alice"])
    page.wait_for_timeout(WAIT_FOR_EXPIRY_MS)
    expect_token_rejected(page)


def test_rotated_pat_works_without_restart(browser: Browser, c: dict[str, str]) -> None:
    """A new PAT saved in Edit database is used at once; the old one is revoked."""
    revoke_proxy_pats()  # the old PAT no longer works
    edit_database_uri(browser, c, new_proxy_pat())
    expect_query_user(login(browser, "alice", c["alice"]), "alice")


# --- URI variants through "Edit database" ---------------------------------------


@pytest.mark.usefixtures("restore_uri")
@pytest.mark.parametrize(
    "uri_tail",
    [
        pytest.param(
            f"{SECURE_TAIL}&TrustedCerts=%2Frun%2Fdremio%2Fca.pem",
            id="repeated-identical-parameter",
        ),
        pytest.param(SECURE_TAIL.removeprefix(":32010"), id="no-port-default-32010"),
        pytest.param(
            ":32010/?DisableCertificateVerification=true",
            id="verification-disabled",
        ),
    ],
)
def test_uri_variant_works(browser: Browser, c: dict[str, str], uri_tail: str) -> None:
    """The URI variant can be saved, and queries run as the logged-in user."""
    edit_database_uri(browser, c, MASK, uri_tail=uri_tail)
    expect_query_user(login(browser, "alice", c["alice"]), "alice")


def test_conflicting_parameters_cannot_be_saved(
    browser: Browser,
    c: dict[str, str],
) -> None:
    """Edit database refuses two different TrustedCerts values."""
    conflicting = f"{SECURE_TAIL}&TrustedCerts=%2Fnonexistent%2Fother.pem"
    edit_database_uri(browser, c, MASK, uri_tail=conflicting, saved=False)
