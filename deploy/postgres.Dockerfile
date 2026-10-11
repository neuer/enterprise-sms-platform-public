FROM postgres:16-alpine@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea AS prepared

# Retained signed packages; installation never consults a mutable mirror.
RUN --network=none --mount=type=bind,source=deploy/vendor-apk/postgres,target=/apks,ro \
    --mount=type=bind,source=deploy/install-retained-apk.sh,target=/install-retained-apk.sh,ro \
    sh /install-retained-apk.sh \
    && rm /usr/local/bin/gosu \
    && ln -s /sbin/su-exec /usr/local/bin/gosu

FROM scratch

ARG APP_VERSION
ARG GIT_SHA
ARG SCHEMA_REVISION

COPY --from=prepared / /

ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=en_US.utf8 \
    PG_MAJOR=16 \
    PG_VERSION=16.15
LABEL org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      com.sms-platform.schema-revision="${SCHEMA_REVISION}"
ENV PGDATA=/var/lib/postgresql/data

WORKDIR /
EXPOSE 5432
VOLUME ["/var/lib/postgresql/data"]
USER 70:70
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["postgres"]
STOPSIGNAL SIGINT
