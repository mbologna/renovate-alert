# renovate-alert

[![CI](https://github.com/mbologna/renovate-alert/actions/workflows/ci.yml/badge.svg)](https://github.com/mbologna/renovate-alert/actions/workflows/ci.yml)

A daily check for a self-hosted [Renovate](https://docs.renovatebot.com/) on
[Forgejo](https://forgejo.org/) or [Gitea](https://about.gitea.com/). It
catches the failures Renovate doesn't report, and sends them as one
[Apprise](https://github.com/caronc/apprise-api) digest.

All of these happened without a single error anywhere:

- **Renovate didn't run:** no run log newer than `RUN_STALE_HOURS`.
- **A run aborted:** a registry or the forge timed out or failed (an
  `ExternalHostError`), so Renovate skipped the whole repository, yet exited 0.
  The log says "External host error causing abort" or
  `"result": "external-host-error"`.
- **A run failed:** the log says "Renovate is exiting with a non-zero code" or
  "... with an error-specific code".
- **The branch limit was hit:** new updates were skipped with a debug-level line.
- **The Dependency Dashboard lists Repository Problems**, each line reported
  on its own. Two expected warnings are skipped: `timestamp-optional,
  proceeding` and `Could not determine resolved version after updating
  package file` (Terraform provider and lock file branches log it every run).
- **An update has been stuck too long:** in Rate-Limited (the merge queue isn't
  draining) or in Pending Status Checks (usually a release with no timestamp
  for `minimumReleaseAge`).
- **An automerge PR stays open:** it has the automerge label and hasn't merged
  within `OPEN_AUTOMERGE_DAYS`.

Every run log written since the previous check is read, current (`.log`) and
rotated (`.log.gz`) alike, so a failed run is caught even if later runs
succeeded. Each log is read once: the check remembers the newest modification
time it has read, and the first check (no state yet) looks back
`RUN_STALE_HOURS`. Rotating with `gzip` keeps the modification time, so a
compressed log is not read again.

Each problem is reported once, and again only if it goes away and comes back:
a run failure counts as gone once a check reads only clean runs. The first time
each update was seen, how far the logs have been read, and what has been
reported, are kept in `LOG_DIR/alert-state.json`. A digest that Apprise doesn't
accept is retried on the next run, logs included, and that run exits non-zero
so the failure is visible.

## Configuration

| Variable | Required | Default | |
|---|---|---|---|
| `FORGE_API` | no | | API base URL, e.g. `https://codeberg.org/api/v1`. Unset: run logs only (see below) |
| `REPOSITORY` | with `FORGE_API` | | `owner/repo` that Renovate runs on |
| `DASHBOARD_ISSUE` | with `FORGE_API` | | Issue number of the Dependency Dashboard |
| `RENOVATE_TOKEN` | with `FORGE_API` | | Token that can read the repo's issues and PRs |
| `APPRISE_URL` | yes | | Apprise notify URL, e.g. `http://apprise:8000/notify/alerts` |
| `LOG_DIR` | no | `/logs` | Directory holding Renovate's run logs and the state file |
| `WEB_URL` | no | `FORGE_API` without `/api/v1` | Base URL for the dashboard link |
| `LOG_PREFIX` | no | `renovate-` | Run logs are `LOG_PREFIX*.log[.gz]` |
| `STATE_FILE` | no | `alert-state.json` | State file name in `LOG_DIR`; give each check its own |
| `NAME` | no | `REPOSITORY` | Name in the digest title |
| `AUTOMERGE_LABEL` | no | `renovate-automerge` | Label that marks automerge PRs |
| `RUN_STALE_HOURS` | no | `26` | Hours without a run log before it reports one missing; also how far back the first check reads logs |
| `PENDING_DAYS` | no | `35` | Days in Pending Status Checks before an update counts as stuck |
| `RATE_LIMITED_DAYS` | no | `10` | Days in Rate-Limited before an update counts as stuck |
| `OPEN_AUTOMERGE_DAYS` | no | `8` | Days an automerge PR may stay open |

Without `FORGE_API` the check reads the run logs only (missing runs, aborted or
failed runs, the branch limit), for a Renovate on a platform it can't query,
such as GitLab. Two Renovates can share `LOG_DIR` if their logs use different
`LOG_PREFIX`es; run one check per prefix, each with its own `STATE_FILE`.

Renovate only writes run logs when it's told to. Set `LOG_FILE` to a
per-run path on a volume this check can read, e.g.
`/logs/renovate-$(date -u +%Y%m%d-%H%M%S).log`.

## Running

```sh
docker run --rm \
  -e FORGE_API=https://codeberg.org/api/v1 -e REPOSITORY=owner/repo \
  -e DASHBOARD_ISSUE=1 -e RENOVATE_TOKEN=... \
  -e APPRISE_URL=http://apprise:8000/notify/alerts \
  -v renovate-logs:/logs \
  ghcr.io/mbologna/renovate-alert:1.0.0
```

The image runs as uid 1000 and works with a read-only root filesystem. It
only needs write access to `LOG_DIR`, for the state file.

## Development

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Dependencies resolve only to releases at least 7 days old (`exclude-newer`
in `pyproject.toml`, which needs uv 0.12.12 or later), and the image installs
exactly what `uv.lock` pins. To take a CVE fix released less than 7 days ago,
add the package to `exclude-newer-package` as `<name> = false`, run `uv lock`,
and remove the entry once the release is a week old. Own packages
(`apprise-notify`) are exempt permanently.
