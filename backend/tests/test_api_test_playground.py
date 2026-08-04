"""API 测试台单页文件的自包含与内存凭据约束。"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAYGROUND = ROOT / "frontend" / "public" / "api-test.html"


def test_playground_is_a_single_self_contained_html_file() -> None:
    source = PLAYGROUND.read_text(encoding="utf-8")

    assert source.lower().startswith("<!doctype html>")
    assert "X-Api-Key" in source
    assert "/api/v1/messages/send" in source
    assert "/api/v1/messages/uat-send" in source
    assert "/api/v1/messages/batches/" in source
    assert "IP_NOT_ALLOWED" in source
    assert "biz_id" in source
    assert 'id="errorBanner"' in source
    assert 'id="sendFeedback"' in source
    assert 'id="selfCheck"' in source
    assert 'id="uatSendBtn"' in source
    assert 'id="uatModeTemplate"' in source
    assert 'id="uatTemplatePreview"' in source
    assert re.search(r'(?:src|href)=["\']https?://', source) is None
    assert re.search(r"<script(?:\s[^>]*)?>", source, re.IGNORECASE) is not None


def test_playground_keeps_credentials_in_memory_only() -> None:
    source = PLAYGROUND.read_text(encoding="utf-8")

    for forbidden in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
        assert forbidden not in source
    assert 'type="password"' in source
    assert "state.key" in source


def test_playground_csp_hashes_match_nginx_route() -> None:
    """内联 script/style 的 CSP 哈希必须与 nginx 单路由白名单一致。"""

    source = PLAYGROUND.read_text(encoding="utf-8")
    nginx = (ROOT / "deploy" / "nginx.conf").read_text(encoding="utf-8")
    location_start = nginx.index("location = /api-test.html {")
    location_text = nginx[location_start:]

    assert "script-src-attr 'none'" in location_text
    assert "script-src 'self' 'unsafe-inline'" not in location_text
    for name in ("script", "style"):
        match = re.search(rf"<{name}>([\s\S]*?)</{name}>", source)
        assert match is not None, name
        digest = base64.b64encode(
            hashlib.sha256(match.group(1).encode("utf-8")).digest()
        ).decode()
        assert f"'sha256-{digest}'" in location_text, f"{name} hash missing in nginx CSP"
