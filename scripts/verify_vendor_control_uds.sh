#!/usr/bin/env bash
# 一次性 Linux 容器中验证真实 SO_PEERCRED/UDS/RSA；运行期无网络、无宿主权限。
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ "${SMS_DOCKER_PUBLIC_SESSION:-0}" != "1" ]]; then
  exec "$ROOT/scripts/docker_public.sh" run -- bash "$0" "$@"
fi

test_context="$(mktemp -d "${TMPDIR:-/tmp}/sms-control-uds.XXXXXX")"
test_image="sms-control-uds-${UID}-$$"
cleanup() {
  docker image rm "$test_image" >/dev/null 2>&1 || true
  rm -rf "$test_context"
}
trap cleanup EXIT
cp "$ROOT/backend/pyproject.toml" "$ROOT/backend/uv.lock" "$test_context/"
cat > "$test_context/Dockerfile" <<'DOCKERFILE'
FROM python:3.12-alpine
RUN pip install --no-cache-dir uv==0.7.12
WORKDIR /dependencies
COPY pyproject.toml uv.lock ./
RUN UV_PROJECT_ENVIRONMENT=/opt/test-venv uv sync --locked --no-install-project
DOCKERFILE
docker build --quiet --tag "$test_image" "$test_context" >/dev/null
docker run --rm --network none --cpus 2 --memory 512m --pids-limit 128 --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=64m \
  --volume "$ROOT/backend:/workspace/backend:ro" \
  --volume "$ROOT/deploy/scripts:/workspace/deploy/scripts:ro" \
  --workdir /workspace/backend \
  --env ENVIRONMENT=test --env DEBUG=1 --env AUTH_MOCK=1 --env VENDOR_MOCK=1 \
  --env PYTHONDONTWRITEBYTECODE=1 \
  "$test_image" /opt/test-venv/bin/python -m pytest -q -s -p no:cacheprovider \
    tests/test_vendor_seal_sessions.py tests/test_vendor_control_resources.py
