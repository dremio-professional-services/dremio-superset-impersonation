# Contributing

Thank you for your interest in improving the Dremio impersonation mutator for
Apache Superset. This guide describes the development setup and the tests. For
what the mutator does and how to configure it, see [README.md](README.md).

Everything runs in containers; nothing is installed on the host apart from
Docker and `make`.

## Repository layout

| Path | Content |
|---|---|
| `dremio_impersonation.py` | the mutator (the only runtime code) |
| `README.md`, `ARCHITECTURE.md` | usage and configuration; design and decisions |
| `CHANGELOG.md`, `RELEASE.md` | changes per version; release notes of the next or latest release |
| `AGENTS.md` | rules for coding agents (Claude Code, Codex) |
| `SECURITY.md`, `CODE_OF_CONDUCT.md` | security policy and code of conduct |
| `Makefile` | entry points: hooks, lint, build, the local test environment, release |
| `.githooks/` | git hooks (`make hooks`) and the release check `check-release` |
| `pyproject.toml` | package (wheel) metadata and ruff configuration |
| `.github/workflows/release.yml` | builds and publishes a GitHub release for a pushed tag |
| `.dockerignore` | keeps secrets and local state out of Docker build contexts |
| `local-test/compose.yaml` | Dremio, Superset and the browser-test container |
| `local-test/.env.example` | template for local secrets (`local-test/.env`, gitignored) |
| `local-test/certs.sh` | generates the local test CA and the Dremio TLS certificate |
| `local-test/dremio/dremio.conf` | single-node Dremio (coordinator + executor) with TLS |
| `local-test/superset/` | Superset image with the mutator's wheel and `superset_config.py` |
| `local-test/scripts/bootstrap.py` | accepts the Dremio EULA, seeds users, data, grants, inbound policy and the proxy PAT |
| `local-test/ui_tests/` | browser tests (Playwright, Chromium): `test_ui.py`, fixtures in `conftest.py`, helpers in `helpers.py` |
| `local-test/tests/` | mutator tests inside the Superset container: `test_mutator.py`, Superset app in `conftest.py` |
| `local-test/.state/` | generated state: CA, certificates, credentials (gitignored) |

## Prerequisites

- Docker with Compose v2 (tested with Rancher Desktop on arm64; all images
  also exist for amd64), at least 2 CPU and 8 GB RAM for Docker (Dremio is
  capped at `DREMIO_MAX_MEMORY_SIZE_MB=6144`), and about 6 GB disk (images
  3.2 GB + 1.3 GB).
- `docker login quay.io` with access to `quay.io/dremio/dremio-enterprise`.
- A Dremio Enterprise license.

Set up the local secrets once:

```sh
cp local-test/.env.example local-test/.env
# DREMIO_LICENSE=<license string>
# SUPERSET_SECRET_KEY=<openssl rand -base64 42>
```

`local-test/.env` is gitignored. The license reaches Dremio as a Compose secret
(file `/opt/dremio/license/key`), not as an environment variable.

Enable the repository's git hooks once per clone with `make hooks`
(`.githooks/`): `pre-commit` runs `make lint`, and `pre-push` lets a release
tag through only if `make test` passed for exactly that commit. Git cannot
enforce hooks; do not bypass them with `--no-verify`.

## Local test environment

```sh
make hooks       # enable the git hooks (once per clone)
make lint        # ruff lint + format check (see "Code style" below)
make build       # build the wheel and sdist into dist/ (gitignored)
make up          # make build, then start Dremio + Superset with that wheel
make seed        # create users, data, grants, inbound policy, a fresh proxy PAT
make test        # seed, mutator tests with coverage report, browser tests
make down        # stop, keep data
make clean       # remove containers, volumes and local-test/.state of this project only
make release     # test and tag the version from CHANGELOG.md (see "Release steps")
```

- Superset: http://localhost:8088
- Dremio: https://localhost:9047 (TLS with the local CA
  `local-test/.state/superset/ca.pem`; your browser shows a certificate warning)
- Generated passwords and the current PAT: `local-test/.state/secrets/credentials.json`
- After a green `make test` with a clean worktree, the tested commit is
  recorded in `local-test/.state/tested-commit` (used by `make release` and the
  `pre-push` hook).
- The Makefile uses your current Docker context; select another one with
  `DOCKER_CONTEXT=<name> make ...`.
- The browser tests save screenshots of every page still open at the end of
  each test in `local-test/.state/ui-artifacts/screenshots/` (gitignored,
  replaced on every run), named `<running number>-<test>-<page>.png`, for
  example `027-revoked_pat_fails_queries-superset-alice.png`. Pages where a
  PAT was typed (*Edit database*) are closed before, so they never appear on a
  screenshot.

The seeded users, test data and inbound policy are described in the docstring
of [`local-test/scripts/bootstrap.py`](local-test/scripts/bootstrap.py).

