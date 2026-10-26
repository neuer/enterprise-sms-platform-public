"""CSV/XLSX 手机号流式导入、三列保护与无 PII 剔除清单。"""

from __future__ import annotations

import csv
import re
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from io import StringIO, TextIOWrapper
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol, cast
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from app.services.crypto import CryptoService
from app.services.runtime_policy import RuntimePolicy

_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_CENTRAL_DIRECTORY_SIGNATURE = b"PK\x01\x02"
_EOCD = struct.Struct("<4s4H2LH")
_EOCD_MAX_COMMENT_BYTES = 65_535
_CENTRAL_DIRECTORY_HEADER_BYTES = 46


class ImportTooLarge(ValueError):
    pass


class ImportFormatError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ImportLimits:
    max_bytes: int = 10 * 1024 * 1024
    max_rows: int = 50_000
    expire_hours: int = 24
    max_archive_entries: int = 256
    max_entry_uncompressed_bytes: int = 32 * 1024 * 1024
    max_total_uncompressed_bytes: int = 64 * 1024 * 1024
    max_compression_ratio: int = 200
    max_archive_metadata_bytes: int = 1024 * 1024

    @classmethod
    def from_policy(cls, policy: RuntimePolicy) -> ImportLimits:
        return cls(
            policy.import_max_mb * 1024 * 1024,
            policy.import_max_rows,
            policy.import_expire_hours,
        )


def _find_eocd(source: BinaryIO, archive_size: int) -> tuple[int, tuple[int, ...]]:
    """在 ZIP 尾部有界查找 EOCD，并拒绝截断或伪造的尾记录。"""

    tail_size = min(archive_size, _EOCD.size + _EOCD_MAX_COMMENT_BYTES)
    source.seek(archive_size - tail_size)
    tail = source.read(tail_size)
    if len(tail) != tail_size:
        raise ImportFormatError("XLSX 压缩包尾记录不完整")
    search_end = len(tail)
    while search_end > 0:
        relative_offset = tail.rfind(_EOCD_SIGNATURE, 0, search_end)
        if relative_offset < 0:
            break
        if relative_offset + _EOCD.size <= len(tail):
            fields = _EOCD.unpack_from(tail, relative_offset)
            comment_size = int(fields[-1])
            if relative_offset + _EOCD.size + comment_size == len(tail):
                absolute_offset = archive_size - tail_size + relative_offset
                return absolute_offset, tuple(int(value) for value in fields[1:])
        search_end = relative_offset
    raise ImportFormatError("XLSX 压缩包缺少有效尾记录")


def _preflight_central_directory(source: BinaryIO, limits: ImportLimits) -> None:
    """在构造 ZipInfo 前有界校验中央目录大小、条目数与结构。"""

    source.seek(0, 2)
    archive_size = source.tell()
    if archive_size > limits.max_bytes:
        raise ImportTooLarge("导入文件超过大小限制")
    if archive_size < _EOCD.size:
        raise ImportFormatError("XLSX 压缩包格式无效")

    eocd_offset, fields = _find_eocd(source, archive_size)
    (
        disk_number,
        central_disk_number,
        entries_on_disk,
        declared_entries,
        central_size,
        central_offset,
        _comment_size,
    ) = fields
    if eocd_offset >= 20:
        source.seek(eocd_offset - 20)
        if source.read(4) == _ZIP64_LOCATOR_SIGNATURE:
            raise ImportFormatError("XLSX 不支持 ZIP64 元数据")
    if (
        entries_on_disk == 0xFFFF
        or declared_entries == 0xFFFF
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
    ):
        raise ImportFormatError("XLSX 不支持 ZIP64 元数据")
    if disk_number != 0 or central_disk_number != 0 or entries_on_disk != declared_entries:
        raise ImportFormatError("XLSX 不支持多盘压缩包")
    if declared_entries > limits.max_archive_entries:
        raise ImportTooLarge("XLSX 压缩包条目过多")
    if central_size > limits.max_archive_metadata_bytes:
        raise ImportTooLarge("XLSX 中央目录元数据过大")
    if central_offset + central_size != eocd_offset:
        raise ImportFormatError("XLSX 中央目录边界无效")

    source.seek(central_offset)
    central = source.read(central_size)
    if len(central) != central_size:
        raise ImportFormatError("XLSX 中央目录不完整")
    cursor = 0
    actual_entries = 0
    while cursor < len(central):
        remaining = len(central) - cursor
        if (
            remaining < _CENTRAL_DIRECTORY_HEADER_BYTES
            or central[cursor : cursor + 4] != _CENTRAL_DIRECTORY_SIGNATURE
        ):
            raise ImportFormatError("XLSX 中央目录记录无效")
        filename_size, extra_size, comment_size = struct.unpack_from(
            "<HHH",
            central,
            cursor + 28,
        )
        record_size = _CENTRAL_DIRECTORY_HEADER_BYTES + filename_size + extra_size + comment_size
        if record_size > remaining:
            raise ImportFormatError("XLSX 中央目录记录被截断")
        actual_entries += 1
        if actual_entries > limits.max_archive_entries:
            raise ImportTooLarge("XLSX 压缩包条目过多")
        cursor += record_size
    if actual_entries != declared_entries:
        raise ImportFormatError("XLSX 中央目录条目计数不一致")


