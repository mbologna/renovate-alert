# One report

`REPORT=on` turns renovate-alert's two messages, the Renovate digest and the
needs-you list, into one report a day, and adds what one or more
[Gatus](https://github.com/TwiN/gatus) instances saw since the last report.
It is meant to run once in the morning, after the night's maintenance, so
you read one message about the night instead of one per problem.

## What it reads

- **Gatus**, for each `name=url` in `GATUS_URLS`: every check's status from
  `GET /api/v1/endpoints/statuses?page=1&pageSize=REPORT_PAGE_SIZE`, external
  endpoints (heartbeats) included. No token: Gatus serves this read-only API
  to anyone who can reach it, so keep it on a private network.
- **The Renovate checks** this run makes anyway (the run logs, the dashboard,
  the needs-you list), unchanged: each still keeps its own state, so a problem
  is reported once.

## What it sends

**What went bad**, to `APPRISE_URL` as a warning, only when something did:

- a check failing now, with the time it went down and why (its errors, or the
  conditions it failed);
- a check that failed and recovered since the last report, with how long it
  was down, unless both ends fall inside one of `QUIET_WINDOWS` on the same
  day (a reboot slot, when checks fail on purpose);
- a Gatus that could not be read;
- the Renovate problems and the needs-you list, if this run has any.

**What went well**, every time, to `REPORT_GOOD_URL` with Apprise's tag
`info`, which selects a silent notification in a config that pairs each
target with an `info`-tagged silent copy:

- how many checks are healthy on each Gatus;
- how long each heartbeat job took, from the `duration` its last push
  carried (`?duration=` on Gatus's external endpoint API);
- the failures inside the quiet windows, as expected.

## State

`STATE_DIR/report-state.json` holds the time of the last report that was
delivered. A first run looks back `REPORT_FIRST_HOURS`. If the bad report is
not delivered, nothing moves on: the report's period stays where it was, and
the Renovate checks' state files go back to what they were before the run, so
the next run reports the same problems again. The job then exits non-zero.

## Example

```sh
docker run --rm \
  -e REPORT=on \
  -e GATUS_URLS=home=http://gatus.home:8080,cloud=http://gatus.cloud:8080 \
  -e QUIET_WINDOWS=05:15-05:55 -e REPORT_TZ=Europe/Rome \
  -e APPRISE_URL=http://apprise:8000/notify/alerts \
  -e REPORT_GOOD_URL=http://apprise:8000/notify/reports \
  -e FORGE_API=https://forgejo.example.org/api/v1 -e REPOSITORY=owner/repo \
  -e DASHBOARD_ISSUE=1 -e RENOVATE_TOKEN=<token> \
  -v renovate-logs:/logs \
  ghcr.io/mbologna/renovate-alert:<version>
```
