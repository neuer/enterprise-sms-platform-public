"""API 测试台单页文件的自包含与内存凭据约束。"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAYGROUND = ROOT / "frontend" / "public" / "api-test.html"


def test_playground_is_a_single_self_contained_html_file() -> None:
    source = PLAYGROUND.read_text(encoding="utf-8")

    assert source.lower().startswith("<!doctype html>")
    assert "X-Api-Key" in source
    assert "/api/v1/messages/send" in source
    assert "/api/v1/messages/batches/" in source
    assert "IP_NOT_ALLOWED" in source
    assert "biz_id" in source
    assert 'id="errorBanner"' in source
    assert 'id="sendFeedback"' in source
    assert 'id="selfCheck"' in source
    assert re.search(r'(?:src|href)=["\']https?://', source) is None


def test_playground_keeps_credentials_in_memory_only() -> None:
    source = PLAYGROUND.read_text(encoding="utf-8")

    for forbidden in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
        assert forbidden not in source
    assert 'type="password"' in source
    assert "state.key" in source
