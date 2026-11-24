# Run log and forge checks

This page gives the exact rules of the checks in `src/renovate_alert/__init__.py`:
which run logs are read, what each detector matches, what the Forgejo/Gitea
checks read, and how problems are reported and remembered.

## Run logs

Renovate writes a run log only when `LOG_FILE` is set. Give each run its own
file, such as `/logs/renovate-$(date -u +%Y%m%d-%H%M%S).log`, and keep
`LOG_FILE_LEVEL` at its default, `debug`: the branch limits and dateless
releases are logged only at that level. Both `LOG_FILE_FORMAT=pretty` and JSON
logs are read. Logs are streamed, so a debug log of tens of megabytes is never
held in memory.

The check reads `LOG_DIR/LOG_PREFIX*.log*`: names ending in `.gz` are
decompressed, any other match is read as plain text. With `LOG_SOURCE` the logs
come from a bucket instead ([gcs-logs.md](gcs-logs.md)).

Each check reads every log modified since the newest one the previous check
read, current and rotated alike, so a failed run is caught even when later
runs followed it. The first check, with no state yet, looks back
`RUN_STALE_HOURS`. Rotating with `gzip` keeps the modification time, so a
compressed log is not read twice.

Without `FORGE_API` the check reads the run logs only, for a Renovate on a
platform it can't query, such as GitLab. Two Renovates can share `LOG_DIR` if
their logs use different `LOG_PREFIX`es; run one check per prefix, each with
its own `STATE_FILE`.

A record's repository comes from `(repository=...` in a pretty line or
`"repository": "..."` in a JSON line; a record without one is filed under `NAME`.

## Renovate didn't run

With no run log at all the digest says `No Renovate run log in <LOG_DIR or
LOG_SOURCE>.` When the newest log is older than `RUN_STALE_HOURS` (default 26,
for daily runs) it says `No Renovate run for <N>h (newest log <name>)`.

## Aborted and failed runs

A run aborted when its log contains `"result": "external-host-error"` or
`External host error causing abort`. A registry or the forge timing out or
failing raises an `ExternalHostError`, which skips the whole repository, yet
Renovate exits 0 unless `exitCodeForErrors` is set, so the Job looks fine.
`Repository finished` is logged for aborted runs too, so its absence is no sign
of an abort.

A run failed when its log contains `Renovate is exiting with a non-zero code`
or `Renovate is exiting with an error-specific code`.

A failed run (aborted or failed) is reported only when Renovate did not
recover from it:

- the newest run failed too, so no later run succeeded; or
- it is one of two or more failed runs in a row, even if a later run
  succeeded. The newest run of the previous check counts towards the streak
  (`last_run_failed` in the state file).

A single failure that the next run recovered from, such as one `git
ls-remote` to the forge timing out, is not reported. Neither is a Renovate that
fails every other run, as long as the newest run succeeds. One that keeps
failing is reported by the first check after its failures start.

## Failed repositories

Each repository's run ends with a `Repository finished` record. Its `"result"`
is matched with a regular expression, because its fields can hold a bare
`undefined` that isn't JSON. A result outside this list means Renovate skipped
the repository and updates nothing in it until the cause is fixed:

`done`, `automerged`, `onboarding`, `archived`, `blocked`, `cannot-fork`,
`disabled`, `disabled-by-config`, `disabled-closed-onboarding`,
`disabled-no-config`, `empty`, `fork`, `fork-missing`, `fork-mode-forked`,
`mirror`, `no-package-files`, `pending-deletion`, `renamed`, `uninitiated`,
`external-host-error`, `no-vulnerability-alerts`, `rate-limit-exceeded`,
`repository-changed`, `temporary-error`.

