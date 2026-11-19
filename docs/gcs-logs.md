# Run logs in Google Cloud Storage

With `LOG_SOURCE=gs://bucket[/prefix]` the check reads the run logs from a
Cloud Storage bucket instead of `LOG_DIR`. Use it when Renovate runs somewhere
this check can't mount a volume from, such as a Cloud Run job that writes
`LOG_FILE` to a Cloud Storage volume mount. The code is in
`src/renovate_alert/gcs.py`.

The check lists the objects directly under the prefix whose names start with
`LOG_PREFIX` and end in `.log` or `.log.gz` (one level, no subfolders), through
the Cloud Storage JSON API. An object's last update time is its modification
time, so the rules for which logs are new are those of
[checks.md](checks.md#run-logs). Calls time out after 60 seconds and are not
retried. A value that isn't a `gs://` URL stops the program at start with
`LOG_SOURCE: not a gs://bucket[/prefix] URL: ...`.

The state files stay local, in `STATE_DIR`, so the check still needs a small
writable volume.

## Authentication

The check reads a service account key file named by
`GOOGLE_APPLICATION_CREDENTIALS` and exchanges a JWT signed with its key for an
access token with the `https://www.googleapis.com/auth/devstorage.read_only`
scope, at the key's `token_uri` (`https://oauth2.googleapis.com/token` if the
key has none). The token is reused until a minute before it expires. Without
the variable, reading the logs fails with `LOG_SOURCE=gs://... needs
GOOGLE_APPLICATION_CREDENTIALS (a service account key file).`

No Google client library is needed. The standard library has no RS256, so the
code signs the JWT itself (PKCS#1 v1.5 with SHA-256); the key must be the
unencrypted PKCS#8 RSA key that a service account key file holds.

Give the service account `roles/storage.objectViewer` on that bucket and
nothing else.

```yaml
env:
  - name: LOG_SOURCE
    value: gs://renovate-logs
  - name: STATE_DIR
    value: /state
  - name: GOOGLE_APPLICATION_CREDENTIALS
    value: /var/run/secrets/gcs/key.json
```
