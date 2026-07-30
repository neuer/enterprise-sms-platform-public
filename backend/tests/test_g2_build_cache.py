from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts/build_g2_images.sh"


def _fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
bin_dir="$(cd "$(dirname "$0")" && pwd -P)"
if [ "${1:-}" = "context" ] && [ "${2:-}" = "show" ]; then
  printf '%s\\n' fake-local
  exit 0
fi
if [ "${1:-}" = "context" ] && [ "${2:-}" = "inspect" ]; then
  printf '%s\\n' unix:///tmp/fake-docker.sock
  exit 0
fi
if [ "${1:-}" = "info" ] && [ "${2:-}" = "--format" ]; then
  printf '[{"Name":"buildx","Path":"%s/docker-buildx"},' "$bin_dir"
  printf '{"Name":"compose","Path":"%s/docker-compose"}]\\n' "$bin_dir"
  exit 0
fi
if [ "${1:-}" != "buildx" ] || [ "${2:-}" != "build" ]; then
  exit 0
fi
if [ "${FAKE_DOCKER_FAIL_MODE:-}" = "always" ]; then
  exit 9
fi
if [ "${FAKE_DOCKER_FAIL_MODE:-}" = "cache-once" ] \
  && [[ " $* " = *" --cache-from "* ]] \
  && [ ! -e "$FAKE_DOCKER_MARKER" ]; then
  : > "$FAKE_DOCKER_MARKER"
  exit 8
fi
if [ "${FAKE_DOCKER_CONCURRENCY_BARRIER:-}" = "1" ]; then
  mkdir -p "$FAKE_DOCKER_BARRIER_DIR"
  : > "$FAKE_DOCKER_BARRIER_DIR/$BASHPID"
  barrier_count=0
  for _ in $(seq 1 200); do
    barrier_count=$(find "$FAKE_DOCKER_BARRIER_DIR" -type f | wc -l | tr -d ' ')
    [ "$barrier_count" -ge 2 ] && break
    sleep 0.01
  done
  [ "$barrier_count" -ge 2 ] || exit 7
fi
destination=""
for argument in "$@"; do
  case "$argument" in
    type=local,dest=*,mode=max)
      destination="${argument#type=local,dest=}"
      destination="${destination%,mode=max}"
      ;;
  esac
done
[ -n "$destination" ]
mkdir -p "$destination"
printf '%s\\n' new-cache > "$destination/index.json"
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    for plugin in ("docker-buildx", "docker-compose"):
        path = fake_bin / plugin
        path.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    return fake_bin, log


def _run_helper(
    tmp_path: Path,
    *,
    fail_mode: str = "",
    concurrency_barrier: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    fake_bin, log = _fake_docker(tmp_path)
    cache = tmp_path / "cache"
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "G2_DOCKER_CACHE_DIR": str(cache),
            "COMPOSE_PROJECT_NAME": "g2-cache-test",
            "FAKE_DOCKER_LOG": str(log),
            "FAKE_DOCKER_FAIL_MODE": fail_mode,
            "FAKE_DOCKER_MARKER": str(tmp_path / "failed-once"),
            "FAKE_DOCKER_CONCURRENCY_BARRIER": "1" if concurrency_barrier else "0",
            "FAKE_DOCKER_BARRIER_DIR": str(tmp_path / "build-barrier"),
        }
    )
    result = subprocess.run(
        ("bash", str(HELPER)),
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, cache, log


def _build_calls(log: Path) -> list[str]:
    return [
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.startswith("buildx build ")
    ]


def test_builds_four_existing_local_tags_with_separate_cache_dirs(tmp_path: Path) -> None:
    result, cache, log = _run_helper(tmp_path)

    assert result.returncode == 0, result.stderr
    builds = _build_calls(log)
    expected = (
        ("api", "sms-platform-api:local", "backend/Dockerfile"),
        ("web", "sms-platform-web:local", "frontend/Dockerfile"),
        ("postgres", "sms-platform-postgres:local", "deploy/postgres.Dockerfile"),
        ("redis", "sms-platform-redis:local", "deploy/redis.Dockerfile"),
    )
    assert len(builds) == len(expected)
    for cache_name, tag, dockerfile in expected:
        call = next(line for line in builds if f"--tag {tag}" in line)
        assert f"--file {dockerfile}" in call
        assert "--load" in call
        assert f"dest={cache / f'{cache_name}.next'},mode=max" in call
        assert call.endswith(str(ROOT))
        assert (cache / cache_name / "index.json").read_text(encoding="utf-8") == (
            "new-cache\n"
        )
    assert log.read_text(encoding="utf-8").splitlines()[-1].startswith("buildx rm ")


def test_four_image_builds_run_concurrently(tmp_path: Path) -> None:
    result, _cache, _log = _run_helper(tmp_path, concurrency_barrier=True)

    assert result.returncode == 0, result.stderr


def test_cache_hit_failure_retries_once_without_cache_from(tmp_path: Path) -> None:
    cache = tmp_path / "cache" / "api"
    cache.mkdir(parents=True)
    (cache / "index.json").write_text("old-cache\n", encoding="utf-8")

    result, cache_root, log = _run_helper(tmp_path, fail_mode="cache-once")

    assert result.returncode == 0, result.stderr
    api_builds = [line for line in _build_calls(log) if "sms-platform-api:local" in line]
    assert len(api_builds) == 2
    assert "--cache-from" in api_builds[0]
    assert "--cache-from" not in api_builds[1]
    assert (cache_root / "api" / "index.json").read_text(encoding="utf-8") == "new-cache\n"


def test_cold_failure_does_not_retry_or_loop(tmp_path: Path) -> None:
    result, _cache, log = _run_helper(tmp_path, fail_mode="always")

    assert result.returncode == 9
    builds = _build_calls(log)
    assert len(builds) == 4
    for tag in (
        "sms-platform-api:local",
        "sms-platform-web:local",
        "sms-platform-postgres:local",
        "sms-platform-redis:local",
    ):
        assert sum(f"--tag {tag}" in call for call in builds) == 1
    assert log.read_text(encoding="utf-8").splitlines()[-1].startswith("buildx rm ")


def test_failed_export_never_replaces_previous_cache(tmp_path: Path) -> None:
    previous = tmp_path / "cache" / "api"
    previous.mkdir(parents=True)
    (previous / "index.json").write_text("old-cache\n", encoding="utf-8")

    result, cache, log = _run_helper(tmp_path, fail_mode="always")

    assert result.returncode == 9
    builds = _build_calls(log)
    api_builds = [call for call in builds if "--tag sms-platform-api:local" in call]
    assert len(api_builds) == 2
    assert "--cache-from" in api_builds[0]
    assert "--cache-from" not in api_builds[1]
    assert (cache / "api" / "index.json").read_text(encoding="utf-8") == "old-cache\n"
    assert not (cache / "api.next").exists()
    assert log.read_text(encoding="utf-8").splitlines()[-1].startswith("buildx rm ")
