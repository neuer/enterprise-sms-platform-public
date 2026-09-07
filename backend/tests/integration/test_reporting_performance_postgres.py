"""以真实 PostgreSQL 聚合验证报表分页、完整摘要与有界 JSON。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.api import reports
from app.core.auth.jwt import JwtClaims
from app.services.reporting import ReportingService
from app.services.reporting_repository import SqlReportingRepository

ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ, reason="requires isolated PostgreSQL",
)


@pytest.fixture
async def report_engine() -> AsyncIterator[AsyncEngine]:
    """每例独立合成表，无业务数据与号码，无共享 schema 写入。"""
    url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    assert url.host in {"127.0.0.1", "localhost"}
    schema = f"report_perf_{uuid4().hex}"
    owner = create_async_engine(url)
    async with owner.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.begin() as connection:
            await connection.execute(text("""
                CREATE TABLE app(id integer PRIMARY KEY,name varchar(128),dept varchar(128))
            """))
            await connection.execute(text("""
                CREATE TABLE stat_daily(
                  stat_date date,dim_type text,dim_value text,category text,
                  total bigint,total_segments bigint,delivered bigint,
                  failed bigint,unknown_cnt bigint
                )
            """))
        yield engine
    finally:
        await engine.dispose()
        async with owner.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await owner.dispose()


async def seed(engine: AsyncEngine, *, apps: int, days: int) -> None:
    async with engine.begin() as connection:
        await connection.execute(text("""
            INSERT INTO app SELECT n,repeat('应用',40)||n,
              CASE WHEN n%2=0 THEN 'A' ELSE 'B' END FROM generate_series(1,:apps) n
        """), {"apps": apps})
        await connection.execute(text("""
            INSERT INTO stat_daily
            SELECT DATE '2024-01-01'+d,'app',n::text,'all',n,n*2,n-1,1,0
            FROM generate_series(1,:apps) n CROSS JOIN generate_series(0,:last_day) d
        """), {"apps": apps, "last_day": days-1})


def service_for(engine: AsyncEngine) -> ReportingService:
    repository = SqlReportingRepository(cast(Any, SimpleNamespace(database_url=engine.url)))
    repository._engine = lambda: engine  # type: ignore[method-assign]
    return ReportingService(repository)


@pytest.mark.parametrize("apps", [20, 50])
async def test_large_legal_report_api_and_body_reader_are_bounded(
    report_engine: AsyncEngine, apps: int, tmp_path: Path,
) -> None:
    await seed(report_engine, apps=apps, days=366)
    service = service_for(report_engine)

    class Facade:
        async def verify(self, _: str) -> JwtClaims:
            return JwtClaims("synthetic", "Synthetic", "A", "admin")

    app = FastAPI()
    app.include_router(reports.router)
    app.dependency_overrides[reports.get_reporting_service] = lambda: service
    app.dependency_overrides[reports.get_auth_facade] = Facade
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.get("/api/v1/web/reports/stats", params={
            "start": "2024-01-01", "end": "2024-12-31", "size": 100,
        }, headers={"Authorization": "Bearer synthetic"})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == apps*366 and body["dimension_total"] == apps
    assert len(body["items"]) == 100
    expected = sum(range(1, apps+1))*366
    assert body["summary"]["total"] == expected
    assert sum(sum(series["total"]) for series in body["trend"]["series"]) == expected
    assert len(body["trend"]["periods"]) == 366
    assert len(body["trend"]["series"]) == 6
    assert body["dim_summary"][-1]["is_other"] is True
    assert len(response.content) < 1024*1024
    # 执行生产 TS 正文读取器，禁止只用估算 JSON 字节替代浏览器合同。
    payload = tmp_path / "response.json"
    payload.write_bytes(response.content)
    reader = ROOT / "frontend/src/api/httpDeadline.ts"
    node_script = tmp_path / "read-response.mjs"
    node_script.write_text(
        f"import {{readJsonBody,API_JSON_MAX_BYTES}} from {str(reader.as_uri())!r};\n"
        "import {readFile} from 'node:fs/promises';\n"
        "const payload=await readFile(process.argv[2]);\n"
        "const parsed=await readJsonBody(new Response(payload),"
        "new AbortController().signal,API_JSON_MAX_BYTES);\n"
        f"if(parsed.total!=={apps*366}) process.exitCode=1;\n",
    )
    process = await asyncio.create_subprocess_exec(
        "node", str(node_script), str(payload),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()


async def test_pages_sort_scope_empty_and_global_trend(report_engine: AsyncEngine) -> None:
    await seed(report_engine, apps=7, days=4)
    service = service_for(report_engine)
    filters: dict[str, Any] = {
        "granularity": "day", "group_by": "app", "category": "all",
        "start": date(2024, 1, 1), "end": date(2024, 1, 4), "role": "viewer", "dept": "A",
    }
    seen: list[tuple[date, str]] = []
    summaries = []
    for page in range(1, 5):
        result = await service.get(**filters, page=page, size=3, sort="success_rate", order="asc")
        assert result.total == 12
        seen.extend((row.period_start, row.dim_value) for row in result.items)
        summaries.append(result.summary)
        assert result.trend.periods == tuple(date(2024, 1, day) for day in range(1, 5))
    assert len(seen) == len(set(seen)) == 12
    assert {dim for _, dim in seen} == {"2", "4", "6"}
    assert [dim for _, dim in seen] == ["2"]*4+["4"]*4+["6"]*4
    assert all(summary == summaries[0] for summary in summaries)
    beyond = await service.get(**filters, page=99)
    assert beyond.items == () and beyond.total == 12 and beyond.summary.total == 48
    empty = await service.get(**{**filters, "dept": "absent"})
    assert empty.total == 0 and empty.summary.total == 0 and empty.trend.periods == ()
    all_apps = await service.get(**{**filters, "role": "approver"}, metric="total_segments")
    assert all_apps.dimension_total == 7
    assert all_apps.dim_summary[-1].is_other
    assert sum(item.total for item in all_apps.dim_summary) == sum(range(1, 8))*4
    for sort in ("period_start", "total", "total_segments"):
        ordered = await service.get(**filters, sort=sort, order="desc", size=100)
        keys = [getattr(item, sort) for item in ordered.items]
        assert keys == sorted(keys, reverse=True)
