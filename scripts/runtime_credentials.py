"""供本地验收脚本读取文件型运行凭据，错误信息不得回显凭据内容。"""

from __future__ import annotations

import stat
from pathlib import Path


def read_secret_file(path: Path, *, label: str) -> str:
    """读取 owner-only 非空 secret 文件并去除唯一的行尾换行。"""

    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} file is not a regular file")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError(f"{label} file permissions must be 0600")
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"{label} file is unavailable") from error
    if not value:
        raise ValueError(f"{label} file is empty")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{label} file must contain exactly one line")
    return value
