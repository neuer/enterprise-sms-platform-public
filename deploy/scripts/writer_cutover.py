#!/usr/bin/env python3
"""应用成本限流 writer 切换入口：复用发布/更新控制，不引入新运维平台。

关闭新发送入口、用 compose/进程探测隔离旧 writer，再用 Redis TIME 等待完整
窗口后 CAS 激活 v2。探测适配器必须执行可验证命令，测试再替换该适配器。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

SCRIPT_PATH = Path(__file__).resolve()
ROOT_CANDIDATE = SCRIPT_PATH.parents[2]
LOCAL_MARKER_PATH = Path("/var/lib/sms-platform/writer-cutover/state.json")


def _ensure_backend_path(root: Path | None = None) -> None:
    """host-control 副本不在应用树内，必须按 --root 找到 backend。"""

    candidates = []
    if root is not None:
        candidates.append(root / "backend")
    candidates.append(ROOT_CANDIDATE / "backend")
    for backend in candidates:
        if backend.is_dir():
            path = str(backend)
            if path not in sys.path:
                sys.path.insert(0, path)
            return


def _cutover():
    _ensure_backend_path()
    from app.services import app_ratelimit_cutover as cutover

    return cutover


class CommandRunner(Protocol):
    def run(self, argv: list[str], *, timeout_s: int = 30) -> str: ...


class CommandError(RuntimeError):
    def __init__(self, status: str, detail: str = "") -> None:
        super().__init__(detail or status)
        self.status = status
        self.detail = detail


@dataclass
class SubprocessRunner:
    """生产执行器：跑真实命令并解析输出，不用 drained=true 硬编码。"""

    def run(self, argv: list[str], *, timeout_s: int = 30) -> str:
        import subprocess

        try:
            completed = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise CommandError("timeout", type(exc).__name__) from exc
        except OSError as exc:
            raise CommandError("error", type(exc).__name__) from exc
        if completed.returncode != 0:
            raise CommandError("error", f"exit:{completed.returncode}")
        return completed.stdout


@dataclass
class ComposeWriterExecutor:
    """用受支持的 compose/进程控制隔离旧 writer，并回查权威探测结果。"""

    runner: CommandRunner
    compose: tuple[str, ...]
    root: Path
    redis: object | None = None
    rollback: bool = False
    writer_version: int | None = None
    owned_operation: tuple[int, str] | None = None

    def _marker(self):
        if self.redis is None:
            raise _cutover().CutoverError("cutover control plane is unavailable")
        return _cutover().read_cutover_marker(self.redis)

    def close_admission(self, *, reason: str, generation: int):
        cutover = _cutover()
        marker = self._marker()
        if (marker is None or marker.generation != generation
                or marker.admission_reason != reason
                or marker.state not in {"preparing", "old_writers_fenced", "waiting_window"}):
            raise cutover.CutoverError("cutover admission ownership conflict")
        self.owned_operation = (marker.generation, marker.release_binding)
        return self.query_admission()

    def query_admission(self):
        cutover = _cutover()
        # 成本准入的权威事实就是业务 Lua 读取的 marker；独立 Send Admission 不改写。
        marker = self._marker()
        active = (marker is not None and marker.state in {"active_v1", "active_v2"}
                  and not marker.requires_recovery)
        owned = marker is not None and self.owned_operation == (
            marker.generation, marker.release_binding,
        )
        return cutover.AdmissionView(
            state="open" if active else "closed",
            reason=cutover.CUTOVER_ADMISSION_REASON,
            owned=owned,
        )

    def open_admission_if_owned(self, *, reason: str):
        current = self.query_admission()
        if not current.owned or current.reason != reason:
            return current
        # activate CAS 已改变权威准入；这里仅回查同一操作，不存在本地开闸旁路。
        return current

    def isolate_writers(self):
        cutover = _cutover()
        try:
            self.runner.run([*self.compose, "stop", "api"], timeout_s=60)
        except CommandError as exc:
            return cutover.ProbeResult(exc.status, exc.detail)  # type: ignore[arg-type]
        return self.probe_writers()

    def probe_writers(self):
        cutover = _cutover()
        try:
            output = self.runner.run(
                [
                    *self.compose,
                    "ps",
                    "--status",
                    "running",
                    "--format",
                    "{{.Service}}",
                    "api",
                ],
                timeout_s=30,
            )
        except CommandError as exc:
            return cutover.ProbeResult(exc.status, exc.detail)  # type: ignore[arg-type]
        services = {line.strip() for line in output.splitlines() if line.strip()}
        if "api" in services:
            # 运行中的 api 就是仍存活的 writer。不能用当前工作树元数据
            # 把旧容器误判为已隔离：旧二进制不会读 minimum_writer_version。
            return cutover.ProbeResult("present", "api writer still running")
        return cutover.ProbeResult("absent")

    def probe_in_flight_accepts(self):
        cutover = _cutover()
        try:
            output = self.runner.run(
                [
                    *self.compose,
                    "ps",
                    "--status",
                    "running",
                    "--format",
                    "{{.Service}}",
                    "api",
                ],
                timeout_s=30,
            )
        except CommandError as exc:
            return cutover.ProbeResult(exc.status, exc.detail)  # type: ignore[arg-type]
        services = {line.strip() for line in output.splitlines() if line.strip()}
        if "api" in services:
            return cutover.ProbeResult("present", "in-flight accept still running")
        return cutover.ProbeResult("absent")

    def trusted_writer_version(self, root: Path) -> int:
        return self.writer_version or _cutover().trusted_writer_version(root)


@dataclass
class ComposeControlRunner:
    """通过受支持的 compose exec 访问 control Redis。"""

    runner: CommandRunner
    compose: tuple[str, ...]

    def run(self, argv: list[str], *, timeout_s: int = 30) -> str:
        script = (
            'exec redis-cli --user sms_control --askpass --raw "$@" '
            "< /run/secrets/redis_control_password"
        )
        return self.runner.run(
            [
                *self.compose,
                "exec",
                "-T",
                "redis-control",
                "sh",
                "-ec",
                script,
                "sh",
                *argv,
            ],
            timeout_s=timeout_s,
        )


class CliRedis:
    """通过注入的 redis-cli/eval 适配器访问 control Redis，时间只用 Redis TIME。"""

    def __init__(self, eval_impl: CommandRunner, *, prefix: tuple[str, ...] = ()) -> None:
        self.eval_impl = eval_impl
        self.prefix = prefix

    def eval(self, script: object, numkeys: object, *args: object) -> object:
        argv = [
            *self.prefix,
            "EVAL",
            str(script),
            str(numkeys),
            *[str(item) for item in args],
        ]
        raw = self.eval_impl.run(argv, timeout_s=15)
        return _decode_eval_table(raw)

    def hgetall(self, key: str) -> dict[str, str]:
        raw = self.eval_impl.run([*self.prefix, "HGETALL", key], timeout_s=15)
        # redis-cli --raw 对不存在的 Hash 输出单独换行；不能删掉真实字段中的空值。
        if raw in {"", "\n", "\r\n"}:
            return {}
        # 保留空行：greenfield bootstrap 的 release_binding 就是空字符串。
        values = list(raw.splitlines())
        if len(values) % 2 != 0:
            raise _cutover().CutoverError("cutover marker is corrupt")
        return {values[index]: values[index + 1] for index in range(0, len(values), 2)}


def _decode_eval_table(raw: str) -> object:
    lines = [line for line in raw.splitlines()]
    if not lines:
        raise _cutover().CutoverError("cutover control plane is unavailable")
    if len(lines) == 1:
        try:
            return int(lines[0])
        except ValueError:
            return lines[0]
    return lines


def load_local_marker(path: Path = LOCAL_MARKER_PATH) -> object | None:
    cutover = _cutover()
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise cutover.CutoverError("local writer cutover marker is corrupt") from exc
    if not isinstance(document, dict):
        raise cutover.CutoverError("local writer cutover marker is corrupt")
    return cutover.parse_cutover_marker({str(key): str(value) for key, value in document.items()})


def write_local_marker(marker: object, path: Path = LOCAL_MARKER_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(marker.as_hash(), separators=(",", ":"), sort_keys=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload + "\n", encoding="utf-8")
    tmp.replace(path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="writer cutover")
    parser.add_argument(
        "command",
        choices=("advance", "rollback", "status", "check-launch", "bootstrap"),
    )
    parser.add_argument("--root", type=Path, default=ROOT_CANDIDATE)
    parser.add_argument("--release-binding", default="")
    parser.add_argument("--environment", default="development")
    parser.add_argument("--dry-run", action="store_true")
    # REMAINDER：compose 前缀含 --env-file/-f，nargs="+" 会把它们当成未知可选参。
    parser.add_argument("--compose", nargs=argparse.REMAINDER, default=())
    return parser.parse_args(argv)


def _print(result: object) -> int:
    cutover = _cutover()
    payload = result.as_json() if isinstance(result, cutover.CutoverResult) else result
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    if isinstance(result, cutover.CutoverResult):
        return 0 if result.ok else 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve()
    _ensure_backend_path(root)
    cutover = _cutover()
    if args.command == "check-launch":
        try:
            marker = load_local_marker()
            cutover.check_supported_launch(
                root=root,
                marker=marker,
                environment=args.environment,
            )
        except cutover.CutoverError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    if args.command == "bootstrap":
        if not args.compose:
            print("compose command is required", file=sys.stderr)
            return 2
        redis = CliRedis(ComposeControlRunner(SubprocessRunner(), tuple(args.compose)))
        try:
            result = cutover.bootstrap_greenfield_cutover(redis=redis, root=root)
        except cutover.CutoverError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if result.ok:
            marker = cutover.read_cutover_marker(redis)
            if marker is not None:
                try:
                    write_local_marker(marker)
                except OSError:
                    print("local writer marker could not be persisted", file=sys.stderr)
                    return 1
        return _print(result)
    if args.command == "status":
        if not args.compose:
            print("compose command is required for authoritative status", file=sys.stderr)
            return 2
        try:
            redis = CliRedis(ComposeControlRunner(SubprocessRunner(), tuple(args.compose)))
            marker = cutover.read_cutover_marker(redis)
            version = cutover.trusted_writer_version(root)
        except cutover.CutoverError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        ready = (marker is not None and marker.state in {"active_v1", "active_v2"}
                 and not marker.requires_recovery
                 and marker.target_writer_version == version)
        print(
            json.dumps(
                {
                    "marker_key": cutover.CUTOVER_MARKER_KEY,
                    "writer_protocol_version": cutover.WRITER_PROTOCOL_VERSION,
                    "trusted_writer_version": version,
                    "state": marker.state if marker else "missing",
                    "generation": marker.generation if marker else 0,
                    "ready": ready,
                    "error": "" if ready else "controlled writer cutover required",
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0
    if not args.release_binding:
        print("release binding is required", file=sys.stderr)
        return 2
    if args.dry_run:
        print(
            json.dumps(
                {
                    "command": args.command,
                    "release_binding": args.release_binding,
                    "dry_run": True,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0
    if not args.compose:
        print("compose command is required", file=sys.stderr)
        return 2
    runner = SubprocessRunner()
    redis = CliRedis(ComposeControlRunner(runner, tuple(args.compose)))
    executor = ComposeWriterExecutor(runner, tuple(args.compose), root, redis=redis)
    try:
        result = cutover.run_writer_cutover(
            redis=redis, executor=executor, release_binding=args.release_binding,
            root=root, rollback=args.command == "rollback",
        )
        marker = cutover.read_cutover_marker(redis)
        if marker is not None:
            write_local_marker(marker)
    except (cutover.CutoverError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if result.error == "waiting for not_before":
        _print(result)
        return 1
    return _print(result)


if __name__ == "__main__":
    raise SystemExit(main())