_CELL_REF = re.compile(r"^([A-Z]+)([1-9][0-9]{0,6})$")


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _column_index(letters: str) -> int:
    value = 0
    for char in letters:
        if not "A" <= char <= "Z":
            raise ImportFormatError("XLSX 单元格坐标无效")
        value = value * 26 + (ord(char) - 64)
        if value > 16_384:
            raise ImportFormatError("XLSX 只允许一列手机号")
    if value < 1:
        raise ImportFormatError("XLSX 单元格坐标无效")
    return value


class _LimitedReader:
    """限制单条目继续解压的字节数；到达上限后再读到数据即拒绝。"""

    def __init__(self, raw: BinaryIO, limit: int) -> None:
        self.raw = raw
        self.limit = limit
        self.read_bytes = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0 or size > 1024 * 1024:
            size = 1024 * 1024
        if self.read_bytes >= self.limit:
            if self.raw.read(1):
                raise ImportTooLarge("XLSX 工作表解压后过大")
            return b""
        chunk = self.raw.read(min(size, self.limit - self.read_bytes))
        self.read_bytes += len(chunk)
        return chunk


def _relationship_id(element: ElementTree.Element) -> str | None:
    for key, value in element.attrib.items():
        if key == "id" or key.endswith("}id"):
            return value
    return None


def _safe_sheet_path(target: str) -> str:
    normalized = target.split("?", 1)[0].replace("\\", "/")
    if normalized.startswith("/"):
        name = normalized.lstrip("/")
    else:
        name = str(PurePosixPath("xl") / normalized)
    path = PurePosixPath(name)
    if not name.startswith("xl/") or ".." in path.parts or path.is_absolute():
        raise ImportFormatError("XLSX 工作表路径无效")
    return name


def _active_sheet_path(archive: ZipFile) -> str:
    try:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    except (KeyError, ElementTree.ParseError, OSError) as error:
        raise ImportFormatError("XLSX 工作簿无效") from error
    sheets = [item for item in workbook.iter() if _xml_local_name(item.tag) == "sheet"]
    if not sheets:
        raise ImportFormatError("XLSX 不包含工作表")
    views = [item for item in workbook.iter() if _xml_local_name(item.tag) == "workbookView"]
    index = 0
    if views:
        raw_index = views[0].attrib.get("activeTab", "0")
        if not raw_index.isascii() or not raw_index.isdecimal():
            raise ImportFormatError("XLSX 工作表索引无效")
        index = int(raw_index)
    if index < 0 or index >= len(sheets):
        index = 0
    relation_id = _relationship_id(sheets[index])
    if not relation_id:
        raise ImportFormatError("XLSX 工作表关系无效")
    try:
        relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except (KeyError, ElementTree.ParseError, OSError) as error:
        raise ImportFormatError("XLSX 工作表关系无效") from error
    for item in relationships.iter():
        if _xml_local_name(item.tag) != "Relationship":
            continue
        if item.attrib.get("Id") != relation_id:
            continue
        target = item.attrib.get("Target")
        if not target:
            raise ImportFormatError("XLSX 工作表路径无效")
        return _safe_sheet_path(target)
    raise ImportFormatError("XLSX 工作表关系无效")


