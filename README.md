# renovate-alert

[![CI](https://github.com/mbologna/renovate-alert/actions/workflows/ci.yml/badge.svg)](https://github.com/mbologna/renovate-alert/actions/workflows/ci.yml)

renovate-alert is a daily check for a self-hosted
[Renovate](https://docs.renovatebot.com/) that reports the failures Renovate
does not alert on, as one [Apprise](https://github.com/caronc/apprise-api)
digest. It reads Renovate's run logs from a volume or a
[Google Cloud Storage](https://cloud.google.com/storage) bucket, so it works
with a Renovate on any platform, [GitLab](https://about.gitlab.com/) included.
On [Forgejo](https://forgejo.org/) or [Gitea](https://about.gitea.com/) it also
reads the Dependency Dashboard and open PRs. An optional second check lists the
Renovate PRs on [GitHub](https://github.com/), GitLab and Forgejo that need a
person.

## What it catches

From the run logs (rules and matched log lines in [docs/checks.md](docs/checks.md)):

- Renovate didn't run: no run log, or none newer than `RUN_STALE_HOURS`.
- A run aborted on an external host error, though Renovate exited 0.
- A run exited with an error code.
- A repository's run ended in an error result (`unknown-error`, `config-validation`, `forbidden`...).
- A limit (`branchConcurrentLimit`, `prHourlyLimit`...) kept new branches from being created.
- A release has no release date, so `minimumReleaseAge` holds it pending for good.
- An update is still pending more than `OVERDUE_DAYS` after its release.
- Renovate offers no update, yet the newest release is `STUCK_DAYS` newer than
  the pin, or the pin is missing from the releases: versions that no longer
  sort by date, such as after a linuxserver `-ls` counter reset.

From the Forgejo/Gitea API, with `FORGE_API` set:

- The Dependency Dashboard lists Repository Problems.
- An update has sat in Rate-Limited or Pending Status Checks too long.
- An automerge PR has stayed open longer than `OPEN_AUTOMERGE_DAYS`.

With `NEEDS_YOU` ([docs/needs-you.md](docs/needs-you.md)) and the upkeep checks
([docs/upkeep.md](docs/upkeep.md)), each switched on by itself:

- A Renovate PR is held for manual review, or its CI failed.
- The Renovate app has stopped visiting a GitHub repository.
- GitLab CI minutes or namespace storage are near the quota.
- `GITHUB_TOKEN` or `GITLAB_TOKEN` expires soon.
- GitHub disabled a scheduled workflow for inactivity.
- A Renovate config holds versions back (`allowedVersions`), listed every fourth Monday.

## Needs you

`NEEDS_YOU=on` sends the entries above as a second message, apart from the
digest, grouped by forge and repository with each one's reason and age;
`NEEDS_YOU=only` sends it alone. It goes out when something new appears, and as
a Monday reminder while the list isn't empty.

## One report

`REPORT=on` folds the digest and the needs-you message into one report a
day, together with what [Gatus](https://github.com/TwiN/gatus) saw since the
last one: checks failing now, checks that failed and recovered, and how long
each heartbeat job took. What went bad goes to `APPRISE_URL` as a warning;
what went well goes silently to `REPORT_GOOD_URL`. Failures inside
`QUIET_WINDOWS`, such as a nightly reboot slot, count as expected. See
[docs/report.md](docs/report.md).

## Run it

Renovate writes run logs only when `LOG_FILE` is set: give each run its own
file on a volume this check can read, such as
`/logs/renovate-$(date -u +%Y%m%d-%H%M%S).log`, and keep `LOG_FILE_LEVEL` at
`debug`. For logs in Cloud Storage, see [docs/gcs-logs.md](docs/gcs-logs.md).

The image is `ghcr.io/mbologna/renovate-alert`, built for `linux/amd64` and
`linux/arm64`. Each [release](https://github.com/mbologna/renovate-alert/releases)
is tagged `X.Y.Z` and `X.Y`, and the newest also `latest`. Run it once a day
(a CronJob or timer):

```sh
docker run --rm \
  -e FORGE_API=https://forgejo.example.org/api/v1 -e REPOSITORY=owner/repo \
  -e DASHBOARD_ISSUE=1 -e RENOVATE_TOKEN=<token> \
  -e APPRISE_URL=http://apprise:8000/notify/alerts \
  -v renovate-logs:/logs \
  ghcr.io/mbologna/renovate-alert:<version>
```

The image runs as uid 1000, works with a read-only root filesystem and writes
only to `STATE_DIR` (`LOG_DIR` unless set). `RENOVATE_TOKEN` needs read access
to `REPOSITORY`'s issues and PRs (Forgejo scopes `read:repository` and
`read:issue`); the read-only needs-you tokens are listed in
[docs/needs-you.md](docs/needs-you.md#tokens).

## Configuration

Every variable the code reads:

| Variable | Default | Meaning |
|---|---|---|
| **Run logs and delivery** | | |
| `APPRISE_URL` | required | Apprise API notify URL, e.g. `http://apprise:8000/notify/alerts`; several, space-separated, are tried in order until one delivers |
| `NAME` | `REPOSITORY`, else `Renovate` | Name in the digest title |
| `LOG_DIR` | `/logs` | Directory holding the run logs `LOG_PREFIX*.log*` |
| `LOG_PREFIX` | `renovate-` | File name prefix of the run logs |
| `LOG_SOURCE` | | `gs://bucket[/prefix]`: read the logs from Cloud Storage instead of `LOG_DIR` |
| `GOOGLE_APPLICATION_CREDENTIALS` | | Service account key file, needed with `LOG_SOURCE` |
| `STATE_DIR` | `LOG_DIR` | Directory for the state files |
| `STATE_FILE` | `alert-state.json` | State file of the run log and forge checks; one per check sharing `STATE_DIR` |
| `RUN_STALE_HOURS` | `26` | Hours without a new run log before Renovate counts as not running; first check's look-back |
| `OVERDUE_DAYS` | `95` | Days after its release that a still-pending update is reported |
| `STUCK_DAYS` | `14` | Days the newest release may be newer than a pin with no update; `0`: off |
| `NEEDS_YOU` | `off` | `on`: also list the PRs that need you; `only`: list them alone |
| **Forgejo/Gitea dashboard and PRs** | | |
| `FORGE_API` | | API base, e.g. `https://forgejo.example.org/api/v1`; unset: run logs only |
| `CODEBERG_API` | | Old name of `FORGE_API`, read when `FORGE_API` is unset |
| `REPOSITORY` | | `owner/repo` Renovate runs on; required with `FORGE_API` |
| `DASHBOARD_ISSUE` | | Issue number of its Dependency Dashboard; required with `FORGE_API` |
| `RENOVATE_TOKEN` | | Token that reads its issues and PRs; required with `FORGE_API` |
| `WEB_URL` | `FORGE_API` without `/api/v1` | Base URL of the dashboard link |
| `AUTOMERGE_LABEL` | `renovate-automerge` | Label of automerge PRs; a needs-you PR without it waits for review |
| `PENDING_DAYS` | `35` | Days in Pending Status Checks before an update counts as stuck |
| `RATE_LIMITED_DAYS` | `10` | Days in Rate-Limited before an update counts as stuck |
| `OPEN_AUTOMERGE_DAYS` | `8` | Days an automerge PR may stay open |
| **Needs you** | | |
| `GITHUB_TOKEN` | | Reads GitHub; unset: GitHub is skipped |
| `GITHUB_OWNER` | | User or organisation whose PRs are read; required with `GITHUB_TOKEN` |
| `GITLAB_TOKEN` | | Reads GitLab; unset: GitLab is skipped |
| `GITLAB_URL` | `https://gitlab.com` | GitLab base URL |
| `GITLAB_NAMESPACE` | | Group (with subgroups) or user whose MRs are read; required with `GITLAB_TOKEN` |
| `FORGEJO_TOKEN` | | Reads Forgejo or Gitea; unset: skipped |
| `FORGEJO_URL` | | Base URL, e.g. `https://forgejo.example.org`; required with `FORGEJO_TOKEN` |
| `FORGEJO_REPOS` | | Comma-separated `owner/repo`s; required with `FORGEJO_TOKEN` |
| `MANUAL_REVIEW_LABEL` | `manual-review` | Label of a PR held for review |
| `DASHBOARD_TITLE` | `🤖 Renovate Dependency Dashboard` | Exact title of the GitHub dashboard issues |
| `DASHBOARD_STALE_DAYS` | `8` | Days a GitHub dashboard awaiting schedule may go unchanged; `0`: off |
| `NEEDS_YOU_STATE_FILE` | `needs-you-state.json` | State file of this check, in `STATE_DIR` |
| **Upkeep checks** | | |
| `GITLAB_USAGE` | `off` | `on`: list GitLab CI minutes and storage near the quota |
| `GITLAB_MINUTES_QUOTA` | `400` | Monthly CI minutes |
| `GITLAB_MINUTES_WARN` | `75` | Percent of the quota used that lists it |
| `GITLAB_MINUTES_PROJECTED_WARN` | `100` | Percent of the quota projected by month end that lists it |
| `GITLAB_MINUTES_PROJECT_AFTER_DAYS` | `7` | Days into the month before minutes are projected |
| `GITLAB_STORAGE_LIMIT_GIB` | `5` | Namespace storage limit in GiB (decimals allowed) |
| `GITLAB_STORAGE_WARN` | `80` | Percent of the storage limit that lists it |
| `TOKEN_EXPIRY` | `off` | `on`: list `GITHUB_TOKEN` and `GITLAB_TOKEN` close to expiry |
| `TOKEN_EXPIRY_DAYS` | `30` | Days before expiry that list a token |
| `DISABLED_WORKFLOWS` | `off` | `on`: list GitHub workflows disabled for inactivity |
| `VERSION_HOLDS` | `off` | `on`: list the config's version holds every fourth Monday |
| `HOLDS_FORGE` | `forgejo` | Forge of `HOLDS_REPO`: `forgejo`, `github` or `gitlab` |
| `HOLDS_REPO` | | `owner/repo` holding the Renovate config; required with `VERSION_HOLDS` |
| `HOLDS_FILE` | `renovate.json` | Path of the config (JSON) in `HOLDS_REPO` |
| **One report** | | |
| `REPORT` | `off` | `on`: send one report instead of the digest and the needs-you message |
| `GATUS_URLS` | | `name=url` pairs, comma-separated: the Gatus instances to read |
| `REPORT_GOOD_URL` | `APPRISE_URL` | Where the good report goes, with Apprise's tag `info` (silent) |
| `QUIET_WINDOWS` | | Local `HH:MM-HH:MM` windows, comma-separated, in which a failure that recovered is expected |
| `REPORT_TZ` | `UTC` | Time zone of `QUIET_WINDOWS` and of the times in the report |
| `REPORT_FIRST_HOURS` | `24` | Hours the first report looks back |
| `REPORT_SINCE` | | Local `HH:MM`: no report looks back past the last time the clock read it, so a morning report covers the night, not the day before |
| `REPORT_MIN_DOWN_MINUTES` | `10` | A failure that recovered sooner is a blip, counted in the good report, not listed in the bad one |
| `REPORT_PAGE_SIZE` | `100` | Results per Gatus check read (Gatus keeps 100 by default); failures come from its events, which reach further back |
| `REPORT_STATE_FILE` | `report-state.json` | State file of the report (the last one's time), in `STATE_DIR` |

## Development

The code is a Python 3.15 package in `src/renovate_alert/`, managed with
[uv](https://docs.astral.sh/uv/):

```sh
uv sync
uv run pytest --cov    # fails below fail_under (97%) in pyproject.toml
uv run ruff check . && uv run ruff format --check .
uv run pre-commit install
uv run pre-commit run --all-files
```

The [pre-commit](https://pre-commit.com/) hooks run
[ruff](https://docs.astral.sh/ruff/), [mypy](https://mypy-lang.org/),
[Bandit](https://bandit.readthedocs.io/),
[hadolint](https://github.com/hadolint/hadolint) and schema checks of the
workflows and `renovate.json`. CI runs ruff, the tests with the coverage gate
and [pip-audit](https://github.com/pypa/pip-audit) through
[mbologna/.github](https://github.com/mbologna/.github)'s `python-ci`
workflow, then builds, smoke-tests and scans the image with its `docker-ci`
workflow.

Dependencies resolve only to releases at least 7 days old (`exclude-newer`, uv
0.12.12 or later); a newer CVE fix needs a temporary `exclude-newer-package`
entry, then `uv lock`. Pull requests merge with a merge
commit, and every commit subject must follow
[Conventional Commits](https://www.conventionalcommits.org/); they decide the
next release ([Versioning](#versioning)).

The project skeleton comes from the [Copier](https://copier.readthedocs.io/)
template [python-app-template](https://github.com/mbologna/python-app-template);
see [AGENTS.md](AGENTS.md) for the conventions.

## Why not an existing tool

- Renovate's `exitCodeForErrors` (experimental) gives a failed run an exit
  code, but sends no alert. Nothing reads a self-hosted run's logs for missed
  runs, branch limits, releases without a date or overdue updates.
- Renovate's Dependency Dashboard on Forgejo or Gitea lists Repository
  Problems, but nothing sends a notification when one appears.
- GitHub's and GitLab's own emails warn about token expiry and CI minutes only
  at fixed thresholds, and not through Apprise.
- [Gatus](https://github.com/TwiN/gatus) alerts on each check as it fails. No
  tool builds a daily report from its history, or one "needs you" list across
  GitHub, GitLab and Forgejo.

Checked 2026-11-23.

## Versioning

Releases follow SemVer: [auto-release](https://github.com/mbologna/auto-release)
tags `vX.Y.Z` from the Conventional Commit types (`feat` minor; `fix`, `perf`,
`revert` patch; a breaking change major) on CI's Monday schedule, or on a push
to `main` for a CVE fix. Only the tag builds publish the image, as `X.Y.Z`,
`X.Y` and `latest`. `pyproject.toml`'s version stays `0.0.0`. See the
[versioning standard](https://github.com/mbologna/.github/blob/main/VERSIONING.md).

## Licence

[MIT](LICENSE).
