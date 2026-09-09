FROM ghcr.io/astral-sh/uv:0.7.12@sha256:4faec156e35a5f345d57804d8858c6ba1cf6352ce5f4bffc11b7fdebdef46a38 AS uv-bin

FROM python:3.12-alpine@sha256:b64631e04e4920160c50fbe8d8df828f7f35f06f425cb44aa09bca53e708a35a AS builder

# Dependencies are built by uv (the mailer uses only stdlib); remove unused installers.
RUN python -m pip uninstall --yes pip \
    && rm -rf /usr/local/lib/python3.12/ensurepip

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    UV_LINK_MODE=copy

COPY --from=uv-bin /uv /uvx /bin/
WORKDIR /app

COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-alpine@sha256:b64631e04e4920160c50fbe8d8df828f7f35f06f425cb44aa09bca53e708a35a

# Dependencies are built by uv (the mailer uses only stdlib); remove unused installers.
RUN python -m pip uninstall --yes pip \
    && rm -rf /usr/local/lib/python3.12/ensurepip

RUN apk add --no-cache --upgrade \
      libcrypto3=3.5.8-r0 \
      libuuid=2.42.3-r1 \
      libssl3=3.5.8-r0

ARG APP_VERSION
ARG GIT_SHA=development
ARG SCHEMA_REVISION=development

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    GIT_SHA="${GIT_SHA}" \
    SCHEMA_REVISION="${SCHEMA_REVISION}" \
    IMAGE_DIGEST="unresolved"

LABEL org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      com.sms-platform.schema-revision="${SCHEMA_REVISION}"

RUN adduser -D -u 10001 sms \
    && mkdir -p \
      /var/lib/sms/beat \
      /var/lib/sms/imports \
      /var/lib/sms/exports \
      /var/lib/sms/raw-spill \
    && chown -R sms:sms /var/lib/sms \
    && chmod 0700 \
      /var/lib/sms/beat \
      /var/lib/sms/imports \
      /var/lib/sms/exports \
      /var/lib/sms/raw-spill

WORKDIR /app

COPY --from=builder /app/.venv ./.venv

COPY backend/app ./app
COPY backend/alembic.ini ./alembic.ini
COPY backend/migrations ./migrations
COPY backend/scripts_support ./scripts_support
COPY deploy/scripts/vendor_control_protocol.py ./vendor_control_protocol.py
COPY schema.sql ./schema.sql
COPY VERSION ./VERSION

# The release build context is materialized with private directory modes.  The
# runtime user still needs to traverse the application package and migration
# trees, so normalize only those copied runtime trees before dropping root.
RUN find /app/app /app/migrations /app/scripts_support \
      -type d -exec chmod 0755 {} + \
    && find /app/app /app/migrations /app/scripts_support \
      -type f -exec chmod 0644 {} +

USER sms
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--no-access-log"]