The list holds success, repositories Renovate skips by design, external host
errors (reported as aborted runs) and temporary errors the next run clears.
Results such as `unknown-error`, `config-validation`, `forbidden` or
`not-found` are reported, with the repository's name, even when Renovate exited 0.
The cause is the first line (at most 200 characters) of `err.message` in the
repository's last `Repository has unknown error`, `Repository has invalid
config` or `Git error - aborting` record before it finished. Only each
repository's newest result among the logs read counts, so one that recovered
in a later run is not reported.

## Branch limits

Renovate logs `Reached branch limit - skipping branch creation` whichever limit
stopped a branch. The debug line just before it names the limit:

| Line before it | Reported as |
|---|---|
| `Hourly PRs limit reached` | `prHourlyLimit` |
| `Hourly commits limit reached` | `commitHourlyLimit` |
| `Commits limit reached` | `prCommitsPerRunLimit` |
| `Branches limit reached`, `VulnerabilityBranches limit reached` | `branchConcurrentLimit` |
| none of these | `an unidentified limit` |

Branches are told apart by their `(repository=..., branch=...)` id, so a branch
skipped by several runs counts once. The problem names the runs and the number
of branches skipped. For `branchConcurrentLimit` it adds that updates arrive
faster than the merge window merges them, or branches are stuck; for
`prHourlyLimit`, that the hourly cap parks updates even while the queue has
room (Renovate's default is 2).

## Releases with no date

Under `minimumReleaseAge` with the default
`minimumReleaseAgeBehaviour=timestamp-required`, a release with no release
date stays pending for good. Renovate says so only in one debug line, matched
on `do not have a releaseTimestamp and we're running with
minimumReleaseAgeBehaviour=timestamp-required`, followed by the updates it
holds. Each `"depName"` in that record is reported once per repository while it
stays held (`an unnamed dependency` if none is named). The dashboard's Pending
Status Checks lists a branch only when every update in it is pending, so this
line is the only sign of a dateless update grouped with dated ones. The fix is
a datasource that dates releases, or `minimumReleaseAgeBehaviour`
`timestamp-optional` for that dependency.

## Overdue updates

The debug record `packageFiles with updates`, logged by real and
`--dry-run=lookup` runs alike, lists every package file's dependencies and
their updates. An update with `pendingChecks` set and a `releaseTimestamp` more
than `OVERDUE_DAYS` (default 95) old is reported as `<depName> <current> ->
<new> (<repository>): released <N> days ago and still pending`, the new version
being `newVersion`, `newValue` or `newDigest`. No age gate lasts that long, so
something else holds it. In a pretty log the record's indented detail lines
are joined into JSON; a record that doesn't parse is skipped.

## Stuck pins

The same record gives each dependency's `mostRecentTimestamp` and
`currentVersionTimestamp`, the pin's release date. A dependency with no
updates, no `skipReason`, a `mostRecentTimestamp` and no
`currentVersionTimestamp` is reported: it has releases, but the pin isn't
among them, so no update will come. One case is a linuxserver image whose
`-ls` counter was reset, when the image's datasource keeps only the new builds:
they all rank below the pin, so Renovate never offers one, and nothing else
reports it. Each repository's newest run decides, so a run that no longer finds
a pin clears it.

A pin older than `mostRecentTimestamp` is not reported. Renovate computes that
date from the raw releases, before `extractVersion`, the unstable filter and
`allowedVersions` drop any, so it is often a release the dependency never
considers: a Python alpha among `actions/python-versions` releases, a nightly
tag, or a major a rule holds back. 1.12.0 reported a pin 14 days older, and
every hit in the GitLab and GitHub runs of 2026-11 was one of these. A real
sort-order break doesn't show there either: when a lower version is newer than
the highest, Renovate leaves `mostRecentTimestamp` out. A reset whose old
builds stay in the releases therefore goes unseen, as does a maintenance line
releasing below a newer major.

## Forgejo/Gitea checks

With `FORGE_API` set the check also reads, with `RENOVATE_TOKEN`,
`{FORGE_API}/repos/{REPOSITORY}/issues/{DASHBOARD_ISSUE}` and the first 50 open
PRs (`pulls?state=open&limit=50`). A read is tried three times on HTTP 502, 503
or 504, a network error or a timeout, waiting 10 then 20 seconds, with a
120-second timeout. A read that still fails stops the run with an error before
the digest goes out; the logs stay unread, so the next run reports them.

Each ` - ` line under the dashboard's `## Repository Problems` heading is a
problem of its own. Two expected lines are skipped: `timestamp-optional,
proceeding` (rules that opt out of the age gate log it for every update) and
`Could not determine resolved version after updating package file` (Terraform
provider and lock file branches log it every run, and the update goes ahead).

Stuck updates are the unchecked items ` - [ ] <!-- ...=renovate/... -->` under
`## Rate-Limited` and `## Pending Status Checks`, with `chore(deps): ` cut from
the title. An item's age counts from the first check that saw it in that
section (`first_seen` in the state file). It is reported at `RATE_LIMITED_DAYS`
(default 10: the queue drains about 24 merges a week) as "rate-limited (queue
not draining)", or at `PENDING_DAYS` (default 35: the longest
`minimumReleaseAge` is 30 days) as "pending (likely no release timestamp)".

An open PR labelled `AUTOMERGE_LABEL` is reported once it is
`OPEN_AUTOMERGE_DAYS` old (default 8: the next Tuesday to Thursday merge
window).

## Reporting and state

The digest is titled `Renovate (<NAME>): <N> new problem(s)`, sent with
Apprise type `warning`, and lists only problems not reported before; with
`FORGE_API` it ends with a link to `WEB_URL/REPOSITORY/issues/DASHBOARD_ISSUE`.
Standard output lists every problem found.

Each problem has a key that stays the same while it lasts, so it is reported
once, and again only if it goes away and comes back. A problem goes away when a
check no longer finds it. Problems found in logs exist only in the checks that
read those logs: a run failure counts as gone once a check reads none worth a
report, and a failed repository or held dependency once a check reads no run
naming it.

`STATE_DIR/STATE_FILE` (default `alert-state.json`) keeps the first-seen dates
of dashboard items (`first_seen`), the problems reported (`alerted`), how far
the logs have been read (`logs_read_until`) and whether the newest run failed
(`last_run_failed`). It is written to a temporary file and renamed.

`APPRISE_URL` holds one Apprise API URL or several separated by spaces, sent
through the [apprise](https://github.com/caronc/apprise) library. Each URL is
its own priority group: they are tried in order and the first that delivers
ends the attempt, so the others are not called. A URL that fails (a network
error, any status but 200, including 204 when the key has no configuration) is
retried once after a second before the next is tried. An empty `APPRISE_URL`
delivers nothing; an unset one stops the program at start.

A digest that no URL delivers is retried on the next run: its problems stay
unreported and its logs unread. The run then exits non-zero with `Apprise did
not accept the digest; it is retried next run.` A problem that was reported
never makes the run exit non-zero. Configuration errors exit at start, for
example `FORGE_API needs REPOSITORY, DASHBOARD_ISSUE and RENOVATE_TOKEN.` or
`NEEDS_YOU is off, on or only.`