def _shared_strings(archive: ZipFile, limits: ImportLimits) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    cap = (limits.max_rows + 1) * 4
    strings: list[str] = []
    total_chars = 0
    with archive.open("xl/sharedStrings.xml") as raw:
        reader = _LimitedReader(cast(BinaryIO, raw), limits.max_entry_uncompressed_bytes)
        try:
            for _event, element in ElementTree.iterparse(reader, events=("end",)):
                if _xml_local_name(element.tag) != "si":
                    continue
                text = "".join(
                    (node.text or "") for node in element.iter() if _xml_local_name(node.tag) == "t"
                )
                total_chars += len(text)
                strings.append(text)
                element.clear()
                if len(strings) > cap or total_chars > cap * 64:
                    raise ImportTooLarge("XLSX 共享字符串过多")
        except ElementTree.ParseError as error:
            raise ImportFormatError("XLSX 工作表格式无效") from error
    return strings


def _cell_text(element: ElementTree.Element, shared: list[str]) -> str:
    kind = element.attrib.get("t", "")
    if kind == "inlineStr":
        return "".join(
            (node.text or "") for node in element.iter() if _xml_local_name(node.tag) == "t"
        )
    value = next(
        (
            node.text
            for node in element
            if _xml_local_name(node.tag) == "v" and node.text is not None
        ),
        None,
    )
    if kind == "s":
        if value is None or not value.isascii() or not value.isdecimal():
            raise ImportFormatError("XLSX 共享字符串引用无效")
        index = int(value)
        if index >= len(shared):
            raise ImportFormatError("XLSX 共享字符串引用无效")
        return shared[index]
    return value or ""


def _iter_xlsx_rows(source: BinaryIO, limits: ImportLimits) -> Iterator[tuple[int, str]]:
    """按实际单元格坐标有界读取第一列；缺失或伪造的 dimension 都不能放宽列/行上限。"""

    max_physical_row = limits.max_rows + 1
    max_cells = max_physical_row * 4
    try:
        with ZipFile(source) as archive:
            sheet_name = _active_sheet_path(archive)
            try:
                sheet_info = archive.getinfo(sheet_name)
            except KeyError as error:
                raise ImportFormatError("XLSX 不包含工作表") from error
            if sheet_info.file_size > limits.max_entry_uncompressed_bytes:
                raise ImportTooLarge("XLSX 单个条目解压后过大")
            shared = _shared_strings(archive, limits)
            values: dict[int, str] = {}
            last_row = 0
            seen_cells = 0
            physical_rows = 0
            current_row = 0
            cells_in_row = 0
            dimension_end_row: int | None = None
            with archive.open(sheet_name) as raw:
                reader = _LimitedReader(cast(BinaryIO, raw), limits.max_entry_uncompressed_bytes)
                try:
                    for event, element in ElementTree.iterparse(reader, events=("start", "end")):
                        name = _xml_local_name(element.tag)
                        if event == "start" and name == "row":
                            physical_rows += 1
                            if physical_rows > max_physical_row:
                                raise ImportTooLarge("XLSX 行数超过上限")
                            raw_row = element.attrib.get("r", "")
                            if raw_row:
                                if not raw_row.isascii() or not raw_row.isdecimal():
                                    raise ImportFormatError("XLSX 单元格坐标无效")
                                current_row = int(raw_row)
                            else:
                                current_row += 1
                            cells_in_row = 0
                            if current_row > max_physical_row:
                                raise ImportTooLarge("XLSX 行数超过上限")
                            continue
                        if event != "end":
                            continue
                        if name == "dimension":
                            ref = element.attrib.get("ref", "")
                            end = ref.split(":")[-1].upper()
                            match = _CELL_REF.fullmatch(end)
                            if ref and match is None:
                                raise ImportFormatError("XLSX 维度无效")
                            if match is not None:
                                column = _column_index(match.group(1))
                                row_number = int(match.group(2))
                                if column > 1:
                                    raise ImportFormatError("XLSX 只允许一列手机号")
                                if row_number > max_physical_row:
                                    raise ImportTooLarge("XLSX 行数超过上限")
                                dimension_end_row = row_number
                            element.clear()
                            continue
                        if name == "row":
                            element.clear()
                            continue
                        if name != "c":
                            continue
                        seen_cells += 1
                        if seen_cells > max_cells:
                            raise ImportTooLarge("XLSX 单元格过多")
                        cells_in_row += 1
                        ref = element.attrib.get("r", "").upper()
                        if ref:
                            match = _CELL_REF.fullmatch(ref)
                            if match is None:
                                raise ImportFormatError("XLSX 单元格坐标无效")
                            column = _column_index(match.group(1))
                            row_number = int(match.group(2))
                        else:
                            column = cells_in_row
                            row_number = current_row
                        text = _cell_text(element, shared).strip()
                        element.clear()
                        if row_number > max_physical_row:
                            raise ImportTooLarge("XLSX 行数超过上限")
                        if column > 1:
                            if text:
                                raise ImportFormatError("XLSX 只允许一列手机号")
                            continue
                        if row_number < 1:
                            raise ImportFormatError("XLSX 单元格坐标无效")
                        values[row_number] = text
                        last_row = max(last_row, row_number)
                except ElementTree.ParseError as error:
                    raise ImportFormatError("XLSX 工作表格式无效") from error
            # 声明维度可保留尾部空行，但不能截断已验证坐标的真实单元格。
            end_row = max(last_row, dimension_end_row or 0)
            if end_row > max_physical_row:
                raise ImportTooLarge("XLSX 行数超过上限")
            for row_number in range(1, end_row + 1):
                yield row_number, values.get(row_number, "")
    except (ImportFormatError, ImportTooLarge):
        raise
    except (BadZipFile, OSError, RuntimeError, struct.error) as error:
        raise ImportFormatError("XLSX 压缩包格式无效") from error


