# AGENTS.md

Development guidance for agents working on the Dremio impersonation mutator
for Apache Superset.

## Working style

- Keep changes small, explicit, and scoped to the user's request.
- Read `README.md`, `ARCHITECTURE.md` and `CONTRIBUTING.md` first. They are
  the single sources for usage, design, and development rules; follow them and
  do not repeat their content here or elsewhere. Link to them instead.
- Preserve unrelated worktree changes. Never reset, checkout, or revert user
  changes without explicit authorization.
- Do not add tooling, CI, or package managers unless the user asks for them.
- Keep the pinned versions unless the user asks to change them; then follow
  "Changing versions" in `CONTRIBUTING.md`.

## Public repository safety

- Treat this as a public repository (Apache License 2.0) and follow
  `SECURITY.md`. Use only generic examples.
- Never read, print, or stage `local-test/.env` or anything under
  `local-test/.state/`.
- Everything runs in containers (see `CONTRIBUTING.md`); do not create local
  virtualenvs or install packages on the host.
- `make clean` removes this project's containers, volumes and
  `local-test/.state`; check the Docker context before running it.

## Changelog and release notes

- For every change users notice, add an entry under `## [Unreleased]` in
  `CHANGELOG.md` (`Added`, `Changed`, `Fixed`, `Removed`, `Security`; mark
  incompatible changes as **Breaking**). Documentation-, test- and
  test-environment-only changes need no entry.
- Update `RELEASE.md` when a change affects what users must know before
  installing or upgrading (requirements, required settings, breaking changes).
  It is the text of the next GitHub release, not a list of changes.
- The version comes from the git tag; never write a version number into the
  code. Create tags or GitHub releases only when the user asks for a release;
  then follow "Versioning and releases" in `CONTRIBUTING.md`.

## Git discipline

- Do not commit or push unless the user explicitly asks.
- Work on a feature branch, not on `main`.
- Run `git status --short --branch` before staging.
- Stage only an explicit allowlist of files belonging to the requested change.
- Before committing, check the staged diff for secrets and run
  `git diff --cached --check`.
- Use terse, scoped commit messages, for example `feat: ...`, `docs: ...`,
  `test: ...`.
- Never force-push, amend published commits, or delete branches unless
  explicitly requested.
- Do not squash or rewrite the branch history: feature branches are
  squash-merged into `main` (see "Submitting changes" in `CONTRIBUTING.md`).
- Use the repository's git hooks (`make hooks`) and never bypass them with
  `--no-verify`. Create release tags only with `make release` (see "Release
  steps" in `CONTRIBUTING.md`).

## Review checklist

- [ ] No local tool settings or credentials are staged.
- [ ] No secrets, licenses, customer data, or local paths were added.
- [ ] `make lint` and `make test` pass without new lint exceptions.
- [ ] Each changed fact is updated in its single source document, not copied.
- [ ] `CHANGELOG.md` and `RELEASE.md` follow "Changelog and release notes".
- [ ] `git diff --check` passes for unstaged and staged changes.
- [ ] Only requested files are staged.
