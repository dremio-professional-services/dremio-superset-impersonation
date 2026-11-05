# Changelog

All notable changes to this project are documented in this file. The format
is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/); see
[CONTRIBUTING.md](CONTRIBUTING.md#versioning-and-releases) for the rules.

## [Unreleased]

### Added

- `dremio_impersonation_mutator`: Superset `DB_CONNECTION_MUTATOR` that runs
  every `dremio+flight` query as the logged-in Superset user via Dremio's OAuth
  token exchange (inbound impersonation), with the proxy PAT stored as the
  Superset database password.
- URI parameters `TokenPort`, `TokenUrl` and `TokenTimeout`.
- Warnings for insecure TLS settings; rejection of ambiguous or unsafe URIs and
  tokens; no fallback to the proxy user.
- Distributed as a wheel on GitHub releases (with provenance attestation); it
  pins the tested `sqlalchemy-dremio` version.
- Local test environment (Docker Compose) with browser and mutator tests,
  coverage report and lint.

[Unreleased]: https://github.com/dremio-professional-services/dremio-superset-impersonation/commits/main
