# Local test environment (docker compose). Needs local-test/.env (see .env.example).
COMPOSE := docker compose --project-directory local-test -f local-test/compose.yaml
# Mutator tests run under coverage (branch coverage of the mutator module).
COVERAGE := $(COMPOSE) exec -T -w /tmp superset python -m coverage
COVERAGE_ARGS := --data-file=/tmp/.coverage
MIN_COVERAGE := 100

.PHONY: hooks lint build up seed test release-check release down clean

RUFF := docker run --rm -v "$(CURDIR):/src" -w /src ghcr.io/astral-sh/ruff:0.14.3
UV_IMAGE := ghcr.io/astral-sh/uv:0.13.7-python3.11-trixie-slim
# Package version: the release tag on HEAD (vX.Y.Z), else the branch name and
# commit hash as PEP 440 local version (0.0.0+<branch>.git<hash>).
TAG_VERSION := $(patsubst v%,%,$(shell git describe --tags --exact-match --match 'v*' 2>/dev/null))
BRANCH_VERSION := 0.0.0+$(shell git rev-parse --abbrev-ref HEAD | tr -c 'A-Za-z0-9\n' '.').git$(shell git rev-parse --short HEAD)
PACKAGE_VERSION := $(or $(TAG_VERSION),$(BRANCH_VERSION))

hooks:  ## enable the repository's git hooks (.githooks/) for this clone
	git config core.hooksPath .githooks

lint:   ## ruff lint + format check (all rules, see pyproject.toml)
	$(RUFF) check --no-cache .
	$(RUFF) format --no-cache --check .

build:  ## build the wheel and sdist into dist/
	rm -rf dist
	docker run --rm -v "$(CURDIR):/src" -w /src \
		-e SETUPTOOLS_SCM_PRETEND_VERSION=$(PACKAGE_VERSION) \
		$(UV_IMAGE) uv build --out-dir dist

up: build ## start Dremio + Superset (with the wheel from dist/), wait until healthy
	mkdir -p local-test/.state/secrets && chmod 700 local-test/.state/secrets
	$(COMPOSE) up -d --build --wait

seed: up ## create Dremio/Superset users, view, grants, policy, proxy PAT
	$(COMPOSE) exec -T -u root superset chown superset /state
	$(COMPOSE) exec -T superset python /scripts/bootstrap.py

test: seed ## mutator tests with coverage (>= MIN_COVERAGE %), then browser tests
	$(COVERAGE) run $(COVERAGE_ARGS) --branch \
		--include='*/dremio_impersonation.py' \
		-m pytest -p no:cacheprovider /tests/test_mutator.py -v --tb=short
	$(COVERAGE) report $(COVERAGE_ARGS) --show-missing --fail-under=$(MIN_COVERAGE)
	$(COMPOSE) --profile ui-test run --rm --build ui-tests
	@# Record the tested commit for release tags (only for a clean worktree).
	@if [ -z "$$(git status --porcelain)" ]; then \
		git rev-parse HEAD > local-test/.state/tested-commit; \
		echo "make test: passed for commit $$(git rev-parse --short HEAD)"; \
	else \
		rm -f local-test/.state/tested-commit; \
		echo "make test: passed; worktree not clean, so no release can be tagged"; \
	fi

# Version of the release to tag: the newest "## [X.Y.Z] - YYYY-MM-DD" in CHANGELOG.md.
RELEASE_VERSION = $(shell sed -n 's/^\#\# \[\([0-9][0-9.]*\)\] - [0-9-]*$$/\1/p' CHANGELOG.md | head -n 1)

release-check: ## preconditions of `make release`
	@test -n "$(RELEASE_VERSION)" || { echo "release: CHANGELOG.md has no '## [X.Y.Z] - YYYY-MM-DD' section"; exit 1; }
	@! git rev-parse -q --verify "refs/tags/v$(RELEASE_VERSION)" >/dev/null || { echo "release: tag v$(RELEASE_VERSION) exists"; exit 1; }
	@test "$$(git rev-parse --abbrev-ref HEAD)" = main || { echo "release: check out main"; exit 1; }
	@test -z "$$(git status --porcelain)" || { echo "release: worktree not clean"; exit 1; }
	.githooks/check-release $(RELEASE_VERSION)

# The tested wheel (built by test) carries the release version.
release: PACKAGE_VERSION = $(RELEASE_VERSION)
release: release-check lint clean test ## test the version from CHANGELOG.md on a clean main, then tag it
	@test "$$(cat local-test/.state/tested-commit)" = "$$(git rev-parse HEAD)"
	git tag -a v$(RELEASE_VERSION) -m "v$(RELEASE_VERSION)"
	@echo "release: tagged v$(RELEASE_VERSION); push with: git push origin v$(RELEASE_VERSION)"

down:   ## stop containers, keep data
	$(COMPOSE) down

clean:  ## stop containers and delete volumes + generated state of this project only
	$(COMPOSE) --profile ui-test down -v --remove-orphans
	rm -rf local-test/.state
