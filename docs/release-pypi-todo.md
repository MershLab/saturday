# PyPI publishing: one manual step left

The release workflow's `pypi` job is otherwise ready (it builds and would
publish on every `vX.Y.Z` tag), but nobody has ever configured how it's
allowed to publish. No `PYPI_TOKEN` secret exists on this repo, and PyPI's
Trusted Publishing isn't set up either — pick one:

## Option A: Trusted Publishing (recommended, no secret to manage or rotate)

1. Sign in to pypi.org as the owner of the `saturday` project (or, if it's
   never been published under this account, use the "publish a new project"
   pending-publisher flow — same form, works before the project exists).
2. Project → Publishing → add a new trusted publisher:
   - Owner: `MershLab`
   - Repository: `saturday`
   - Workflow: `release.yml`
   - Environment: leave blank (the job doesn't use one)
3. Nothing else to do — `id-token: write` is already set on the `pypi` job.

## Option B: Classic API token

1. pypi.org → Account settings → API tokens → create one scoped to the
   `saturday` project.
2. `gh secret set PYPI_TOKEN --repo MershLab/saturday` and paste it.
3. The workflow already reads `secrets.PYPI_TOKEN`; once it's non-empty the
   action uses it directly instead of falling back to OIDC.

Either one unblocks the `pypi` job on the next tag push. Until then, every
other artifact (deb/rpm/AppImage/dmg/exe + the arch package + the GitHub
Release itself) publishes fine — this is the only step gated on a human
with PyPI access.
