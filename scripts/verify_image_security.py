#!/usr/bin/env python3
"""扫描 G2 已构建镜像或独立 mailer；证据绑定干净源码 SHA 与实际 image ID。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from security_evidence import TRIVY_IMAGE, _database_metadata, summarize

ROOT = Path(__file__).resolve().parents[1]
CORE = ("api", "web", "postgres", "redis")


def command(*args: str) -> str:
    """参数数组执行，失败不发布命令输出或 Docker 原始诊断。"""
    return subprocess.run(args, cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def source_sha() -> str:
    """拒绝将脏工作树或不同提交的镜像标记为当前候选。"""
    if command("git", "status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("unclean source")
    sha = command("git", "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("invalid source SHA")
    if os.environ.get("GITHUB_SHA", sha) != sha:
        raise ValueError("candidate SHA mismatch")
    return sha


def inspect_image(image: str, sha: str) -> tuple[str, str]:
    inspected = json.loads(command("docker", "image", "inspect", image))[0]
    image_id = inspected["Id"]
    platform = f"{inspected['Os']}/{inspected['Architecture']}"
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("invalid image ID")
    if platform != "linux/amd64":
        raise ValueError("unexpected image platform")
    if inspected["Config"]["Labels"].get("org.opencontainers.image.revision") != sha:
        raise ValueError("image source mismatch")
    return image_id, platform


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def scan_summary(raw: object, code: int, image_id: str, database: object) -> dict[str, Any]:
    """扫描内容、镜像身份或漏洞库时效缺失时，不接受 scanner 的零退出码。"""
    result = summarize(raw, code)
    try:
        if not isinstance(raw, dict) or raw["Metadata"]["ImageID"] != image_id:
            raise ValueError("scan image mismatch")
        if not isinstance(database, dict):
            raise ValueError("missing database")
        updated = datetime.fromisoformat(database["updated_at"])
        age = (datetime.now(UTC) - updated).total_seconds()
        if not 0 <= age <= 86400:
            raise ValueError("stale database")
    except (ValueError, KeyError, TypeError):
        result["outcome"] = "tool_error"
    return result


def scan_image(name: str, image: str, sha: str, scratch: Path, output: Path) -> dict[str, Any]:
    """保存并扫描同一 ID，报告只允许聚合字段，SBOM 使用既有规范化器。"""
    image_id, platform = inspect_image(image, sha)
    archive = scratch / f"{name}.tar"
    command("docker", "image", "save", "--output", str(archive), image_id)
    common = (
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--env",
        "HOME=/evidence",
        "-v",
        f"{scratch}:/evidence",
        TRIVY_IMAGE,
    )
    raw = scratch / f"{name}.json"
    with raw.open("w") as stream:
        result = subprocess.run(
            (
                *common,
                "image",
                "--cache-dir",
                "/evidence/cache",
                "--input",
                f"/evidence/{name}.tar",
                "--scanners",
                "vuln",
                "--severity",
                "HIGH,CRITICAL",
                "--exit-code",
                "1",
                "--no-progress",
                "--format",
                "json",
            ),
            cwd=ROOT,
            stdout=stream,
            stderr=subprocess.PIPE,
            check=False,
        )
    database = _database_metadata(scratch / "cache/db/metadata.json")
    try:
        raw_report = json.loads(raw.read_text())
    except (ValueError, OSError):
        raw_report = None
    summary = scan_summary(raw_report, result.returncode, image_id, database)
    evidence = {
        "name": name,
        "image_id": image_id,
        "platform": platform,
        "scanner": TRIVY_IMAGE,
        "database": database,
        "scan_sha256": digest(raw),
        **summary,
    }
    if evidence["outcome"] == "passed":
        sbom_raw = scratch / f"{name}.cdx.json"
        sbom_raw.write_text(
            command(
                *common,
                "image",
                "--cache-dir",
                "/evidence/cache",
                "--input",
                f"/evidence/{name}.tar",
                "--format",
                "cyclonedx",
                "--no-progress",
            )
        )
        sbom = output / f"{name}.cdx.json"
        command(
            sys.executable, str(ROOT / "scripts/canonicalize_sbom.py"), str(sbom_raw), str(sbom)
        )
        evidence["sbom_sha256"] = digest(sbom)
    if name == "mailer" and evidence["outcome"] == "passed":
        retained_archive = output / "mailer.tar"
        shutil.move(str(archive), retained_archive)
        evidence["archive_sha256"] = digest(retained_archive)
    else:
        archive.unlink()
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("g2", "mailer"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("SMS_DOCKER_PUBLIC_SESSION") != "1":
        os.execv(
            str(ROOT / "scripts/docker_public.sh"),
            [
                str(ROOT / "scripts/docker_public.sh"),
                "run",
                "--",
                sys.executable,
                str(Path(__file__).resolve()),
                *sys.argv[1:],
            ],
        )
    os.umask(0o077)
    try:
        sha = source_sha()
        if not args.output.is_absolute() or args.output.exists():
            raise ValueError("evidence output must be a new absolute directory")
        args.output.mkdir(parents=True, mode=0o700)
        images = [(name, f"sms-platform-{name}:local") for name in CORE]
        if args.scope == "mailer":
            image = f"sms-platform-security-report-mailer:scan-{sha}"
            command(
                "docker",
                "build",
                "--pull",
                "--no-cache",
                "--platform",
                "linux/amd64",
                "--build-arg",
                f"GIT_SHA={sha}",
                "--file",
                "deploy/security-report/Dockerfile",
                "--tag",
                image,
                ".",
            )
            # 无网络、无配置挂载，验证标准库 TLS 的信任根与非 root 运行身份。
            command(
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "python",
                image,
                "-c",
                "import os,ssl; assert os.getuid()==10001; "
                "assert ssl.create_default_context().get_ca_certs()",
            )
            images = [("mailer", image)]
        with tempfile.TemporaryDirectory(prefix="sms-image-scan-") as directory:
            results = [
                scan_image(name, image, sha, Path(directory), args.output) for name, image in images
            ]
        if source_sha() != sha:
            raise ValueError("source changed during verification")
        evidence = {
            "schema_version": 1,
            "candidate_sha": sha,
            "scope": args.scope,
            "created_at": datetime.now(UTC).isoformat(),
            "images": results,
            "outcome": "passed" if all(row["outcome"] == "passed" for row in results) else "failed",
        }
        (args.output / "image-security.json").write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n"
        )
        return 0 if evidence["outcome"] == "passed" else 1
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
        print("Image security evidence incomplete; gate remains closed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
