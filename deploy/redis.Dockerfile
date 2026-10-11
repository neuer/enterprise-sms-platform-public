FROM redis:7-alpine@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499

# Retained signed packages; installation never consults a mutable mirror.
RUN --network=none --mount=type=bind,source=deploy/vendor-apk/redis,target=/apks,ro \
    --mount=type=bind,source=deploy/install-retained-apk.sh,target=/install-retained-apk.sh,ro \
    sh /install-retained-apk.sh

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
