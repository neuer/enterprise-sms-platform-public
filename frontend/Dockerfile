FROM node:24-alpine@sha256:a0b9bf06e4e6193cf7a0f58816cc935ff8c2a908f81e6f1a95432d679c54fbfd AS build

ARG GIT_SHA=development
ARG SCHEMA_REVISION=development
ENV VITE_GIT_SHA="${GIT_SHA}" \
    VITE_SCHEMA_REVISION="${SCHEMA_REVISION}"

WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend .
COPY VERSION /VERSION
RUN VITE_APP_VERSION="$(tr -d '\n' < /VERSION)" npm run build

FROM nginx:stable-alpine@sha256:0d3b80406a13a767339fbe2f41406d6c7da727ab89cf8fae399e81f780f814d1

ARG APP_VERSION
ARG GIT_SHA
ARG SCHEMA_REVISION
LABEL org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      com.sms-platform.schema-revision="${SCHEMA_REVISION}"

COPY --from=build /app/dist/classic /usr/share/nginx/html
COPY --from=build /app/dist/next /usr/share/nginx/html/next
COPY deploy/nginx-security-headers.conf /etc/nginx/browser-security-headers.conf
COPY deploy/nginx.conf /etc/nginx/nginx.conf

EXPOSE 8080
USER 101:101
