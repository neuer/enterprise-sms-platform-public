FROM python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

RUN apk add --no-cache --upgrade \
      ca-certificates \
      libcrypto3=3.5.8-r0 \
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
