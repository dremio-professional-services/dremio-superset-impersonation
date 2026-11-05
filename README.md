# Dremio impersonation for Apache Superset

A Superset `DB_CONNECTION_MUTATOR` (one module, [`dremio_impersonation.py`](dremio_impersonation.py),
shipped as a wheel) that runs every Dremio Arrow Flight query as the
**logged-in Superset user**.
Superset connects with a technical proxy user's personal access token (PAT);
for each connection the mutator exchanges that PAT plus the Superset username
for a short-lived Dremio token of that user (Dremio OAuth token exchange,
[inbound impersonation](https://docs.dremio.com/current/security/rbac/inbound-impersonation/)).
Dremio then enforces the privileges of that user.

How it works and why it is built this way: [ARCHITECTURE.md](ARCHITECTURE.md).
The mutator is a workaround until `sqlalchemy-dremio` supports Dremio
impersonation itself; it is therefore published on GitHub only, not on PyPI.

## Disclaimer

> [!CAUTION]
> *All scripts/code are run at your own risk, and while they have been written with the intention of minimizing the potential for unintended consequences, Dremio will not be responsible for any errors or problems. The scripts/code are provided by Dremio "as is" and any express or implied warranties are disclaimed by Dremio. In no event will Dremio be liable for any direct, indirect, incidental, special, exemplary, or consequential damages, or any loss of use or data, however caused and on any theory of liability, arising in any way out of the use of the scripts/code, even if advised of the possibility of such damage.*

## Required: per-user cache keys

> [!IMPORTANT]
> **The mutator alone is not enough.** Superset must also put the username into
> its cache keys, otherwise one user's cached chart results are shown to other
> users.

Superset caches chart results by query, not by user. With impersonation the
same query returns different data per user, and a cache hit never reaches the
mutator. Enable per-user cache keys with **one** of these settings
([why](ARCHITECTURE.md#why-per-user-cache-keys-are-required)):

| Setting | Where | Scope |
|---|---|---|
| `FEATURE_FLAGS = {"CACHE_QUERY_BY_USER": True}` | `superset_config.py` | all databases |
| *Per user caching* (stored as `"per_user_caching": true` in the database's `extra`) | Superset UI: *Settings > Database Connections > Edit > Advanced > Security* | this database only |

Also leave *Schema cache timeout* and *Table cache timeout* (Edit database >
Advanced > Performance) unset: schema and table lists are cached per database,
not per user.

## Requirements and tested versions

| Component | Version |
|---|---|
| Apache Superset (image `apache/superset`) | 6.1.0 |
| sqlalchemy-dremio | 3.0.4 |
| Dremio Enterprise (image `quay.io/dremio/dremio-enterprise`) | 26.1.12 |

This is the only list of tested versions; the local test environment pins the
same versions in [`local-test/superset/Dockerfile`](local-test/superset/Dockerfile)
and [`local-test/compose.yaml`](local-test/compose.yaml). `sqlalchemy-dremio`
3.0.5 cannot be installed next to Superset 6.1.0 (it requires SQLAlchemy 2.0 and
pyarrow 20). The mutator relies on internals of the dialect; see
[ARCHITECTURE.md](ARCHITECTURE.md#dependencies-on-library-internals).

## Installation

1. Install the wheel from a [GitHub release](https://github.com/dremio-professional-services/dremio-superset-impersonation/releases) into the Python
   environment Superset runs in (for example in your Superset image, or the
   Helm chart's `bootstrapScript`). It brings the tested `sqlalchemy-dremio`
   version as a dependency:

   ```sh
   pip install https://github.com/dremio-professional-services/dremio-superset-impersonation/releases/download/vX.Y.Z/dremio_superset_impersonation-X.Y.Z-py3-none-any.whl
   ```

   Optionally verify where the wheel was built:
   `gh attestation verify <wheel> --repo dremio-professional-services/dremio-superset-impersonation`.
2. In `superset_config.py`, register the mutator **and** enable per-user cache
   keys (both are required, see [above](#required-per-user-cache-keys)):

```python
from dremio_impersonation import dremio_impersonation_mutator

DB_CONNECTION_MUTATOR = dremio_impersonation_mutator

# REQUIRED: username in every chart-data cache key. Without it, cached results
# of one user are shown to other users. Alternative for the Dremio database
# only: Edit database > Advanced > Security > "Per user caching".
FEATURE_FLAGS = {"CACHE_QUERY_BY_USER": True}
```

See [`local-test/superset/Dockerfile`](local-test/superset/Dockerfile) for an image that
installs the wheel.
Development and tests: [CONTRIBUTING.md](CONTRIBUTING.md).

## Configuration

Everything per database is the normal Superset database configuration:

```
dremio+flight://<proxy_user>:<proxy_PAT>@<dremio-host>[:<flight_port>]/?TrustedCerts=/path/to/ca.pem
```

- The PAT is the database password, so Superset stores it encrypted with
  `SECRET_KEY` and masks it in the UI and API.
- URL-encode special characters in the PAT if you write the URI by hand.
- The Flight port defaults to `32010` when the URI has none.
- TLS follows the `sqlalchemy-dremio` URI options `UseEncryption`,
  `DisableCertificateVerification` and `TrustedCerts` (default: TLS with
  certificate verification). The token endpoint uses the same settings.
- The token endpoint is `https://<dremio-host>:9047/oauth/token`; change it
  with the URI parameters below.
- The mutator rejects ambiguous or unsafe URIs (for example conflicting
  repeated parameters or `;` in values); see the
  [rules](ARCHITECTURE.md#rules-enforced-by-the-mutator).
- *Engine Parameters* (Edit database > Advanced > Other) are passed to
  SQLAlchemy, except `connect_args` and pool settings (`pool_size`,
  `max_overflow`, `pool_timeout`, `pool_use_lifo`): the mutator always uses
  its own connection factory and `NullPool`. Non-empty `connect_args` are
  logged as ignored; set connection options in the URI.
- Leave *Impersonate logged in user* (Edit database > Advanced > Security)
  **off**. If it is on, Superset rewrites the URI username, and the mutator
  rejects every query.
- Enable per-user cache keys (see [above](#required-per-user-cache-keys)).

Optional URI parameters of the mutator (the dialect ignores them):

| Parameter | Default | Meaning |
|---|---|---|
| `TokenPort` | `9047` | Dremio web/REST port for `/oauth/token`. Dremio serves HTTPS on this port when TLS is enabled (Helm chart: `coordinator.web.tls.enabled`, default `false`); behind an ingress or load balancer it is often `443`. |
| `TokenUrl` | – | full token endpoint, for example `https://dremio.example.com/oauth/token`; overrides host and `TokenPort` |
| `TokenTimeout` | `10` | timeout of the token request in seconds (0.001 to 300) |

Example: `dremio+flight://superset_proxy:<PAT>@dremio.example.com:32010/?TrustedCerts=/certs/ca.pem&TokenPort=443&TokenTimeout=5`

The token request uses the Python `requests` defaults for the environment:
`HTTPS_PROXY`/`NO_PROXY`, `REQUESTS_CA_BUNDLE` and `~/.netrc` of the Superset
process apply. Exclude the Dremio host from proxies with `NO_PROXY` if the
token endpoint must be reached directly.

Insecure settings (`UseEncryption=false`, `DisableCertificateVerification=true`,
an `http://` `TokenUrl`) work, for example for local tests, but are logged as
warnings ([details](ARCHITECTURE.md#transport-security-and-logging)).

Dremio setup (see Dremio's documentation on
[personal access tokens](https://docs.dremio.com/current/security/authentication/personal-access-tokens/)
and [inbound impersonation](https://docs.dremio.com/current/security/rbac/inbound-impersonation/)):

```sql
ALTER SYSTEM SET "auth.personal-access-tokens.enabled" = true;
ALTER SYSTEM SET "exec.impersonation.inbound_policies" =
  '[{proxy_principals:{users:["superset_proxy"]},target_principals:{groups:["bi_users"]}}]';
```

Target principals can be users or external (LDAP or identity provider)
groups; Dremio does not support internal roles in inbound impersonation
policies ([inbound impersonation](https://docs.dremio.com/current/security/rbac/inbound-impersonation/)).

**Give the proxy user no data privileges of its own**: if a Superset node runs
without the mutator, the dialect logs in with the URI credentials as the proxy
user, and it must then see nothing.

The proxy must be a regular local or LDAP user that owns a PAT. A Dremio
service user cannot be the proxy: it cannot own a PAT, and the token exchange
accepts only a PAT as `actor_token`
([details](ARCHITECTURE.md#why-the-proxy-must-be-a-regular-user-not-a-service-user)).

Superset and Dremio usernames must be identical. Superset's
`IMPERSONATE_WITH_EMAIL_PREFIX` feature flag passes the email prefix instead of
the username to the mutator; this option is not covered by the tests.

## PAT rotation

Rotate the PAT before it expires, at the latest every 180 days, the maximum
PAT lifetime in Dremio
([personal access tokens](https://docs.dremio.com/current/security/authentication/personal-access-tokens/));
Dremio rejects longer lifetimes.

1. As the proxy user, create a new PAT in Dremio.
2. Replace the password of the Superset database (UI: *Settings > Database
   Connections > Edit*, or `superset set-database-uri`). New connections use it
   at once; no restart. When saving in the UI, Superset tests the connection
   **as the editing admin**, so that Superset admin must also be an allowed
   impersonation target in Dremio (it needs no data privileges). An invalid
   PAT cannot be saved in the UI.
3. Revoke the old PAT in Dremio.

The flow is shown in [ARCHITECTURE.md](ARCHITECTURE.md#pat-rotation-flow).

## Testing

Tested end to end against real Superset and Dremio Enterprise instances in
Docker, through their web UIs. What is tested and how to run it:
[CONTRIBUTING.md](CONTRIBUTING.md#tests).

## Known limitations

- Superset admins who can edit or test a database connection can point its
  host or `TokenUrl` to another server: Superset keeps the stored PAT when the
  URI contains the masked password, and sends it there. Grant database edit
  rights only to trusted admins.
- One token exchange per new connection (no token cache, connections are never
  pooled): simple and revocation-safe, at the cost of one extra HTTP call per
  query ([why](ARCHITECTURE.md#why-connections-are-never-pooled-nullpool)).
- Background jobs (reports, alerts, thumbnails, async queries) run as the user
  Superset assigns; they are not covered by the tests
  ([details](ARCHITECTURE.md#background-and-async-execution)).

## Security

See [SECURITY.md](SECURITY.md) for reporting vulnerabilities and handling
secrets.

## License

Apache License 2.0, see [LICENSE](LICENSE).
