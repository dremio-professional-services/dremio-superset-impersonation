# Dremio impersonation for Apache Superset 0.1.0

First release of a Superset `DB_CONNECTION_MUTATOR` that runs every Dremio
Arrow Flight query as the logged-in Superset user. Superset connects with the
personal access token (PAT) of a technical proxy user; for each connection the
mutator exchanges it for a short-lived token of the user (Dremio inbound
impersonation), and Dremio enforces that user's privileges.

## Before you install

- Enable per-user cache keys in Superset (`CACHE_QUERY_BY_USER`, or *Per user
  caching* on the Dremio database). Without them, cached results of one user
  are shown to other users.
- The proxy must be a regular Dremio user with a PAT and without data
  privileges of its own; it needs an inbound impersonation policy for the
  target users.
- Requirements, tested versions and installation:
  [README](https://github.com/dremio-professional-services/dremio-superset-impersonation#readme).

## Changes

See the [changelog](https://github.com/dremio-professional-services/dremio-superset-impersonation/blob/main/CHANGELOG.md).
