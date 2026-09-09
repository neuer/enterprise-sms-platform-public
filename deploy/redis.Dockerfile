FROM redis:7-alpine@sha256:ff02b58f971e7d7d156a1267e283fcbbeee91773b6aa36c49dac28ecfe28eadf

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
