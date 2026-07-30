"""从仓库唯一 VERSION 源读取应用版本并校验构建注入元数据。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_SCHEMA_RE = re.compile(r"[0-9]{4}_[a-z0-9_]+")
_DIGEST_RE = re.compile(
    r"(?:[a-z0-9][a-z0-9._:/-]*@)?sha256:[0-9a-f]{64}|development|unresolved"
)
_DEVELOPMENT_IMAGE_RE = re.compile(
    r"[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})"
)


def _version_file() -> Path:
    candidates = (
        Path(__file__).resolve().parents[2] / "VERSION",
        Path(__file__).resolve().parents[1] / "VERSION",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("VERSION source is unavailable")


def _read_version() -> str:
    value = _version_file().read_text(encoding="ascii").strip()
    if _VERSION_RE.fullmatch(value) is None:
        raise RuntimeError("VERSION source is invalid")
    injected = os.environ.get("APP_VERSION")
    if injected is not None and injected != value:
        raise RuntimeError("APP_VERSION does not match VERSION source")
    return value


@dataclass(frozen=True, slots=True)
class BuildInfo:
    app_version: str
    git_sha: str
    schema_revision: str
    image_digest: str


def current_build_info() -> BuildInfo:
    """开发态使用固定占位；发布构建必须注入可验证的精确身份。"""

    git_sha = os.environ.get("GIT_SHA", "development")
    schema_revision = os.environ.get("SCHEMA_REVISION", "development")
    image_digest = os.environ.get("IMAGE_DIGEST", "development")
    if git_sha != "development" and _COMMIT_RE.fullmatch(git_sha) is None:
        raise RuntimeError("GIT_SHA is invalid")
    if schema_revision != "development" and _SCHEMA_RE.fullmatch(schema_revision) is None:
        raise RuntimeError("SCHEMA_REVISION is invalid")
    digest_is_valid = _DIGEST_RE.fullmatch(image_digest) is not None
    development_tag_is_valid = (
        git_sha == "development"
        and schema_revision == "development"
        and _DEVELOPMENT_IMAGE_RE.fullmatch(image_digest) is not None
    )
    if not digest_is_valid and not development_tag_is_valid:
        raise RuntimeError("IMAGE_DIGEST is invalid")
    return BuildInfo(_read_version(), git_sha, schema_revision, image_digest)


APP_VERSION = _read_version()
