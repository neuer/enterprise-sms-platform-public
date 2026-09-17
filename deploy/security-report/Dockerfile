FROM python:3.12-alpine@sha256:b64631e04e4920160c50fbe8d8df828f7f35f06f425cb44aa09bca53e708a35a

# Dependencies are built by uv (the mailer uses only stdlib); remove unused installers.
RUN python -m pip uninstall --yes pip \
    && rm -rf /usr/local/lib/python3.12/ensurepip

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

RUN apk add --no-cache --upgrade \
      ca-certificates \
      libcrypto3=3.5.8-r0 \
      libuuid=2.42.3-r1 \
      libssl3=3.5.8-r0 \
    && adduser -D -u 10001 security-report

WORKDIR /app

RUN install -d -m 0755 /app/deploy/scripts /app/deploy/templates

COPY --chmod=0644 deploy/scripts/render_security_daily_report.py ./deploy/scripts/
COPY --chmod=0644 deploy/scripts/send_security_daily_report_resend.py ./deploy/scripts/
COPY --chmod=0644 deploy/templates/security_daily_report.html ./deploy/templates/
COPY --chmod=0644 deploy/templates/security_daily_report.txt ./deploy/templates/

USER security-report

ENTRYPOINT ["python", "/app/deploy/scripts/send_security_daily_report_resend.py"]
