FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

FROM python:3.14.9-alpine@sha256:dbeb11d16bb95c3a7e3c25c842cb9f20cbc8c18f8be0461a4a0dcc861ad386a0 AS build

# git: apprise-notify is a git dependency. Installed into a virtualenv, so the
# runtime stage needs neither git nor uv.
RUN apk add --no-cache git
COPY --from=uv /uv /usr/local/bin/uv
# Install exactly what uv.lock pins, checking its hashes, rather than
# re-resolving pyproject.toml at build time. Bytecode is compiled here because
# the runtime image sets PYTHONDONTWRITEBYTECODE and runs read-only.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON=/usr/local/bin/python3 UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_CACHE=1
WORKDIR /tmp/build
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ src/
RUN uv sync --locked --no-dev --no-editable

FROM python:3.14.9-alpine@sha256:dbeb11d16bb95c3a7e3c25c842cb9f20cbc8c18f8be0461a4a0dcc861ad386a0
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 1000:1000
ENTRYPOINT ["renovate-alert"]
