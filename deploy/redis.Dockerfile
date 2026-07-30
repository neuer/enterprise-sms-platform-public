FROM redis:7-alpine@sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99

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
