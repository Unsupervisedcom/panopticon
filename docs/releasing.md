# Releasing

Release automation has two legs:

1. **release-please** runs on every push to `main`, opens or updates a release PR that bumps
   version strings and the changelog, and publishes a GitHub Release when that PR merges.
2. The **Release** workflow runs when a GitHub Release is published, validates the package
   (typecheck + tests), builds the wheel + sdist, and uploads to PyPI.

## Important: publish before building container images

`panopticon build` bakes `pip install panopticon-app==${PANOPTICON_VERSION}` into the base
container image. The PyPI publish must complete before operators run `panopticon build` for
that version — otherwise the build will fail because the version isn't available yet.

## One-time GitHub setup

### RELEASE_PLEASE_TOKEN secret

Create a repository (or organization) secret named `RELEASE_PLEASE_TOKEN`. Use a fine-grained
PAT or GitHub App token with write access to **Contents**, **Issues**, and **Pull requests** for
this repository. The default `GITHUB_TOKEN` cannot be used because pull requests opened by it
do not trigger follow-on workflows.

### pypi environment

Create a GitHub environment named `pypi` (Settings → Environments). Restricting it to trusted
maintainers and requiring manual approval gives a final gate before each PyPI upload.

## One-time PyPI setup

Use PyPI Trusted Publishing so no long-lived PyPI token needs to be stored in GitHub. The
`Release` workflow already has `id-token: write` and `environment: pypi` configured.

If the `panopticon-app` PyPI project already exists, add a trusted publisher from the project's
**Manage → Publishing** page. If it does not exist yet, add a pending trusted publisher from the
PyPI account or organization **Publishing** page — the first successful publish creates the
project automatically.

Use these exact publisher values:

```
PyPI project/package: panopticon-app
Owner:                Unsupervisedcom
Repository:           panopticon
Workflow filename:    release.yml
Environment name:     pypi
```

## Normal release flow (after setup)

1. Merge feature/fix PRs to `main` using [Conventional Commits](https://www.conventionalcommits.org/)
   (`fix:`, `feat:`, `feat!:` / `BREAKING CHANGE:`, etc.).
2. release-please opens or updates a release PR titled `chore(main): release X.Y.Z`, bumping
   `pyproject.toml`, `src/panopticon/__init__.py`, `.release-please-manifest.json`, and
   `CHANGELOG.md`.
3. Review and merge the release PR.
4. release-please publishes a GitHub Release tagged `vX.Y.Z`.
5. The `Release` workflow runs, passes CI, builds the wheel + sdist, and uploads to PyPI.
6. Approve the `pypi` environment deployment if you have required reviewers configured.
7. Once PyPI shows the new version, run `panopticon build` to bake it into the container image.

## Local fallback (first publish if trusted publisher not yet configured)

Prefer the pending-trusted-publisher path for the first publish — it proves the same automation
that will run every future release. Only publish locally if that path is blocked.

For a brand-new project, use a short-lived account-scoped PyPI API token (a project-scoped
token cannot exist before the project exists). Delete or rotate it immediately after upload.

```sh
uv build
ls -lh dist/
uvx twine upload dist/*
# username: __token__
# password: <PyPI API token>
```

After upload, verify:

```sh
pip install --upgrade panopticon-app
panopticon --help
```

Then configure the normal trusted publisher so future releases use the automated path.
