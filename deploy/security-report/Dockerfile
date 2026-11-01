FROM python:3.12-alpine@sha256:e6dae589c1c05976188fd3bd019afd091445fc533ed2ea29001beddc7368a9c8

# Dependencies are built by uv (the mailer uses only stdlib); remove unused installers.
RUN python -m pip uninstall --yes pip \
    && rm -rf /usr/local/lib/python3.12/ensurepip

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

ARG GIT_SHA=development
LABEL org.opencontainers.image.revision="${GIT_SHA}"

RUN adduser -D -u 10001 security-report \
    && sed -i 's/^security-report:!:[0-9]*:/security-report:!:0:/' /etc/shadow \
    && python -c "import ssl; assert ssl.create_default_context().get_ca_certs()"

WORKDIR /app

RUN install -d -m 0755 /app/deploy/scripts /app/deploy/templates

COPY --chmod=0644 deploy/scripts/render_security_daily_report.py ./deploy/scripts/
COPY --chmod=0644 deploy/scripts/send_security_daily_report_resend.py ./deploy/scripts/
COPY --chmod=0644 deploy/templates/security_daily_report.html ./deploy/templates/
COPY --chmod=0644 deploy/templates/security_daily_report.txt ./deploy/templates/

USER security-report

ENTRYPOINT ["python", "/app/deploy/scripts/send_security_daily_report_resend.py"]
