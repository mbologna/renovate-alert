# Upkeep checks

Four optional checks add entries to the [needs-you](needs-you.md) message,
under its rules: an entry is sent when it first appears or gains a new reason,
and again in the Monday reminder while it lasts. A number that merely changes
(more minutes used, a token a day closer to expiry) is no news. Each is off
unless set to `on` (any value other than `on` or `off` stops the run), and each
reads with the needs-you tokens. A check that can't be read keeps its entries
from the last good read and fails the run, as a forge does. The code is in
`src/renovate_alert/needs_you.py`.

## GitLab usage

`GITLAB_USAGE=on` lists the CI minutes `GITLAB_NAMESPACE` has used this month,
against `GITLAB_MINUTES_QUOTA` (default 400, the Free tier's), and the same
projected linearly to the end of the month. It is listed once the minutes used
reach `GITLAB_MINUTES_WARN` percent of the quota (default 75), or the
projection reaches `GITLAB_MINUTES_PROJECTED_WARN` percent (default 100). No
projection is made until `GITLAB_MINUTES_PROJECT_AFTER_DAYS` full days of the
month have passed (default 7), as one release build on the 1st projects to
thirty.

The namespace's storage, with its container registry share, is listed at
`GITLAB_STORAGE_WARN` percent (default 80) of `GITLAB_STORAGE_LIMIT_GIB`
(default 5). The entries link the namespace's usage quotas page.

Both come from GitLab's GraphQL API (`ciMinutesUsage`,
`namespace.rootStorageStatistics`), which works for a user namespace and a
group alike. The check needs `GITLAB_TOKEN` and `GITLAB_NAMESPACE`, a token
with the `read_api` (or `api`) scope, and the token's user must own the
namespace. It skips, sending nothing, when:

- the token's scopes (from `GET /personal_access_tokens/self`) lack both, with
  `GitLab usage skipped: GITLAB_TOKEN has no read_api scope (api works too).`;
- GraphQL answers HTTP 401 or 403;
- the namespace has no storage statistics (storage only), with `GitLab storage
  skipped: no storage statistics (the token's user must own the namespace).`

A namespace that GraphQL can't find counts as a failed read.

## Token expiry

`TOKEN_EXPIRY=on` lists `GITHUB_TOKEN` and `GITLAB_TOKEN` when they expire
within `TOKEN_EXPIRY_DAYS` (default 30), or have expired. It needs at least one
of them. GitHub's expiry comes from the `github-authentication-token-expiration`
header of an authenticated call (`/rate_limit`), GitLab's from `GET
/personal_access_tokens/self`; a GitLab token that endpoint can't describe
(HTTP 401, 403 or 404) is skipped. A token with no expiry date is not listed,
nor is a Forgejo token, as Forgejo tokens don't expire. The log gives each
date; token values are never printed.

## Disabled workflows

`DISABLED_WORKFLOWS=on` lists the GitHub workflows whose state is
`disabled_inactivity` in `GITHUB_OWNER`'s non-archived repositories. GitHub
stops scheduled workflows in a public repository after 60 days without
activity, without telling anyone. The repositories come from the search
`user:<owner> archived:false`, the workflows from `GET
/repos/{owner}/{repo}/actions/workflows`, which the fine-grained token's
Actions permission covers. It needs `GITHUB_TOKEN` and `GITHUB_OWNER`.

## Version holds

`VERSION_HOLDS=on` lists every `packageRules` entry with `allowedVersions` in
`HOLDS_FILE` (default `renovate.json`, parsed as plain JSON, so JSON5 fails to
read) of `HOLDS_REPO`, read from the default branch through the contents API
of `HOLDS_FORGE`: `forgejo` (default, at `FORGEJO_URL`), `github` or `gitlab`
(at `GITLAB_URL`). It needs
`HOLDS_REPO` and that forge's token.

Each hold is listed with what it matches (the first of `matchPackageNames`,
`matchDepNames`, `matchPackagePatterns`, `matchPackagePrefixes`,
`matchSourceUrls` and `matchSourceUrlPrefixes` that is set, else
`packageRules[<n>]`) and its `description`, so a hold put in place until
something upstream changes isn't forgotten.

Holds are never news by themselves. They go out on the first run after the
check is switched on, then on a Monday at least 28 days after the last time
(every fourth Monday), in that day's message. A delivery that fails is retried
on the next run.
