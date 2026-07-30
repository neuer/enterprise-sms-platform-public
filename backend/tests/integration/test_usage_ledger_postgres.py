from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.services.freq import FrequencyLimits
from app.services.quota import QuotaExceeded
from app.services.usage_ledger import (
    APPLY_PROJECTION_LUA,
    APPLY_PROJECTIONS_LUA,
    UsageLedgerService,
    UsageProjectionUnavailable,
    commit_usage_reservation,
    request_usage_release_for_batch,
    shanghai_day,
)

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


class ProjectionRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.fail = False

    async def get(self, name: str) -> str | None:
        if self.fail:
            raise ConnectionError("synthetic redis outage")
        return self.values.get(name)

    async def set(self, name: str, value: Any, **kwargs: Any) -> bool:
        if self.fail:
            raise ConnectionError("synthetic redis outage")
        if kwargs.get("nx") and name in self.values:
            return False
        self.values[name] = str(value)
        return True

    async def mget(self, keys: Sequence[str]) -> list[str | None]:
        if self.fail:
            raise ConnectionError("synthetic redis outage")
        return [self.values.get(key) for key in keys]

    async def eval(self, script: str, numkeys: int, *args: Any) -> int:
        if self.fail:
            raise ConnectionError("synthetic redis outage")
        if script == APPLY_PROJECTION_LUA:
            assert numkeys == 2
            key, version_key, value, version, _expires_at = args
            rows = [(key, version_key, value, version)]
        else:
            assert script == APPLY_PROJECTIONS_LUA and numkeys % 2 == 0
            keys = args[:numkeys]
            values = args[numkeys:]
            rows = [
                (
                    keys[index * 2],
                    keys[index * 2 + 1],
                    values[index * 3],
                    values[index * 3 + 1],
                )
                for index in range(numkeys // 2)
            ]
        applied = 0
        for key, version_key, value, version in rows:
            current_version = int(self.values.get(str(version_key), "-1"))
            if current_version > int(version):
                continue
            self.values[str(key)] = str(value)
            self.values[str(version_key)] = str(version)
            applied += 1
        return applied


async def _create_batch(
    engine: Any,
    *,
    app_id: int,
    reservation_id: UUID,
    category: str,
    quota_cost: int,
) -> tuple[int, str]:
    batch_no = uuid4().hex
    async with engine.begin() as connection:
        result = await connection.execute(
            text(
                """
                INSERT INTO sms_batch(
                  batch_no,category,channel,app_id,creator,dept,content,
                  send_content_enc,segments,quota_cost,status,total,
                  usage_reservation_id
                ) VALUES(
                  :batch_no,:category,'api',:app_id,'integration','账本测试部',
                  'masked',:ciphertext,1,:quota_cost,'queued',1,:reservation_id
                ) RETURNING id
                """
            ),
            {
                "batch_no": batch_no,
                "category": category,
                "app_id": app_id,
                "ciphertext": b"synthetic-ciphertext",
                "quota_cost": quota_cost,
                "reservation_id": reservation_id,
            },
        )
        batch_id = int(result.scalar_one())
        await commit_usage_reservation(
            connection,
            reservation_id=reservation_id,
            batch_id=batch_id,
        )
    return batch_id, batch_no


@pytest.mark.asyncio
async def test_usage_facts_release_rebuild_rotation_and_drift_are_recoverable() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    settings = cast(Any, SimpleNamespace(database_url=database_url))
    redis = ProjectionRedis()
    now = datetime.now(UTC).replace(second=10, microsecond=0)
    date_key, usage_date, next_day = shanghai_day(now)
    nonce = uuid4().hex
    digest_v1 = "a" * 64
    digest_v2 = "b" * 64
    digest_v3 = "c" * 64
    app_id: int | None = None
    reservation_ids: list[UUID] = []
    batch_ids: list[int] = []

    async def cleanup() -> None:
        async with engine.begin() as connection:
            if reservation_ids:
                await connection.execute(
                    text(
                        """
                        DELETE FROM outbox_event
                        WHERE aggregate_id=ANY(CAST(:ids AS text[]))
                        """
                    ),
                    {"ids": [str(value) for value in reservation_ids]},
                )
            if batch_ids:
                await connection.execute(
                    text("DELETE FROM sms_batch WHERE id=ANY(CAST(:ids AS bigint[]))"),
                    {"ids": batch_ids},
                )
            if reservation_ids:
                await connection.execute(
                    text(
                        """
                        DELETE FROM usage_reservation
                        WHERE id=ANY(CAST(:ids AS uuid[]))
                        """
                    ),
                    {"ids": reservation_ids},
                )
            await connection.execute(
                text(
                    """
                    DELETE FROM usage_projection
                    WHERE dimension_key LIKE :quota_pattern
                       OR dimension_key LIKE :verify_pattern
                    """
                ),
                {
                    "quota_pattern": f"quota:app:{app_id or 0}:%",
                    "verify_pattern": "freq:v:%",
                },
            )
            await connection.execute(
                text(
                    """
                    DELETE FROM usage_frequency_subject s
                    WHERE s.projection_hmac=ANY(CAST(:digests AS char(64)[]))
                      AND NOT EXISTS (
                        SELECT 1 FROM usage_frequency_entry e
                        WHERE e.subject_id=s.id
                      )
                    """
                ),
                {"digests": [digest_v1, digest_v2, digest_v3]},
            )
            await connection.execute(
                text(
                    """
                    DELETE FROM audit_log
                    WHERE action='usage_projection_rebuild'
                      AND actor='system:usage-projection-auto'
                    """
                )
            )
            await connection.execute(
                text("DELETE FROM alert_log WHERE dedup_key='usage_projection_drift'")
            )
            await connection.execute(
                text(
                    """
                    UPDATE usage_projection_drift SET
                      mismatched_dimensions=0,absolute_delta=0,checked_at=now()
                    """
                )
            )
            if app_id is not None:
                await connection.execute(
                    text("DELETE FROM app WHERE id=:app_id"),
                    {"app_id": app_id},
                )

    try:
        async with engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    INSERT INTO app(
                      name,dept,api_key_hash,api_key_prefix,daily_quota,created_by
                    ) VALUES(
                      :name,'账本测试部',:api_key_hash,:api_key_prefix,10,'integration'
                    ) RETURNING id
                    """
                ),
                {
                    "name": f"usage-ledger-{nonce}",
                    "api_key_hash": "d" * 64,
                    "api_key_prefix": nonce[:8],
                },
            )
            app_id = int(result.scalar_one())

        service = UsageLedgerService(
            redis,
            settings,
            pooled=False,
            clock=lambda: now,
        )
        first = await service.start_reservation(
            request_key=f"acceptance:{uuid4()}",
            app_id=app_id,
            dept="账本测试部",
            category="verify",
            now=now,
        )
        reservation_ids.append(first.reservation_id)
        assert await service.allow_frequency(
            first.reservation_id,
            "verify",
            app_id=app_id,
            phone_hmac=digest_v2,
            hmac_aliases={1: digest_v1, 2: digest_v2},
            limits=FrequencyLimits(1, 10, 1),
            now=now,
        )
        # 重复决策复用数据库唯一事实，不二次增长。
        assert await service.allow_frequency(
            first.reservation_id,
            "verify",
            app_id=app_id,
            phone_hmac=digest_v2,
            hmac_aliases={1: digest_v1, 2: digest_v2},
            limits=FrequencyLimits(1, 10, 1),
            now=now,
        )
        await service.reserve_quota(
            first.reservation_id,
            app_id=app_id,
            dept="账本测试部",
            category="verify",
            date_key=date_key,
            cost=2,
            app_limit=10,
            dept_limit=10,
            expires_at=next_day,
        )
        first_batch_id, _ = await _create_batch(
            engine,
            app_id=app_id,
            reservation_id=first.reservation_id,
            category="verify",
            quota_cost=2,
        )
        batch_ids.append(first_batch_id)

        # 迁移当天跨 HMAC 版本的历史记录可能先形成分裂主体；新请求须原子归并。
        split_subject_id = uuid4()
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO usage_frequency_subject(id,projection_hmac)
                    VALUES(:id,:projection_hmac)
                    """
                ),
                {"id": split_subject_id, "projection_hmac": digest_v3},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO usage_frequency_alias(
                      subject_id,key_version,phone_hmac
                    ) VALUES(:subject_id,3,:phone_hmac)
                    """
                ),
                {"subject_id": split_subject_id, "phone_hmac": digest_v3},
            )

        later = now + timedelta(minutes=1)
        second = await service.start_reservation(
            request_key=f"acceptance:{uuid4()}",
            app_id=app_id,
            dept="账本测试部",
            category="verify",
            now=later,
        )
        reservation_ids.append(second.reservation_id)
        # 轮换后只要保留版本有交集，就解析为同一不可逆主体。
        assert await service.allow_frequency(
            second.reservation_id,
            "verify",
            app_id=app_id,
            phone_hmac=digest_v3,
            hmac_aliases={2: digest_v2, 3: digest_v3},
            limits=FrequencyLimits(1, 10, 1),
            now=later,
        )
        await service.reserve_quota(
            second.reservation_id,
            app_id=app_id,
            dept="账本测试部",
            category="verify",
            date_key=date_key,
            cost=2,
            app_limit=10,
            dept_limit=10,
            expires_at=next_day,
        )
        second_batch_id, second_batch_no = await _create_batch(
            engine,
            app_id=app_id,
            reservation_id=second.reservation_id,
            category="verify",
            quota_cost=2,
        )
        batch_ids.append(second_batch_id)

        async with engine.begin() as connection:
            assert await request_usage_release_for_batch(
                connection,
                batch_id=first_batch_id,
                event_id=f"usage:{first.reservation_id}:acceptance-failed",
            )

        # PostgreSQL 已持久化 release_requested；Redis 故障不会丢失补偿事实。
        redis.fail = True
        with pytest.raises(UsageProjectionUnavailable):
            await service.apply_release(first.reservation_id)
        async with engine.connect() as connection:
            state = await connection.scalar(
                text("SELECT state FROM usage_reservation WHERE id=:id"),
                {"id": first.reservation_id},
            )
            assert state == "release_requested"
        redis.fail = False
        assert await service.apply_release(first.reservation_id) == 1
        assert await service.apply_release(first.reservation_id) == 0

        minute_key = f"freq:v:{digest_v1}:m"
        day_key = f"freq:v:{digest_v1}:d"
        quota_key = f"quota:app:{app_id}:{date_key}"
        assert redis.values[minute_key] == "1"
        assert redis.values[day_key] == "1"
        assert redis.values[quota_key] == "2"

        async with engine.connect() as connection:
            outbox_count = await connection.scalar(
                text(
                    """
                    SELECT count(*) FROM outbox_event
                    WHERE event_type='usage.release'
                      AND aggregate_id=:reservation_id
                    """
                ),
                {"reservation_id": str(first.reservation_id)},
            )
            subject_count = await connection.scalar(
                text(
                    """
                    SELECT count(DISTINCT subject_id) FROM usage_frequency_alias
                    WHERE phone_hmac=ANY(CAST(:digests AS char(64)[]))
                    """
                ),
                {"digests": [digest_v1, digest_v2, digest_v3]},
            )
            assert int(outbox_count or 0) == 1
            assert int(subject_count or 0) == 1

        # Redis flush 后自动按事实绝对值重建，不能把额度误置零。
        redis.values.clear()
        await service.ensure_ready(later)
        assert redis.values[minute_key] == "1"
        assert redis.values[day_key] == "1"
        assert redis.values[quota_key] == "2"

        explanation = await service.explain(batch_no=second_batch_no)
        serialized = json.dumps(explanation, ensure_ascii=False)
        assert explanation["reservation_id"] == str(second.reservation_id)
        assert explanation["frequency_dimensions"][0]["subject_id"]
        for forbidden in (
            digest_v1,
            digest_v2,
            digest_v3,
            "phone_hmac",
            "phone_enc",
            "13800138000",
        ):
            assert forbidden not in serialized

        redis.values[quota_key] = "99"
        drift = await service.measure_drift()
        assert drift.quota_mismatches >= 1
        assert drift.quota_delta >= 97

        # 崩溃在批次提交前留下的预留由巡检转为唯一释放事件。
        orphan = await service.start_reservation(
            request_key=f"acceptance:{uuid4()}",
            app_id=app_id,
            dept="账本测试部",
            category="notice",
            now=later,
        )
        reservation_ids.append(orphan.reservation_id)
        await service.reserve_quota(
            orphan.reservation_id,
            app_id=app_id,
            dept="账本测试部",
            category="notice",
            date_key=date_key,
            cost=1,
            app_limit=10,
            dept_limit=10,
            expires_at=next_day,
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE usage_reservation
                    SET updated_at=now()-interval '20 minutes'
                    WHERE id=:id
                    """
                ),
                {"id": orphan.reservation_id},
            )
        assert await service.recover_orphans(older_than_seconds=600) == 1
        assert await service.apply_release(orphan.reservation_id) == 1

        # 上海自然日切换后同一不可逆主体进入新窗口，不继承昨日计数。
        following_day = later + timedelta(days=1)
        following_date_key, _, following_day_end = shanghai_day(following_day)
        cross_day = await service.start_reservation(
            request_key=f"acceptance:{uuid4()}",
            app_id=app_id,
            dept="账本测试部",
            category="verify",
            now=following_day,
        )
        reservation_ids.append(cross_day.reservation_id)
        assert await service.allow_frequency(
            cross_day.reservation_id,
            "verify",
            app_id=app_id,
            phone_hmac=digest_v3,
            hmac_aliases={2: digest_v2, 3: digest_v3},
            limits=FrequencyLimits(1, 1, 1),
            now=following_day,
        )
        await service.reserve_quota(
            cross_day.reservation_id,
            app_id=app_id,
            dept="账本测试部",
            category="verify",
            date_key=following_date_key,
            cost=1,
            app_limit=10,
            dept_limit=10,
            expires_at=following_day_end,
        )
        assert await service.request_release(
            cross_day.reservation_id,
            event_id=f"usage:{cross_day.reservation_id}:orphan-recovery",
        )
        assert await service.apply_release(cross_day.reservation_id) == 1

        # marker 丢失时即使权威值全为零，也必须覆盖 Redis 残留值。
        redis.values[day_key] = "99"
        redis.values.pop(f"usage:projection:ready:{following_date_key}", None)
        await service.ensure_ready(following_day)
        assert redis.values[day_key] == "0"
    finally:
        await cleanup()
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_quota_reservations_are_serialized_in_postgres() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    settings = cast(Any, SimpleNamespace(database_url=database_url))
    redis = ProjectionRedis()
    now = datetime.now(UTC).replace(microsecond=0)
    date_key, _, next_day = shanghai_day(now)
    nonce = uuid4().hex

    async with engine.begin() as connection:
        result = await connection.execute(
            text(
                """
                INSERT INTO app(
                  name,dept,api_key_hash,api_key_prefix,daily_quota,created_by
                ) VALUES(
                  :name,'并发账本部',:api_key_hash,:api_key_prefix,1,'integration'
                ) RETURNING id
                """
            ),
            {
                "name": f"usage-concurrency-{nonce}",
                "api_key_hash": "e" * 64,
                "api_key_prefix": nonce[:8],
            },
        )
        app_id = int(result.scalar_one())
    service = UsageLedgerService(redis, settings, pooled=False, clock=lambda: now)
    reservations = [
        await service.start_reservation(
            request_key=f"acceptance:{uuid4()}",
            app_id=app_id,
            dept="并发账本部",
            category="notice",
            now=now,
        )
        for index in range(2)
    ]
    try:
        results = await asyncio.gather(
            *(
                service.reserve_quota(
                    reservation.reservation_id,
                    app_id=app_id,
                    dept="并发账本部",
                    category="notice",
                    date_key=date_key,
                    cost=1,
                    app_limit=1,
                    dept_limit=1,
                    expires_at=next_day,
                )
                for reservation in reservations
            ),
            return_exceptions=True,
        )
        assert sum(result is None for result in results) == 1
        assert sum(isinstance(result, QuotaExceeded) for result in results) == 1
        assert redis.values[f"quota:app:{app_id}:{date_key}"] == "1"
    finally:
        for reservation in reservations:
            await service.request_release(
                reservation.reservation_id,
                event_id=f"usage:{reservation.reservation_id}:orphan-recovery",
            )
            await service.apply_release(reservation.reservation_id)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    DELETE FROM outbox_event
                    WHERE aggregate_id=ANY(CAST(:ids AS text[]))
                    """
                ),
                {"ids": [str(value.reservation_id) for value in reservations]},
            )
            await connection.execute(
                text(
                    """
                    DELETE FROM usage_reservation
                    WHERE id=ANY(CAST(:ids AS uuid[]))
                    """
                ),
                {"ids": [value.reservation_id for value in reservations]},
            )
            await connection.execute(
                text(
                    "DELETE FROM usage_projection WHERE dimension_key LIKE :pattern"
                ),
                {"pattern": f"quota:%:{app_id}:%"},
            )
            await connection.execute(
                text("DELETE FROM app WHERE id=:app_id"),
                {"app_id": app_id},
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_releases_update_shared_projection_without_deadlock() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    settings = cast(Any, SimpleNamespace(database_url=database_url))
    redis = ProjectionRedis()
    now = datetime.now(UTC).replace(microsecond=0)
    date_key, _, next_day = shanghai_day(now)
    nonce = uuid4().hex

    async with engine.begin() as connection:
        result = await connection.execute(
            text(
                """
                INSERT INTO app(
                  name,dept,api_key_hash,api_key_prefix,daily_quota,created_by
                ) VALUES(
                  :name,'并发释放部',:api_key_hash,:api_key_prefix,100,'integration'
                ) RETURNING id
                """
            ),
            {
                "name": f"usage-release-{nonce}",
                "api_key_hash": nonce * 2,
                "api_key_prefix": nonce[:8],
            },
        )
        app_id = int(result.scalar_one())
    service = UsageLedgerService(redis, settings, pooled=False, clock=lambda: now)
    await service.ensure_ready(now)
    reservations = await asyncio.gather(
        *(
            service.start_reservation(
                request_key=f"acceptance:{uuid4()}",
                app_id=app_id,
                dept="并发释放部",
                category="notice",
                now=now,
            )
            for _ in range(16)
        )
    )
    try:
        await asyncio.gather(
            *(
                service.reserve_quota(
                    reservation.reservation_id,
                    app_id=app_id,
                    dept="并发释放部",
                    category="notice",
                    date_key=date_key,
                    cost=1,
                    app_limit=100,
                    dept_limit=100,
                    expires_at=next_day,
                )
                for reservation in reservations
            )
        )
        assert redis.values[f"quota:app:{app_id}:{date_key}"] == "16"

        await asyncio.wait_for(
            asyncio.gather(
                *(
                    service.request_release(
                        reservation.reservation_id,
                        event_id=(
                            f"usage:{reservation.reservation_id}:orphan-recovery"
                        ),
                    )
                    for reservation in reservations
                )
            ),
            timeout=10,
        )
        await asyncio.gather(
            *(
                service.apply_release(reservation.reservation_id)
                for reservation in reservations
            )
        )
        assert redis.values[f"quota:app:{app_id}:{date_key}"] == "0"
    finally:
        reservation_ids = [value.reservation_id for value in reservations]
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    DELETE FROM outbox_event
                    WHERE aggregate_id=ANY(CAST(:ids AS text[]))
                    """
                ),
                {"ids": [str(value) for value in reservation_ids]},
            )
            await connection.execute(
                text(
                    """
                    DELETE FROM usage_reservation
                    WHERE id=ANY(CAST(:ids AS uuid[]))
                    """
                ),
                {"ids": reservation_ids},
            )
            await connection.execute(
                text("DELETE FROM usage_projection WHERE dimension_key LIKE :pattern"),
                {"pattern": f"quota:%:{app_id}:%"},
            )
            await connection.execute(
                text("DELETE FROM app WHERE id=:app_id"),
                {"app_id": app_id},
            )
        await engine.dispose()