A fresh Dremio instance rejects every request until its End User License
Agreement is accepted. `make seed` accepts it automatically (the same REST call
the Dremio web UI makes), so by running it you accept Dremio's EULA for your
local test instance.

## Tests

- `local-test/ui_tests/`: **browser tests** that log in through the Superset
  login form and use SQL Lab, saved charts (Explore), a dashboard and *Edit
  database*, as a user would. The job owner is read in the Dremio web UI (Jobs
  page, job profile "User:"). Only test setup (datasets, charts, dashboard, new
  PATs) uses REST APIs; those helpers are marked "(setup)" in `helpers.py`. The
  test browser trusts the local CA, so TLS is verified there too.
- `local-test/tests/test_mutator.py`: **mutator tests** that call the mutator
  the way Superset does and cover what the browser cannot reach. They run
  inside the Superset container against the real Superset app and the real
  Dremio, with the PAT stored in the Superset database. Responses the real
  Dremio never gives (redirects, broken JSON) come from a small local HTTP
  server in the test (`FakeTokenEndpoint`).

Every test checks one behavior; its name and docstring say what it proves.
`make test` lists every test by name (`-v`); the files are the list of what is
tested.

### Coverage

`make test` runs the mutator tests under `coverage.py` and reports the branch
coverage of `dremio_impersonation.py` with the missing lines. Below the minimum
(`MIN_COVERAGE` in the `Makefile`), `make test` fails before the browser tests
start. The browser tests are not part of the measurement: there the mutator
runs inside Superset's web server process.

