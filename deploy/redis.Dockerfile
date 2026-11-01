FROM redis:7-alpine@sha256:8da19c7399dca900c6822a1cf30f9d2285585e2c4b457ff4095b0f4500054d3f

ARG APP_VERSION
ARG GIT_SHA
ARG SCHEMA_REVISION
LABEL org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      com.sms-platform.schema-revision="${SCHEMA_REVISION}"

COPY --chmod=0555 deploy/redis-domain-entrypoint.sh /usr/local/bin/redis-domain-entrypoint
COPY --chmod=0555 deploy/redis-domain-healthcheck.sh /usr/local/bin/redis-domain-healthcheck

USER 999:1000
ENTRYPOINT ["redis-domain-entrypoint"]
CMD ["broker"]
