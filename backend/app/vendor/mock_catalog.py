"""Mock 厂商模板/签名目录。

只持久化版本、序列和 ID/审核状态。正文、号码、回执与凭据不得进入该文件。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

CATALOG_VERSION = 1
MAX_CATALOG_BYTES = 256 * 1024
MAX_CATALOG_ENTRIES = 100_000


class CatalogError(ValueError):
    """目录缺失以外的损坏、超限或版本不兼容。"""


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    template_sequence: int
    sign_sequence: int
    templates: dict[int, int]
    signs: dict[int, int]

    @classmethod
    def empty(cls) -> CatalogSnapshot:
        return cls(0, 0, {}, {})


@dataclass(frozen=True, slots=True)
class CatalogLoad:
    snapshot: CatalogSnapshot
    status: str


class MockCatalogStore(Protocol):
    def load(self) -> CatalogLoad: ...

    def save(self, snapshot: CatalogSnapshot) -> None: ...


def _validate_snapshot(snapshot: CatalogSnapshot) -> None:
    """序列不得回退到已分配 ID 之下，条目只能是正整数 ID 与审核状态。"""

    if snapshot.template_sequence < 0 or snapshot.sign_sequence < 0:
        raise CatalogError("Mock 目录序列无效")
    if len(snapshot.templates) > MAX_CATALOG_ENTRIES or len(snapshot.signs) > MAX_CATALOG_ENTRIES:
        raise CatalogError("Mock 目录条目过多")
    for label, entries, sequence in (
        ("template", snapshot.templates, snapshot.template_sequence),
        ("sign", snapshot.signs, snapshot.sign_sequence),
    ):
        for item_id, status in entries.items():
            if type(item_id) is not int or item_id < 1 or item_id > sequence:
                raise CatalogError(f"Mock {label} 编号无效")
            if type(status) is not int or status < 0 or status > 9:
                raise CatalogError(f"Mock {label} 状态无效")
        if entries and sequence < max(entries):
            raise CatalogError("Mock 目录序列小于已分配编号")


def _snapshot_payload(snapshot: CatalogSnapshot) -> dict[str, object]:
    _validate_snapshot(snapshot)
    return {
        "version": CATALOG_VERSION,
        "template_sequence": snapshot.template_sequence,
        "sign_sequence": snapshot.sign_sequence,
        "templates": [
            {"id": item_id, "checkType": status}
            for item_id, status in sorted(snapshot.templates.items())
        ],
        "signs": [
            {"id": item_id, "checkType": status}
            for item_id, status in sorted(snapshot.signs.items())
        ],
    }


def snapshot_from_payload(payload: object) -> CatalogSnapshot:
    """只接受版本、序列和 ID/状态；拒绝正文或其他字段。"""

    if not isinstance(payload, dict):
        raise CatalogError("Mock 目录不是对象")
    allowed = {"version", "template_sequence", "sign_sequence", "templates", "signs"}
    if set(payload) != allowed:
        raise CatalogError("Mock 目录字段不符合契约")
    if payload["version"] != CATALOG_VERSION:
        raise CatalogError("Mock 目录版本不受支持")
    if type(payload["template_sequence"]) is not int or type(payload["sign_sequence"]) is not int:
        raise CatalogError("Mock 目录序列无效")

    def entries(raw: object, label: str) -> dict[int, int]:
        if not isinstance(raw, list):
            raise CatalogError(f"Mock {label} 列表无效")
        result: dict[int, int] = {}
        for item in raw:
            if not isinstance(item, dict) or set(item) != {"id", "checkType"}:
                raise CatalogError(f"Mock {label} 条目无效")
            item_id = item["id"]
            status = item["checkType"]
            if type(item_id) is not int or type(status) is not int:
                raise CatalogError(f"Mock {label} 条目无效")
            if item_id in result:
                raise CatalogError(f"Mock {label} 编号重复")
            result[item_id] = status
        return result

    snapshot = CatalogSnapshot(
        payload["template_sequence"],
        payload["sign_sequence"],
        entries(payload["templates"], "template"),
        entries(payload["signs"], "sign"),
    )
    _validate_snapshot(snapshot)
    return snapshot


class MemoryCatalogStore:
    """单元测试用的内存目录，不触碰宿主文件。"""

    persistent = False

    def __init__(self, snapshot: CatalogSnapshot | None = None) -> None:
        self.snapshot = snapshot or CatalogSnapshot.empty()
        _validate_snapshot(self.snapshot)

    def load(self) -> CatalogLoad:
        current = self.snapshot
        return CatalogLoad(
            CatalogSnapshot(
                current.template_sequence,
                current.sign_sequence,
                dict(current.templates),
                dict(current.signs),
            ),
            "ok",
        )

    def save(self, snapshot: CatalogSnapshot) -> None:
        payload = _snapshot_payload(snapshot)
        self.snapshot = snapshot_from_payload(payload)


class FileCatalogStore:
    """同目录临时文件、flush/fsync 后原子替换。"""

    persistent = True

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> CatalogLoad:
        if not self.path.exists():
            return CatalogLoad(CatalogSnapshot.empty(), "missing")
        size = self.path.stat().st_size
        if size > MAX_CATALOG_BYTES:
            raise CatalogError("Mock 目录文件过大")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise CatalogError("Mock 目录无法读取") from error
        return CatalogLoad(snapshot_from_payload(payload), "ok")

    def save(self, snapshot: CatalogSnapshot) -> None:
        """先落盘新序列和目录，调用方再把成功 ID 返回给绑定请求。"""

        payload = _snapshot_payload(snapshot)
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        if len(encoded) > MAX_CATALOG_BYTES:
            raise CatalogError("Mock 目录文件过大")
        directory = self.path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise CatalogError("Mock 目录目录不可写") from error
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        try:
            with temporary.open("wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise CatalogError("Mock 目录写入失败") from error


def catalog_from_env() -> MockCatalogStore:
    """未配置路径时使用内存目录；开发栈通过 MOCK_CATALOG_PATH 指向专用卷。"""

    raw = os.environ.get("MOCK_CATALOG_PATH", "").strip()
    if not raw:
        return MemoryCatalogStore()
    return FileCatalogStore(Path(raw))
