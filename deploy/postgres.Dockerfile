FROM postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777 AS prepared

RUN apk add --no-cache su-exec=0.3-r0 \
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
    PG_VERSION=16.14
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
