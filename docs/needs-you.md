# PRs that need you

With `NEEDS_YOU=on` each run also reads the open Renovate PRs on up to three
forges and sends one more Apprise message listing those that need a person.
`NEEDS_YOU=only` runs this check alone, without the run log checks;
`APPRISE_URL` is still required. The code is in `src/renovate_alert/needs_you.py`;
the optional checks that add to the same message are in [upkeep.md](upkeep.md).

## Forges

Each forge is checked only if its token is set. A token without the variables
its forge needs stops the run with, for example, `GitHub's token is set but
GITHUB_OWNER is not.`; `NEEDS_YOU` with no token at all stops it with `NEEDS_YOU
needs GITHUB_TOKEN, GITLAB_TOKEN or FORGEJO_TOKEN.`

- GitHub: the search `is:pr is:open author:app/renovate user:<GITHUB_OWNER>`,
  which finds the PRs of the Mend Renovate app in every repository the token
  can read, private ones included.
- GitLab: the open MRs of the group `GITLAB_NAMESPACE`, subgroups included, in
  one listing. A user namespace has no such listing (HTTP 404), so its
  non-archived projects are read one by one. Only MRs from `renovate/` branches
  count, whoever authored them.
- Forgejo or Gitea: the open PRs of each repository in `FORGEJO_REPOS`; only
  PRs from `renovate/` branches count.

`GITLAB_URL` and `FORGEJO_URL` may end in `/api/v4` or `/api/v1`; the suffix is
removed. Draft PRs never count.

## Which PRs need you

A PR is listed for one or both of these reasons.

It waits for a manual review when it has the `MANUAL_REVIEW_LABEL` label or
lacks `AUTOMERGE_LABEL`. If its body has a line starting with "Why this waits
for you" (case aside, after any emoji and bold markup), that line is quoted
under it.

Its CI failed when a check on its head commit failed:

- GitHub: a check run concluded `failure`, `timed_out` or `startup_failure`, or
  a commit status is `failure` or `error`. `cancelled` is ignored, since a newer
  run of the same workflow cancels the older one. A fine-grained token can't
  read check runs (HTTP 403, or 404 on a private repository); the check then
  reads the head commit's GitHub Actions workflow runs, counting the same
  conclusions and `action_required` (a run waiting for approval reports no
  check), and names the workflows.
- GitLab: the head pipeline failed. The failed jobs that aren't allowed to fail
  are named, or the pipeline if every failed job was allowed to.
- Forgejo: the combined commit status is `failure` or `error`. The failing
  contexts are named, or `combined status` if none is.

Pending or running CI doesn't count.

## Repositories Renovate stopped visiting

On GitHub a repository is listed too when its Dependency Dashboard (an open
issue titled exactly `DASHBOARD_TITLE`) has an `## Awaiting Schedule` section
and hasn't changed in `DASHBOARD_STALE_DAYS`. The dashboards come from a second
search, `is:issue is:open in:title "Renovate Dependency Dashboard"
user:<GITHUB_OWNER>`.

The hosted Renovate app visits quiet repositories irregularly. A visit in the
schedule window opens the updates awaiting it, which changes the dashboard, so
one that still lists them after 8 days means a whole weekly window passed with
no visit. A quiet repository with nothing awaiting rightly stays unchanged for
months, and is not listed. The default suits schedules that come round at least
weekly; raise `DASHBOARD_STALE_DAYS` for longer ones, or set it to `0` to turn
this off. The entry links the dashboard and says how many days it has been.

## The message

Entries are grouped by forge and repository. Each has its title, a status line
(the reasons and the age in days, marked `(new)` when new), the quoted note if
any, and the URL. The title is `Renovate: <N> PR(s) need you, <M> new`, ending
in `(Monday reminder)` or `(version hold reminder)` when nothing is new; it
says `item(s)` when the list holds more than PRs. Apprise type is `info`.

The message is sent when something new appears: a PR, or a new reason for one
already listed (its CI going red, say). A PR that merely stays open is not
repeated, nor is a dashboard that goes another day without a visit. One that
goes away (merged, CI fixed, visited) sends nothing; if it comes back, it is
new again. The first run on a Monday sends the full list as a reminder, if it
isn't empty.

`STATE_DIR/NEEDS_YOU_STATE_FILE` (default `needs-you-state.json`) keeps what
was last sent, keyed by URL and reason, and the last good read of each forge.

## Failures

Each call is retried once, after 5 seconds, on a timeout, a network error or an
HTTP 5xx. Calls time out after 30 seconds, Forgejo calls after 120, as
Codeberg's PR list can take 30. A forge that still can't be read keeps the
entries of its last good read, so a day of 504s neither drops its PRs nor makes
them new; the error is added to the message, if one goes out, and the run
exits non-zero.

A message that Apprise doesn't accept leaves the sent keys as they were, so the
next run sends it again, and the run exits non-zero with `Apprise did not
accept the needs-you message; it is retried next run.`

## Tokens

All read-only:

- GitHub: a fine-grained token for the owner's repositories, private ones
  included, with Pull requests, Issues, Contents, Commit statuses and Actions
  set to read. A classic token works too: one with no scopes for public
  repositories only, with the `repo` scope for private ones. The search returns
  the private repositories the token can read.
- GitLab: a personal, group or project access token with `read_api`.
  `GITLAB_USAGE` also needs the token's user to own the namespace.
- Forgejo: a token with `read:repository` and `read:issue`.

## Example

A Kubernetes CronJob's environment, with the run log checks and every upkeep
check:

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
    value: https://forgejo.example.org
  - name: FORGEJO_REPOS
    value: owner/infra
  - name: GITLAB_USAGE
    value: "on"
  - name: TOKEN_EXPIRY
    value: "on"
  - name: DISABLED_WORKFLOWS
    value: "on"
  - name: VERSION_HOLDS
    value: "on"
  - name: HOLDS_REPO
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
