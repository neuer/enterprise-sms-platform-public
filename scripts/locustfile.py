"""[HANDOVER] 单用户全日 10 万条 API 受理压测。"""

from __future__ import annotations

import json
import os
import threading
from itertools import count
from pathlib import Path
from typing import Any

from locust import HttpUser, constant_throughput, task  # pyright: ignore[reportMissingImports]

TARGET_TOTAL = 100_000
SECONDS_PER_DAY = 86_400
_sequence = count()
_completed = 0
_lock = threading.Lock()


def _load_keys() -> dict[str, str]:
    raw_path = os.environ.get("PERF_KEYS_FILE", "")
    if not raw_path:
        raise RuntimeError("PERF_KEYS_FILE is required")
    value: Any = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    required = {"app-iam", "app-oa", "app-mkt"}
    if not isinstance(value, dict) or not required.issubset(value):
        raise RuntimeError("PERF_KEYS_FILE is incomplete")
    if any(not isinstance(value[name], str) or not value[name] for name in required):
        raise RuntimeError("PERF_KEYS_FILE contains an invalid key")
    return {name: str(value[name]) for name in required}


KEYS = _load_keys()


class SmsAllDayUser(HttpUser):
    """必须以 -u 1 运行；单用户总吞吐即目标全局吞吐。"""

    wait_time = constant_throughput(TARGET_TOTAL / SECONDS_PER_DAY)

    @staticmethod
    def _phone(sequence: int) -> str:
        return str(18_800_000_000 + sequence)

    def _send(self, app: str, category: str, content: str) -> None:
        global _completed
        sequence = next(_sequence)
        self.client.post(
            "/api/v1/messages/send",
            name=f"/api/v1/messages/send [{category}]",
            headers={"X-Api-Key": KEYS[app]},
            json={
                "category": category,
                "mobiles": [self._phone(sequence)],
                "content": content,
                "biz_id": f"locust-{sequence}",
            },
        )
        with _lock:
            _completed += 1
            if _completed >= TARGET_TOTAL and self.environment.runner is not None:
                environment = self.environment
                environment.runner.quit()

    @task(2)
    def verify(self) -> None:
        self._send("app-iam", "verify", "验证码123456")

    @task(3)
    def notice(self) -> None:
        self._send("app-oa", "notice", "全日性能验收通知")

    @task(5)
    def market(self) -> None:
        self._send("app-mkt", "market", "全日性能验收活动回T退订")
