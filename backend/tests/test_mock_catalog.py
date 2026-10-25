from pathlib import Path

import pytest

from app.vendor.mock_catalog import (
    CatalogError,
    CatalogSnapshot,
    FileCatalogStore,
    MemoryCatalogStore,
)
from app.vendor.mock_server import MockState


def test_file_catalog_survives_restart_and_does_not_reuse_saved_ids(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    store = FileCatalogStore(path)
    state = MockState(store)
    with state.lock:
        first = state._allocate("template", "验证码{s6}")
        second = state._allocate("sign", "青鸾")
    assert first == 1 and second == 1
    raw = path.read_text(encoding="utf-8")
    assert "验证码" not in raw
    assert "青鸾" not in raw

    restarted = MockState(FileCatalogStore(path))
    with restarted.lock:
        third = restarted._allocate("template", "另一模板")
    assert third == 2
    assert restarted.template_ids[1] == 1
    assert 1 not in restarted.template_contents
    assert restarted.template_contents[2] == "另一模板"


def test_volatile_reset_keeps_catalog_and_explicit_reset_clears_it(tmp_path: Path) -> None:
    state = MockState(FileCatalogStore(tmp_path / "catalog.json"))
    with state.lock:
        state._allocate("template", "仅内存")
    state.reset()
    assert state.template_sequence == 1
    assert state.template_contents == {}
    state.reset_catalog_state()
    assert state.template_sequence == 0
    assert state.template_ids == {}
    with state.lock:
        assert state._allocate("template", "重新开始") == 1


def test_corrupt_or_oversized_catalog_is_not_silently_zeroed(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text("{", encoding="utf-8")
    broken = MockState(FileCatalogStore(path))
    assert broken.catalog_status == "invalid"
    with broken.lock:
        assert broken._allocate("template", "不会成功") is None
    assert broken.template_sequence == 0

    huge = tmp_path / "huge.json"
    huge.write_bytes(b"x" * (256 * 1024 + 1))
    with pytest.raises(CatalogError):
        FileCatalogStore(huge).load()

    memory = MemoryCatalogStore()
    memory.save(CatalogSnapshot(2, 0, {1: 1, 2: 1}, {}))
    with pytest.raises(CatalogError):
        memory.save(CatalogSnapshot(1, 0, {2: 1}, {}))