@dataclass(frozen=True, slots=True)
class ImportPhone:
    phone_enc: bytes
    phone_hmac: str
    phone_mask: str
    key_version: int
    source_row: int


@dataclass(frozen=True, slots=True)
class RemovedPhone:
    phone_mask: str
    source_row: int
    reason: str


@dataclass(frozen=True, slots=True)
class ImportResult:
    valid: list[ImportPhone]
    removed: list[RemovedPhone]

    @property
    def invalid(self) -> int:
        return sum(item.reason == "invalid" for item in self.removed)

    @property
    def duplicate(self) -> int:
        return sum(item.reason == "duplicate" for item in self.removed)

    @property
    def blacklisted(self) -> int:
        return sum(item.reason == "blacklist" for item in self.removed)

    @property
    def removed_csv(self) -> str:
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(["phone_mask", "source_row", "reason"])
        writer.writerows((item.phone_mask, item.source_row, item.reason) for item in self.removed)
        return output.getvalue()


@dataclass(frozen=True, slots=True)
class ImportParseChunk:
    """至多一个数据库写批次的受保护号码、HMAC aliases 与掩码剔除项。"""

    valid: tuple[ImportPhone, ...]
    candidates_by_active: dict[str, frozenset[str]]
    removed: tuple[RemovedPhone, ...]


class BlacklistMatcher(Protocol):
    async def matches(self, candidates: set[str]) -> set[str]: ...


