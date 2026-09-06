#!/usr/bin/env python3
"""应用成本限流 writer 切换入口：复用发布/更新控制，不引入新运维平台。

关闭新发送入口、用 compose/进程探测隔离旧 writer，再用 Redis TIME 等待完整
窗口后 CAS 激活 v2。探测适配器必须执行可验证命令，测试再替换该适配器。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
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
class MemoryAdmission:
    state: str = "open"
    reason: str = "ok"
    owned_reason: str | None = None


@dataclass
class ComposeWriterExecutor:
    """用受支持的 compose/进程控制隔离旧 writer，并回查权威探测结果。"""

    runner: CommandRunner
    compose: tuple[str, ...]
    root: Path
    admission: MemoryAdmission = field(default_factory=MemoryAdmission)
    rollback: bool = False

    def close_admission(self, *, reason: str, generation: int):
        cutover = _cutover()
        current = self.query_admission()
        if current.state == "closed" and current.reason not in {reason, ""}:
            return cutover.AdmissionView(state="closed", reason=current.reason, owned=False)
        self.admission.state = "closed"
        self.admission.reason = reason
        self.admission.owned_reason = reason
        _ = generation
        return self.query_admission()

    def query_admission(self):
        cutover = _cutover()
        owned = (
            self.admission.state == "closed"
            and self.admission.reason == self.admission.owned_reason
            and self.admission.reason == cutover.CUTOVER_ADMISSION_REASON
        )
        return cutover.AdmissionView(
            state=self.admission.state,
            reason=self.admission.reason,
            owned=owned,
        )

    def open_admission_if_owned(self, *, reason: str):
        current = self.query_admission()
        if not current.owned or current.reason != reason:
            return current
        self.admission.state = "open"
        self.admission.reason = "ok"
        self.admission.owned_reason = None
        return self.query_admission()

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
        return _cutover().trusted_writer_version(root)


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
        values = [line for line in raw.splitlines() if line != ""]
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
    parser.add_argument("command", choices=("advance", "rollback", "status", "check-launch"))
    parser.add_argument("--root", type=Path, default=ROOT_CANDIDATE)
    parser.add_argument("--release-binding", default="")
    parser.add_argument("--environment", default="development")
    parser.add_argument("--dry-run", action="store_true")
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
    if args.command == "status":
        print(
            json.dumps(
                {
                    "marker_key": cutover.CUTOVER_MARKER_KEY,
                    "writer_protocol_version": cutover.WRITER_PROTOCOL_VERSION,
                    "trusted_writer_version": cutover.trusted_writer_version(root),
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
    print("writer cutover requires an injected redis and executor", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
