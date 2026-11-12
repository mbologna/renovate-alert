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
- **A limit kept new branches from being created:** Renovate logs the same
  debug-level skip line for every limit, so the line before it decides which
  one is reported (`branchConcurrentLimit`, `prHourlyLimit`,
  `commitHourlyLimit` or `prCommitsPerRunLimit`). A branch skipped by several
  runs counts once.
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

A second, optional check (`NEEDS_YOU`, below) lists the Renovate PRs that
need a person, on GitHub, GitLab and Forgejo: the ones held for manual review,
and the ones whose CI failed. On GitHub it also lists the repositories the
Renovate app has stopped visiting.

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

## PRs that need you

With `NEEDS_YOU=on` each run also reads the open Renovate PRs on up to three
forges and sends one more Apprise message listing those that need a person,
grouped by forge and repository, each with its title, URL, reason and age in
days. `NEEDS_YOU=only` runs this check alone, without the run log checks.

A PR needs you when:

- **it waits for a manual review:** it has the `MANUAL_REVIEW_LABEL` label, or
  lacks `AUTOMERGE_LABEL`. If its body has a line starting with "Why this waits
  for you" (emoji and bold markup aside), that line is quoted under it;
- **its CI failed:** a check on its head commit failed. GitHub: a check run
  concluded `failure`, `timed_out` or `startup_failure`, or a commit status is
  `failure` or `error`; with a token that can't read check runs, a GitHub
  Actions workflow run concluded one of those or `action_required`, and the
  workflow is named. GitLab: the head pipeline failed; the failed jobs that
  aren't allowed to fail are named. Forgejo: the combined commit status is
  `failure` or `error`.

Pending or running CI doesn't count, nor do draft PRs.

On GitHub, a repository is listed too when **Renovate hasn't visited it**: its
Dependency Dashboard (the open issue titled `DASHBOARD_TITLE`) has an
"Awaiting Schedule" section and hasn't changed in `DASHBOARD_STALE_DAYS`. The
hosted Renovate app visits quiet repositories irregularly. A visit in the
schedule window opens the updates awaiting it, which changes the dashboard, so
one that still lists them after 8 days means a whole weekly window passed with
no visit. A quiet repository with nothing awaiting rightly stays unchanged for
months, and is not listed. The default suits schedules that come round at
least weekly; raise `DASHBOARD_STALE_DAYS` for longer ones, or set it to `0` to
turn this off. The entry links the dashboard and says how many days it's been.

The message is sent when something new appears: a PR, or a new reason for one
already listed (its CI going red, say). A PR that merely stays open is not
repeated, nor is a dashboard that goes another day without a visit. One that
goes away (merged, CI fixed, visited) sends nothing; if it comes back, it is
new again. On Mondays the full list goes out as a reminder, if it
isn't empty. What was last sent is kept in `LOG_DIR/needs-you-state.json`,
keyed by PR URL and reason.

A forge that can't be read (after one retry on a timeout or a 5xx) keeps the
PRs from its last good read, so a day of 504s neither drops them nor makes
them new; the run then exits non-zero. Forgejo calls wait up to 120 seconds:
Codeberg's PR list can take 30.

Each forge is checked only if its token is set:

| Variable | Required | Default | |
|---|---|---|---|
| `NEEDS_YOU` | no | `off` | `on`: run this check as well; `only`: run it alone |
| `GITHUB_TOKEN` | no | | Token for GitHub (scopes below) |
| `GITHUB_OWNER` | with `GITHUB_TOKEN` | | User or organisation: searches `is:pr is:open author:app/renovate user:<owner>` |
| `GITLAB_TOKEN` | no | | Token for GitLab |
| `GITLAB_URL` | no | `https://gitlab.com` | GitLab base URL |
| `GITLAB_NAMESPACE` | with `GITLAB_TOKEN` | | Group (subgroups included) or user whose projects' MRs are read; only MRs from `renovate/` branches count |
| `FORGEJO_TOKEN` | no | | Token for Forgejo or Gitea |
| `FORGEJO_URL` | with `FORGEJO_TOKEN` | | Base URL, e.g. `https://codeberg.org` |
| `FORGEJO_REPOS` | with `FORGEJO_TOKEN` | | Comma-separated `owner/repo`s; only PRs from `renovate/` branches count |
| `AUTOMERGE_LABEL` | no | `renovate-automerge` | As above: a PR without it waits for a review |
| `MANUAL_REVIEW_LABEL` | no | `manual-review` | Label of a PR held for review |
| `DASHBOARD_TITLE` | no | `🤖 Renovate Dependency Dashboard` | Exact title of the GitHub Dependency Dashboard issues |
| `DASHBOARD_STALE_DAYS` | no | `8` | Days a GitHub dashboard with updates awaiting schedule may go unchanged; `0`: off |
| `NEEDS_YOU_STATE_FILE` | no | `needs-you-state.json` | State file name in `LOG_DIR` |

The GitHub search covers the PRs of the Mend Renovate app. The dashboards
come from a second search, `is:issue is:open in:title "Renovate Dependency
Dashboard" user:<owner>`, which keeps only the issues titled exactly
`DASHBOARD_TITLE`. On GitLab and Forgejo, a self-hosted Renovate's PRs are told apart by their `renovate/`
branch, whoever authored them. A GitLab group's MRs are read in one listing; a
user namespace has none, so its projects are read one by one.

Tokens, all read-only:

- **GitHub:** a fine-grained token for the owner's repositories, private
  ones included, with Pull requests, Issues, Contents, Commit statuses and
  Actions set to read. Fine-grained tokens can't read check runs (403, or 404
  on a private repository), so the check then reads the head commit's GitHub
  Actions workflow runs instead, which the Actions permission covers. A
  classic token works too: one with no scopes for public repositories only,
  with the `repo` scope for private ones. The search returns the private
  repositories the token can read.
- **GitLab:** a personal, group or project access token with `read_api`.
- **Forgejo:** a token with `read:repository` and `read:issue`.

For example, in a Kubernetes CronJob alongside the run log checks:

```yaml
env:
  - name: NEEDS_YOU
    value: "on"
  - name: AUTOMERGE_LABEL
    value: automerge
  - name: GITHUB_OWNER
    value: owner
  - name: GITLAB_NAMESPACE
    value: owner
  - name: FORGEJO_URL
    value: https://codeberg.org
  - name: FORGEJO_REPOS
    value: owner/infra
  - name: GITHUB_TOKEN
    valueFrom:
      secretKeyRef: {name: renovate-alert, key: GITHUB_TOKEN}
  - name: GITLAB_TOKEN
    valueFrom:
      secretKeyRef: {name: renovate-alert, key: GITLAB_TOKEN}
  - name: FORGEJO_TOKEN
    valueFrom:
      secretKeyRef: {name: renovate-alert, key: FORGEJO_TOKEN}
```

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
