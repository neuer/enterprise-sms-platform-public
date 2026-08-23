"""Import/Export 共用的目录持久化合同。

受控密文根目录的提交顺序必须是：

1. 将完整密文写入同目录临时文件（``.part``）；
2. ``flush`` + ``fsync`` 该文件描述符，使文件数据与元数据落盘；
3. ``os.replace`` 原子改名为最终 ``.smsx``；
4. 收敛最终文件权限（``0600``）；
5. ``fsync`` 密文根目录，使新目录项在断电/重挂载后仍存在；
6. 只有完成以上步骤后，才允许把最终路径写入 PostgreSQL。

禁止在目录 ``fsync`` 之前 ``mark_done`` / 登记 source。日志、指标与调用方
不得把手机号、短信正文、Token、Key 或明文路径详情带入该合同。
"""

from __future__ import annotations

import os
from pathlib import Path


def fsync_directory(path: Path) -> None:
    """打开目录并 fsync，确保最近一次 rename/create 的目录项持久化。"""

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
