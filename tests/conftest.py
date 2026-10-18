import os

# renovate_alert reads its configuration at import time.
os.environ.setdefault("FORGE_API", "https://forge.example/api/v1")
os.environ.setdefault("REPOSITORY", "owner/infra")
os.environ.setdefault("DASHBOARD_ISSUE", "7")
os.environ.setdefault("RENOVATE_TOKEN", "t")
os.environ.setdefault("APPRISE_URL", "http://apprise.example/notify/x")
