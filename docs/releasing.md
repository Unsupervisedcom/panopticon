# Releasing

Release automation has two legs:

1. `release-please` runs on pushes to `main`, opens or updates the release PR, and publishes the
   GitHub Release when that PR merges.
2. The `Release` workflow runs when a GitHub Release is published, validates the package, builds
   the source distribution and wheel, and publishes them to PyPI.

## GitHub setup

Create an organization secret named `RELEASE_PLEASE_TOKEN`. Use a fine-grained GitHub PAT or
GitHub App token that can write contents, issues, and pull requests for this repository.

The secret is intentionally separate from the default `GITHUB_TOKEN`: release-please opens and
updates release PRs, and the follow-on workflows from those PRs need to behave like normal
repository events.

Also create a GitHub environment named `pypi`. Keep it restricted to trusted maintainers; requiring
environment approval is a good final gate before an already-published GitHub Release can upload to
PyPI.

## PyPI trusted publisher setup

Use PyPI trusted publishing for the normal release path. This avoids storing a long-lived PyPI token
in GitHub and lets the `Release` workflow mint a short-lived upload token through GitHub OIDC.

If the `panopticon` PyPI project already exists, add a trusted publisher from the project's
`Manage` -> `Publishing` page. If it does not exist yet, add a pending publisher from the PyPI
account or organization `Publishing` page; the first successful trusted publish creates the project.

Use these exact publisher values:

```text
PyPI project/package: panopticon
Owner: Unsupervisedcom
Repository: panopticon
Workflow filename: release.yml
Environment name: pypi
```

The workflow side is already configured in `.github/workflows/release.yml`:

```yaml
permissions:
  contents: read
  id-token: write

jobs:
  publish-pypi:
    environment: pypi
```

and the publish step uses `pypa/gh-action-pypi-publish@release/v1` without a username, password, or
PyPI API token.

## First release checklist

1. Confirm the `RELEASE_PLEASE_TOKEN` organization secret is visible to this repository.
2. Confirm the `pypi` GitHub environment exists and has the intended reviewers.
3. Confirm the PyPI trusted publisher, or pending trusted publisher, matches the values above.
4. Merge the release-please PR only after it bumps `pyproject.toml`,
   `src/panopticon/__init__.py`, and `.release-please-manifest.json` together.
5. Approve the `pypi` GitHub environment deployment when the `Release` workflow runs from the
   published GitHub Release.

## Local first publish fallback

Prefer the pending trusted publisher path for the first publish. It proves the same automation that
will run every future release, and it does not require a PyPI token.

Only publish locally if the trusted-publisher first publish is blocked and an operator deliberately
chooses to create the PyPI project by hand. For a brand-new project, use a short-lived
account-scoped PyPI API token because a project-scoped token cannot exist before the project exists.
Delete or rotate that token immediately after the upload, then configure the normal trusted
publisher.

Build and inspect the distributions:

```sh
uv build
ls -lh dist/
```

Upload to PyPI with Twine:

```sh
uvx twine upload dist/*
```

When prompted, use `__token__` as the username and paste the PyPI API token as the password. After
the upload, verify installation from PyPI:

```sh
python3 -m pip install --upgrade panopticon
panopticon --help
```