class ImportParser:
    def __init__(
        self,
        crypto: CryptoService,
        blacklist: BlacklistMatcher,
        *,
        limits: ImportLimits | None = None,
    ) -> None:
        self.crypto = crypto
        self.blacklist = blacklist
        self.limits = limits or ImportLimits()

    def _rows(self, filename: str, source: BinaryIO) -> Iterator[tuple[int, str]]:
        suffix = Path(filename).suffix.casefold()
        if suffix == ".csv":
            wrapper = TextIOWrapper(source, encoding="utf-8-sig", newline="")
            try:
                for row_no, row in enumerate(csv.reader(wrapper), start=1):
                    yield row_no, row[0].strip() if row else ""
            finally:
                wrapper.detach()
            return
        if suffix == ".xlsx":
            yield from _iter_xlsx_rows(source, self.limits)
            return
        raise ImportFormatError("仅支持 csv/xlsx 文件")

    def preflight(self, filename: str, source: BinaryIO, *, size: int) -> None:
        """重型解析前只检查大小、扩展名和 XLSX ZIP 放大风险。"""

        if size < 0 or size > self.limits.max_bytes:
            raise ImportTooLarge("导入文件超过大小限制")
        suffix = Path(filename).suffix.casefold()
        if suffix not in {".csv", ".xlsx"}:
            raise ImportFormatError("仅支持 csv/xlsx 文件")
        if suffix == ".xlsx":
            self._preflight_xlsx(source)
        source.seek(0)

    def _preflight_xlsx(self, source: BinaryIO) -> None:
        """只读检查 XLSX ZIP 元数据，在 openpyxl 解压前限制资源放大。"""

        try:
            source.seek(0)
            _preflight_central_directory(source, self.limits)
            source.seek(0)
            with ZipFile(source) as archive:
                entries = archive.infolist()
                if len(entries) > self.limits.max_archive_entries:
                    raise ImportTooLarge("XLSX 压缩包条目过多")
                total_uncompressed = 0
                total_compressed = 0
                for entry in entries:
                    normalized = entry.filename.replace("\\", "/")
                    path = PurePosixPath(normalized)
                    if (
                        not normalized
                        or normalized.startswith("/")
                        or ".." in path.parts
                        or ":" in path.parts[0]
                        or "\x00" in normalized
                    ):
                        raise ImportFormatError("XLSX 压缩包包含不安全路径")
                    if entry.flag_bits & 0x1:
                        raise ImportFormatError("XLSX 不允许加密条目")
                    if entry.file_size > self.limits.max_entry_uncompressed_bytes:
                        raise ImportTooLarge("XLSX 单个条目解压后过大")
                    entry_ratio_limit = (
                        max(1, entry.compress_size) * self.limits.max_compression_ratio
                    )
                    if entry.file_size > entry_ratio_limit:
                        raise ImportTooLarge("XLSX 单个条目压缩比过高")
                    total_uncompressed += entry.file_size
                    total_compressed += entry.compress_size
                    if total_uncompressed > self.limits.max_total_uncompressed_bytes:
                        raise ImportTooLarge("XLSX 解压后总大小过大")
                if (
                    total_uncompressed
                    > max(1, total_compressed) * self.limits.max_compression_ratio
                ):
                    raise ImportTooLarge("XLSX 总压缩比过高")
        except (ImportFormatError, ImportTooLarge):
            raise
        except (BadZipFile, OSError, RuntimeError, struct.error) as error:
            raise ImportFormatError("XLSX 压缩包格式无效") from error
        finally:
            source.seek(0)

    async def parse(
        self,
        filename: str,
        source: BinaryIO,
        *,
        size: int,
    ) -> ImportResult:
        valid: list[ImportPhone] = []
        removed: list[RemovedPhone] = []
        for chunk in self.iter_chunks(filename, source, size=size):
            blocked_candidates = await self.blacklist.matches(
                set().union(*chunk.candidates_by_active.values())
                if chunk.candidates_by_active
                else set()
            )
            blocked_active = {
                active
                for active, candidates in chunk.candidates_by_active.items()
                if not candidates.isdisjoint(blocked_candidates)
            }
            for item in chunk.valid:
                if item.phone_hmac in blocked_active:
                    removed.append(RemovedPhone(item.phone_mask, item.source_row, "blacklist"))
                else:
                    valid.append(item)
            removed.extend(chunk.removed)
        return ImportResult(valid, removed)

    def iter_chunks(
        self,
        filename: str,
        source: BinaryIO,
        *,
        size: int,
        chunk_size: int = 500,
    ) -> Iterator[ImportParseChunk]:
        """同步分块解析；调用方可逐块查黑名单并批量写库。"""

        if chunk_size < 1 or chunk_size > 2_000:
            raise ValueError("import chunk_size must be 1..2000")
        self.preflight(filename, source, size=size)
        seen: set[str] = set()
        valid: list[ImportPhone] = []
        removed: list[RemovedPhone] = []
        candidates_by_active: dict[str, frozenset[str]] = {}
        data_rows = 0

        def emit() -> ImportParseChunk:
            result = ImportParseChunk(
                tuple(valid),
                dict(candidates_by_active),
                tuple(removed),
            )
            valid.clear()
            candidates_by_active.clear()
            removed.clear()
            return result

        for source_row, value in self._rows(filename, source):
            if source_row == 1 and value.casefold() in {"phone", "mobile", "手机号"}:
                continue
            data_rows += 1
            if data_rows > self.limits.max_rows:
                raise ImportTooLarge("导入文件超过行数限制")
            try:
                protected = self.crypto.protect_phone(value, table="import_phone")
            except ValueError:
                removed.append(RemovedPhone("***********", source_row, "invalid"))
                continue
            if protected.phone_hmac in seen:
                removed.append(RemovedPhone(protected.phone_mask, source_row, "duplicate"))
                continue
            seen.add(protected.phone_hmac)
            candidates_by_active[protected.phone_hmac] = frozenset(
                self.crypto.hmac_candidates(value).values()
            )
            valid.append(
                ImportPhone(
                    protected.phone_enc,
                    protected.phone_hmac,
                    protected.phone_mask,
                    protected.key_version,
                    source_row,
                )
            )
            if len(valid) + len(removed) >= chunk_size:
                yield emit()
        if valid or removed:
            yield emit()
