"""正反静态样例约束发送组合类依赖及数据库连接类型，不接受 Any 掩盖。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_strict_mypy_accepts_real_store_and_rejects_missing_or_wrong_capabilities(tmp_path) -> None:
    backend = Path(__file__).resolve().parents[1]
    fixtures = backend / "tests" / "typing_fixtures"
    base = [sys.executable, "-m", "mypy", "--strict", "--no-incremental",
            "--cache-dir", str(tmp_path / "mypy")]
    good = subprocess.run([*base, str(fixtures / "send_repository_good.py")], cwd=backend,
                          capture_output=True, text=True, check=False, timeout=60)
    assert good.returncode == 0, good.stdout + good.stderr
    bad = subprocess.run([*base, str(fixtures / "send_repository_bad.py")], cwd=backend,
                         capture_output=True, text=True, check=False, timeout=60)
    assert bad.returncode == 1, bad.stdout + bad.stderr
    for expected in ("[arg-type]", "[override]", "[assignment]", "[attr-defined]"):
        assert expected in bad.stdout
    assert "MissingDependencies" in bad.stdout
    assert "_payload" in bad.stdout and "_enqueue_chunk_ready" in bad.stdout
    assert "nonexistent_engine_capability" in bad.stdout
