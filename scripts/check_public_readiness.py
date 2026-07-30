#!/usr/bin/env python3
"""检查允许进入公开快照的文件；只依赖 Python 标准库。"""

from __future__ import annotations

import argparse
import fnmatch
import ipaddress
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "public-repository.json"
TEXT_SUFFIXES: Final = {
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".vue",
    ".yaml",
    ".yml",
}
DOCUMENT_SUFFIXES: Final = {".html", ".json", ".md", ".txt", ".yaml", ".yml"}
IGNORED_DIRECTORY_NAMES: Final = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "coverage",
    "dist",
    "node_modules",
}
IPV4_PATTERN: Final = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")
PHONE_PATTERN: Final = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
EMAIL_PATTERN: Final = re.compile(
    r"(?<![A-Za-z0-9._%+-])"
    r"[A-Za-z0-9._%+-]+@(?P<domain>[A-Za-z0-9.-]+\.[A-Za-z]{2,})"
)
URL_PATTERN: Final = re.compile(
    r"https?://[^\s`\"'<>\\\[\](),]+",
    re.IGNORECASE,
)
SAFE_PUBLIC_URL_HOSTS: Final = frozenset(
    {
        "127.0.0.1",
        "api.resend.com",
        "files.pythonhosted.org",
        "github.com",
        "localhost",
        "opencollective.com",
        "paulmillr.com",
        "pypi.org",
        "qyapi.weixin.qq.com",
        "registry.npmjs.org",
        "tidelift.com",
    }
)
SAFE_PUBLIC_HOST_SUFFIXES: Final = (
    ".example",
    ".example.com",
    ".example.invalid",
    ".example.internal",
    ".example.test",
    ".internal",
    ".invalid",
    ".test",
    ".trycloudflare.com",
)
SAFE_PUBLIC_EMAIL_SUFFIXES: Final = (
    "example.com",
    "example.invalid",
    "example.test",
)
FORBIDDEN_TEXT_PATTERNS: Final = (
    ("legacy mock password", re.compile(r"Dev@[0-9]{5}")),
    ("legacy LDAP password", re.compile(r"dev-ldap-[A-Za-z0-9_-]+")),
    (
        "legacy fixed API key",
        re.compile(r"dev_(?:iam|oa|mkt)_[A-Za-z0-9_]{16,}"),
    ),
    ("AWS access key", re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}")),
    (
        "GitHub token",
        re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    ),
    ("OpenAI token", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
)


def _is_safe_public_host(hostname: str) -> bool:
    host = hostname.casefold().rstrip(".")
    if host in SAFE_PUBLIC_URL_HOSTS or "." not in host:
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return host.endswith(SAFE_PUBLIC_HOST_SUFFIXES)


def _is_forbidden_tracked_path(relative: str) -> bool:
    path = Path(relative)
    if path.name == ".gitignore":
        return False
    lowered = relative.casefold()
    return (
        lowered == ".env"
        or (path.name.startswith(".env.") and path.name != ".env.example")
        or lowered.startswith("deploy/secrets/")
        or lowered.startswith("deploy/security-report/secrets/")
        or lowered.startswith("deploy/security-report/runtime/")
        or lowered == "deploy/security-report/config/recipients.txt"
        or path.suffix.casefold() in {".key", ".p12", ".pfx"}
    )


def _tracked_paths(root: Path) -> tuple[str, ...]:
    if not (root / ".git").exists():
        return ()
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        return ()
    return tuple(
        item.decode("utf-8")
        for item in completed.stdout.split(b"\0")
        if item
    )


@dataclass(frozen=True, slots=True)
class PublicPolicy:
    excluded_paths: tuple[str, ...]
    required_public_files: tuple[str, ...]
    documentation_phone_allowlist: frozenset[str]


def load_policy(path: Path = POLICY_PATH) -> PublicPolicy:
    """读取公开快照策略并拒绝未知结构。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("public repository policy is unavailable or invalid") from error
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("public repository policy has an unsupported schema")

    def string_tuple(name: str) -> tuple[str, ...]:
        items = value.get(name)
        if (
            not isinstance(items, list)
            or any(not isinstance(item, str) or not item for item in items)
        ):
            raise ValueError(f"public repository policy field is invalid: {name}")
        return tuple(items)

    return PublicPolicy(
        excluded_paths=string_tuple("excluded_paths"),
        required_public_files=string_tuple("required_public_files"),
        documentation_phone_allowlist=frozenset(
            string_tuple("documentation_phone_allowlist")
        ),
    )


def is_excluded(relative_path: str, policy: PublicPolicy) -> bool:
    """判断 POSIX 相对路径是否属于私有研发材料。"""

    normalized = relative_path.strip("/")
    return any(
        fnmatch.fnmatch(normalized, pattern)
        or (
            pattern.endswith("/**")
            and normalized == pattern.removesuffix("/**").rstrip("/")
        )
        for pattern in policy.excluded_paths
    )


def _publishable_files(root: Path, policy: PublicPolicy) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if (
            any(part in IGNORED_DIRECTORY_NAMES for part in path.relative_to(root).parts)
            or is_excluded(relative, policy)
        ):
            continue
        if path.is_file():
            files.append(path)
    return sorted(files)


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _scan_text(relative: str, text: str, policy: PublicPolicy) -> list[str]:
    findings: list[str] = []
    for label, pattern in FORBIDDEN_TEXT_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            findings.append(f"{relative}:{_line_number(text, match.start())}: {label}")

    suffix = Path(relative).suffix.lower()
    for match in URL_PATTERN.finditer(text):
        try:
            hostname = urlsplit(match.group(0)).hostname
        except ValueError:
            continue
        if hostname is not None and not _is_safe_public_host(hostname):
            findings.append(
                f"{relative}:{_line_number(text, match.start())}: "
                "URL host is not approved for public source"
            )
            break

    if suffix in DOCUMENT_SUFFIXES and Path(relative).name not in {
        "package-lock.json",
    }:
        for match in EMAIL_PATTERN.finditer(text):
            domain = match.group("domain").casefold()
            if not domain.endswith(SAFE_PUBLIC_EMAIL_SUFFIXES):
                findings.append(
                    f"{relative}:{_line_number(text, match.start())}: "
                    "non-example email address in publication document"
                )
                break

    for match in IPV4_PATTERN.finditer(text):
        try:
            address = ipaddress.ip_address(match.group(0))
        except ValueError:
            continue
        if address.is_global:
            findings.append(
                f"{relative}:{_line_number(text, match.start())}: "
                "globally routable IPv4 address in publishable file"
            )
            break

    if (
        relative.startswith("docs/")
        and relative not in policy.documentation_phone_allowlist
        and suffix in DOCUMENT_SUFFIXES
    ):
        match = PHONE_PATTERN.search(text)
        if match is not None:
            findings.append(
                f"{relative}:{_line_number(text, match.start())}: "
                "full mobile number in publication document"
            )
    return findings


def _scan_data(relative: str, data: bytes, policy: PublicPolicy) -> list[str]:
    findings: list[str] = []
    private_key_start = b"-----" + b"BEGIN "
    private_key_end = b"PRIVATE " + b"KEY-----"
    private_key_headers = tuple(
        private_key_start + prefix + private_key_end
        for prefix in (b"", b"RSA ", b"EC ", b"OPENSSH ")
    )
    if any(header in data for header in private_key_headers):
        findings.append(f"{relative}: private key material")
    path = Path(relative)
    if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {
        "LICENSE",
        "Dockerfile",
    }:
        return findings
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        findings.append(f"{relative}: expected text file is not UTF-8")
        return findings
    findings.extend(_scan_text(relative, text, policy))
    return findings


def check_git_commit(
    root: Path,
    *,
    commit: str,
    policy: PublicPolicy,
) -> list[str]:
    """扫描一个待推送 Git tree；结果不回显任何命中值。"""

    resolved = subprocess.run(
        ["git", "rev-parse", "--verify", f"{commit}^{{commit}}"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if resolved.returncode != 0:
        return [f"{commit}: unable to resolve Git commit for public scan"]
    commit_sha = resolved.stdout.strip()
    listed = subprocess.run(
        ["git", "ls-tree", "-r", "-z", "--name-only", commit_sha],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if listed.returncode != 0:
        return [f"{commit_sha}: unable to list Git tree for public scan"]

    findings: list[str] = []
    for raw_relative in listed.stdout.split(b"\0"):
        if not raw_relative:
            continue
        try:
            relative = raw_relative.decode("utf-8")
        except UnicodeDecodeError:
            findings.append(f"{commit_sha}: non-UTF-8 Git path")
            continue
        path = Path(relative)
        if _is_forbidden_tracked_path(relative) or (
            is_excluded(relative, policy) and path.name != ".gitignore"
        ):
            findings.append(
                f"{commit_sha}:{relative}: private-only path in public Git tree"
            )
            continue
        blob = subprocess.run(
            ["git", "show", f"{commit_sha}:{relative}"],
            cwd=root,
            check=False,
            capture_output=True,
        )
        if blob.returncode != 0:
            findings.append(f"{commit_sha}:{relative}: unable to read Git blob")
            continue
        findings.extend(
            f"{commit_sha}:{finding}"
            for finding in _scan_data(relative, blob.stdout, policy)
        )
    return findings


def check_repository(
    root: Path,
    *,
    policy: PublicPolicy,
    snapshot: bool = False,
) -> list[str]:
    """返回全部公开阻断项，信息只含位置与类别，不回显命中值。"""

    findings: list[str] = []
    for relative in _tracked_paths(root):
        if _is_forbidden_tracked_path(relative):
            findings.append(f"{relative}: sensitive local path is tracked by Git")
    for required in policy.required_public_files:
        if not (root / required).is_file():
            findings.append(f"{required}: required public repository file is missing")
    if snapshot:
        if (root / ".git").exists():
            findings.append(".git: public snapshot must not contain Git history")
        if not (root / "PUBLIC-SNAPSHOT.json").is_file():
            findings.append("PUBLIC-SNAPSHOT.json: snapshot provenance is missing")
        for path in root.rglob("*"):
            relative = path.relative_to(root).as_posix()
            if is_excluded(relative, policy):
                findings.append(f"{relative}: private-only path is present in snapshot")

    for path in _publishable_files(root, policy):
        relative = path.relative_to(root).as_posix()
        try:
            data = path.read_bytes()
        except OSError:
            findings.append(f"{relative}: file is unreadable")
            continue
        findings.extend(_scan_data(relative, data, policy))
    return findings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--policy", type=Path, default=POLICY_PATH)
    parser.add_argument("--snapshot", action="store_true")
    parser.add_argument(
        "--git-range",
        help="also scan every commit reachable from this git rev-list expression",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = args.root.resolve()
    try:
        policy = load_policy(args.policy.resolve())
    except ValueError as error:
        print(f"FAIL: {error}")
        return 1
    findings = check_repository(root, policy=policy, snapshot=args.snapshot)
    if args.git_range:
        commits = subprocess.run(
            ["git", "rev-list", "--reverse", args.git_range],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        if commits.returncode != 0:
            findings.append("unable to resolve --git-range for public scan")
        else:
            for commit in commits.stdout.splitlines():
                findings.extend(check_git_commit(root, commit=commit, policy=policy))
    if findings:
        for finding in findings:
            print(f"FAIL: {finding}")
        print(f"公开仓库门禁失败: {len(findings)} 项")
        return 1
    print(
        f"公开仓库门禁通过: files={len(_publishable_files(root, policy))} "
        f"snapshot={str(args.snapshot).lower()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
