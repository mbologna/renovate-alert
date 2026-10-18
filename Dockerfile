FROM python:3.14.8-alpine@sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72 AS build

# git: apprise-notify is a git dependency. Installed into a virtualenv, so the
# runtime stage needs neither git nor pip.
RUN apk add --no-cache git
COPY pyproject.toml README.md LICENSE /tmp/build/
COPY src/ /tmp/build/src/
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir /tmp/build

FROM python:3.14.8-alpine@sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 1000:1000
ENTRYPOINT ["renovate-alert"]