Not tested: background and async execution (see
[ARCHITECTURE.md](ARCHITECTURE.md#background-and-async-execution)).

### Checking that the cache tests can fail

The cache isolation tests only prove something if they fail without per-user
cache keys. To check this after changing the tests or the Superset version:

1. Set `"CACHE_QUERY_BY_USER": False` in `local-test/superset/superset_config.py`.
2. `make up`, then
   `docker compose --project-directory local-test -f local-test/compose.yaml --profile ui-test run --rm ui-tests -k "only_own_result or cached_rows"`.
3. Expect all six tests to fail (bob sees alice's cached result and vice
   versa). Restore the flag and run `make up`.

With the flag off and *Per user caching* checked on the Dremio database (Edit
database > Advanced > Security), the same tests pass.

## Code style

`make lint` runs ruff (version pinned in the `Makefile`) in a container: `ruff check` with **all** rules
(`select = ["ALL"]` in `pyproject.toml`) and `ruff format --check`. The only
exception is `S101` (`assert`) in test files, because pytest is built on
`assert`; the rule targets production code, where `python -O` removes asserts.
Ruff resolves two pairs of mutually exclusive docstring rules (D203/D211,
D212/D213) itself and prints a warning about it on every run.

Do not add `# noqa` comments or further exceptions; change the code instead.
To apply ruff's automatic fixes and formatting, run the same image as
`make lint` with `check --fix .` and `format .`.

Public functions and classes have Google-style docstrings (`Args`, `Returns`,
`Raises`); all code has type annotations.

## Development workflow

1. Change `dremio_impersonation.py`.
2. `make lint`, then `make test`. `make up` builds the wheel and rebuilds the
   Superset image with it, so no manual image step is needed.
3. Look at the screenshots of the browser tests, or log in at
   http://localhost:8088 with a password from `credentials.json`.

Conventions:

- Test what a user does: drive the Superset and Dremio web UIs. Use REST APIs
  only for setup, and mark such helpers "(setup)".
- One behavior per test; name and docstring say which.
- Every test must be able to fail. For protective behavior, check that the
  test fails when the protection is switched off (as for the cache tests).
- Never print secrets. Tests run with `--tb=short`, because pytest's long
  tracebacks show function arguments (for example a PAT). Generated secrets
  stay in `local-test/.state/` (gitignored).
- Each `make seed` creates a fresh proxy PAT, because the PAT test revokes the
  existing ones.
- Do not run the test suites in parallel against the same stack: the browser
  PAT tests revoke and expire the stored PAT, and the mutator tests use it.
  `make test` runs them one after the other.
- `make clean` only touches this Compose project (`dremio-superset-local-test`)
  and `local-test/.state`.

### Changing versions

1. Re-check the [library internals](ARCHITECTURE.md#dependencies-on-library-internals)
   the mutator relies on.
2. Change the pins in `local-test/superset/Dockerfile` (Superset,
   `sqlalchemy-dremio`; the build runs `uv pip check`) or
   `local-test/compose.yaml` (Dremio).
3. Run `make clean && make test`, and update the
   [version table](README.md#requirements-and-tested-versions).

## Versioning and releases

This project follows [Semantic Versioning 2.0.0](https://semver.org/). Changes
are recorded in [CHANGELOG.md](CHANGELOG.md); [RELEASE.md](RELEASE.md) holds
the release notes of the next or latest release, which become the text of the
GitHub release.

### Version number

The single source of the version is the git tag `vX.Y.Z`. No version number is
kept in the code: the wheel gets its version from the tag when it is built
(`hatch-vcs`), and `__version__` reads it from the installed package. Builds
without a release tag (`make build`, the test image) get the branch name and
commit hash as version, for example `0.0.0+main.git1a2b3c4`; PEP 440 requires
the `0.0.0+` prefix. Builds outside `make` without git metadata get
`0.0.0+unknown`. The release is prepared in
`CHANGELOG.md` and `RELEASE.md`; `make release` reads the version from the
newest `## [X.Y.Z] - YYYY-MM-DD` section of `CHANGELOG.md` and creates the
tag.

### Public API

Semantic Versioning applies to what users configure or depend on:

- the function `dremio_impersonation_mutator` and its use as
  `DB_CONNECTION_MUTATOR`,
- the URI parameters of the mutator (`TokenPort`, `TokenUrl`, `TokenTimeout`)
  and their defaults,
- which URIs, users and tokens are accepted or rejected,
- the exception class `ImpersonationError`, the logger name
  `dremio_impersonation` and its warnings,
- the [tested versions](README.md#requirements-and-tested-versions) of
  Superset, `sqlalchemy-dremio` and Dremio.

Private helpers (names starting with `_`) and the local test environment are
not part of the public API.

### Which number to increase

| Change | Example | Version |
|---|---|---|
| Incompatible change of the public API | rename or remove a URI parameter, change a default, reject a URI or user that worked before, drop a tested Superset or Dremio version | **MAJOR** |
| Backwards-compatible addition | new URI parameter, new supported Superset or Dremio version | **MINOR** |
| Backwards-compatible fix | bug or security fix without configuration change, clearer error or log message | **PATCH** |
| No change for users | documentation, tests, local test environment | no release |

While the version is `0.x`, the public API may still change in MINOR
releases; such changes are marked as **Breaking** in the changelog.

### Release steps

Release tags are created only by `make release`, never by hand: the functional
tests need the local Dremio Enterprise license and run before the tag is
pushed.

1. Prepare the release in a pull request titled `chore: release vX.Y.Z`;
   choose the version with the table above:
   - `CHANGELOG.md`: rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`,
     add a new empty `## [Unreleased]` above it, and update the links at the
     bottom (`[Unreleased]` compares `vX.Y.Z...HEAD`, `[X.Y.Z]` points to the
     release of tag `vX.Y.Z`).
   - `RELEASE.md`: write the release notes with `X.Y.Z` at the end of the
     first line (replace the previous release's text and remove the "not
     released yet" note).
2. After the squash merge, on an up-to-date, clean `main`, run `make release`.
   It takes `X.Y.Z` from `CHANGELOG.md`, checks that `RELEASE.md` names the
   same version (`.githooks/check-release`), runs `make lint`, `make clean` and
   `make test` with the wheel built as version `X.Y.Z`, and tags the tested
   commit as `vX.Y.Z`.
3. Push the tag: `git push origin vX.Y.Z`. The `pre-push` hook repeats the
   checks for the tagged commit.
4. The `release` workflow (`.github/workflows/release.yml`) takes the version
   from the tag, repeats the checks and lint, builds the wheel with this
   version, installs it into the tested Superset image, compares the installed
   version with the tag,
   attests its provenance, and creates the GitHub release with `RELEASE.md` as
   text and the wheel, sdist and `SHA256SUMS` as assets. Check that the run
   succeeded. It cannot run the functional tests (no Dremio license in CI);
   step 2 covers them.

Security fixes are released as PATCH (or as part of the next release) after
the report has been handled as described in [SECURITY.md](SECURITY.md).

## Submitting changes

1. Open an issue first for larger changes, so the approach can be discussed.
2. Create a branch from `main` and keep the change focused.
3. Run `make lint` and `make test`; for new protective behavior, check that its
   test fails when the protection is switched off.
4. Update `README.md`, `ARCHITECTURE.md` and this guide when behavior,
   configuration, design, or tested versions change, and add an entry under
   `Unreleased` in [CHANGELOG.md](CHANGELOG.md) for every change users notice.
   Releases are made by maintainers (see [Versioning and releases](#versioning-and-releases)).
5. Follow [SECURITY.md](SECURITY.md) for secrets; use only generic examples.
6. Open a pull request that describes the change and how it was tested.
7. The pull request is **squash-merged** into `main`: all commits of the branch
   become one commit on `main`, with a [Conventional Commits](https://www.conventionalcommits.org/)
   title such as `feat: ...` or `fix: ...`. The branch history is therefore
   not rewritten before the merge; intermediate commits do not reach `main`.

Report security issues privately as described in [SECURITY.md](SECURITY.md),
not in a public issue. This project follows the
[Code of Conduct](CODE_OF_CONDUCT.md).

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).
