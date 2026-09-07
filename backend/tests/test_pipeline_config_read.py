"""受理配置只读必要键，保持每请求新快照与部门隔离。"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.pipeline_repository import SqlPipelineStore
from app.services.runtime_policy import DEFAULTS, RuntimePolicy


class ConfigDatabase:
    def __init__(self) -> None:
        self.config = (
            DEFAULTS
            | {"vendor_batch_size": "400"}
            | {f"unrelated_{index}": "x" * 100 for index in range(100)}
        )
        self.quotas = {"first": 10, "second": 20}
        self.selected_rows: list[int] = []
        self.fail = False

    def connect(self) -> ConfigDatabase:
        return self

    async def __aenter__(self) -> ConfigDatabase:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, sql: object, params: dict[str, Any]) -> Any:
        if self.fail:
            raise ConnectionError("synthetic config failure")
        if "sys_config" in str(sql):
            assert "WHERE key=ANY" in str(sql)
            selected = [
                {"key": key, "value": self.config[key]}
                for key in params["keys"]
                if key in self.config
            ]
            self.selected_rows.append(len(selected))

            class Rows:
                def mappings(self) -> list[dict[str, str]]:
                    return selected

            return Rows()
        quota = self.quotas.get(params["dept"])

        class Quota:
            def scalar_one_or_none(self) -> int | None:
                return quota

        return Quota()


@pytest.mark.asyncio
async def test_config_filter_preserves_policy_and_observes_changes_without_cache() -> None:
    database = ConfigDatabase()
    store = SqlPipelineStore(object())  # type: ignore[arg-type]
    store._engine = lambda: database  # type: ignore[method-assign]
    first = await store.load_config("first")
    assert RuntimePolicy.from_mapping(first) == RuntimePolicy.from_mapping(database.config)
    assert first["vendor_batch_size"] == "400"
    assert first["dept_daily_quota"] == "10"
    assert database.selected_rows == [len(database.config) - 100]
    database.config["unsubscribe_suffix"] = "退订回N"
    database.quotas["second"] = 30
    second = await store.load_config("second")
    assert second["unsubscribe_suffix"] == "退订回N"
    assert second["dept_daily_quota"] == "30"
    assert first["unsubscribe_suffix"] != second["unsubscribe_suffix"]
    assert (await store.load_config("missing"))["dept_daily_quota"] == "0"
    database.fail = True
    with pytest.raises(ConnectionError):
        await store.load_config("first")


@pytest.mark.asyncio
async def test_config_snapshots_do_not_cross_independent_settings() -> None:
    first_db, second_db = ConfigDatabase(), ConfigDatabase()
    second_db.config["vendor_batch_size"] = "37"
    first, second = SqlPipelineStore(object()), SqlPipelineStore(object())  # type: ignore[arg-type]
    first._engine = lambda: first_db  # type: ignore[method-assign]
    second._engine = lambda: second_db  # type: ignore[method-assign]
    assert (await first.load_config("first"))["vendor_batch_size"] == "400"
    assert (await second.load_config("first"))["vendor_batch_size"] == "37"
