FROM python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

RUN apk add --no-cache ca-certificates \
    && adduser -D -u 10001 security-report

WORKDIR /app

COPY deploy/scripts/render_security_daily_report.py ./deploy/scripts/
COPY deploy/scripts/send_security_daily_report_resend.py ./deploy/scripts/
COPY deploy/templates/security_daily_report.html ./deploy/templates/
COPY deploy/templates/security_daily_report.txt ./deploy/templates/

USER security-report

ENTRYPOINT ["python", "/app/deploy/scripts/send_security_daily_report_resend.py"]
