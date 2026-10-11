FROM python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4

# Dependencies are built by uv (the mailer uses only stdlib); remove unused installers.
RUN python -m pip uninstall --yes pip \
    && rm -rf /usr/local/lib/python3.12/ensurepip

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

ARG GIT_SHA=development
LABEL org.opencontainers.image.revision="${GIT_SHA}"

RUN adduser -D -u 10001 security-report \
    && python -c "import ssl; assert ssl.create_default_context().get_ca_certs()"

WORKDIR /app

RUN install -d -m 0755 /app/deploy/scripts /app/deploy/templates

COPY --chmod=0644 deploy/scripts/render_security_daily_report.py ./deploy/scripts/
COPY --chmod=0644 deploy/scripts/send_security_daily_report_resend.py ./deploy/scripts/
COPY --chmod=0644 deploy/templates/security_daily_report.html ./deploy/templates/
COPY --chmod=0644 deploy/templates/security_daily_report.txt ./deploy/templates/

USER security-report

ENTRYPOINT ["python", "/app/deploy/scripts/send_security_daily_report_resend.py"]
