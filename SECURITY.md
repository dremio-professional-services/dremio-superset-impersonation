# Security Policy

## Supported Versions

Only the latest release receives security fixes (see
[CHANGELOG.md](CHANGELOG.md)); before the first release, the `main` branch.

## Reporting a Vulnerability

Report vulnerabilities privately by email to
[security@dremio.com](mailto:security@dremio.com), Dremio's channel for
responsible disclosure. Do not open a public issue or pull request for
security problems such as leaked secrets, authentication or impersonation
bypasses, cross-user data exposure, or sensitive endpoint exposure.

Please include:

- a description of the vulnerability and its impact,
- the affected component (this repository, `dremio_impersonation.py`) and
  versions (Superset, `sqlalchemy-dremio`, Dremio),
- steps to reproduce, and supporting material such as requests, responses,
  or logs, with all secrets removed.

Dremio's [responsible disclosure policy](https://www.dremio.com/platform/security/responsible-disclosure-limitations/)
applies, including its out-of-scope list. There is no bug bounty program.

## Secret Handling

Never commit:

- Dremio PATs, OAuth tokens, or passwords
- Dremio Enterprise licenses
- Superset `SECRET_KEY` values or database URIs containing credentials
- Customer endpoints or customer-specific configuration
- Local files with credentials, such as `local-test/.env` or anything under
  `local-test/.state/`

Use environment variables or local secret stores outside the repository.
