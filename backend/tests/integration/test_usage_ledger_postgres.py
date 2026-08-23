from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
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
    _ACTIVE_RESERVATION_STATES,
    APPLY_PROJECTION_LUA,
    APPLY_PROJECTIONS_LUA,
    FREQUENCY_MERGE_FUTURE_DAY_SKEW,
    FREQUENCY_MERGE_FUTURE_MINUTE_SKEW,
    FrequencyDecisionItem,
    UsageLedgerService,
    UsageProjectionUnavailable,
    UsageReservationConflict,
    _ensure_frequency_subject,
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
        self.fail_eval = False

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
        if self.fail or self.fail_eval:
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
                  display_content_enc,send_content_enc,segments,quota_cost,status,total,
                  usage_reservation_id
                ) VALUES(
                  :batch_no,:category,'api',:app_id,'integration','账本测试部',
                  '[encrypted]',:ciphertext,:ciphertext,1,:quota_cost,'queued',1,
                  :reservation_id
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
        assert not await service.request_unlinked_release(
            first.reservation_id,
            event_id=f"usage:{first.reservation_id}:ambiguous-save",
        )
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT state FROM usage_reservation WHERE id=:id"),
                    {"id": first.reservation_id},
                )
                == "committed"
            )

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
async def test_uncertain_retry_rebuilds_and_orphan_recovery_spares_committed() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    settings = cast(Any, SimpleNamespace(database_url=database_url))
    redis = ProjectionRedis()
    now = datetime.now(UTC).replace(second=10, microsecond=0)
    date_key, _, next_day = shanghai_day(now)
    nonce = uuid4().hex
    reservation_ids: list[UUID] = []
    batch_ids: list[int] = []
    app_id: int | None = None
    try:
        async with engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    INSERT INTO app(
                      name,dept,api_key_hash,api_key_prefix,daily_quota,created_by
                    ) VALUES(
                      :name,'重试账本部',:api_key_hash,:api_key_prefix,10,'integration'
                    ) RETURNING id
                    """
                ),
                {
                    "name": f"usage-retry-{nonce}",
                    "api_key_hash": "f" * 64,
                    "api_key_prefix": nonce[:8],
                },
            )
            app_id = int(result.scalar_one())
        service = UsageLedgerService(redis, settings, pooled=False, clock=lambda: now)
        await service.ensure_ready(now)
        request_key = f"acceptance:{uuid4()}"
        first = await service.start_reservation(
            request_key=request_key,
            app_id=app_id,
            dept="重试账本部",
            category="notice",
            now=now,
        )
        reservation_ids.append(first.reservation_id)
        assert not first.reused

        # Redis 投影写失败 → 预留转 uncertain（历史缺陷：CHECK 拒绝
        # uncertain-retry 事件导致同键重试持续 500）。只让写脚本失败，
        # ensure_ready 的 marker 读取保持可用。
        redis.fail_eval = True
        with pytest.raises(UsageProjectionUnavailable):
            await service.reserve_quota(
                first.reservation_id,
                app_id=app_id,
                dept="重试账本部",
                category="notice",
                date_key=date_key,
                cost=1,
                app_limit=10,
                dept_limit=10,
                expires_at=next_day,
            )
        redis.fail_eval = False
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT state FROM usage_reservation WHERE id=:id"),
                    {"id": first.reservation_id},
                )
                == "uncertain"
            )

        # 同 request_key 重试必须当场重建全新预留，旧行进入排水。
        second = await service.start_reservation(
            request_key=request_key,
            app_id=app_id,
            dept="重试账本部",
            category="notice",
            now=now,
        )
        reservation_ids.append(second.reservation_id)
        assert second.reservation_id != first.reservation_id
        assert not second.reused
        async with engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            """
                        SELECT state,release_event_id FROM usage_reservation
                        WHERE id=:id
                        """
                        ),
                        {"id": first.reservation_id},
                    )
                )
                .mappings()
                .one()
            )
        assert row["state"] == "release_requested"
        assert row["release_event_id"] == f"usage:{first.reservation_id}:uncertain-retry"
        assert await service.apply_release(first.reservation_id) == 1

        await service.reserve_quota(
            second.reservation_id,
            app_id=app_id,
            dept="重试账本部",
            category="notice",
            date_key=date_key,
            cost=1,
            app_limit=10,
            dept_limit=10,
            expires_at=next_day,
        )
        batch_id, batch_no = await _create_batch(
            engine,
            app_id=app_id,
            reservation_id=second.reservation_id,
            category="notice",
            quota_cost=1,
        )
        batch_ids.append(batch_id)

        # 已提交（已绑定批次）的预留即使 updated_at 陈旧也不得被孤儿回收
        # 释放，否则配额双花且终态释放永久 409。
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE usage_reservation
                    SET state='reserved',updated_at=now()-interval '20 minutes'
                    WHERE id=:id
                    """
                ),
                {"id": second.reservation_id},
            )
        assert await service.recover_orphans(older_than_seconds=600) == 0
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE usage_reservation SET state='committed' WHERE id=:id"),
                {"id": second.reservation_id},
            )

        # 终态业务释放入口保持可用：驳回/取消不会因回收竞态永久失败。
        async with engine.begin() as connection:
            assert await request_usage_release_for_batch(
                connection,
                batch_id=batch_id,
                event_id=f"batch:{batch_no}:cancelled",
            )
        assert await service.apply_release(second.reservation_id) == 1
    finally:
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
            if app_id is not None:
                await connection.execute(
                    text("DELETE FROM usage_projection WHERE dimension_key LIKE :pattern"),
                    {"pattern": f"quota:%:{app_id}:%"},
                )
                await connection.execute(
                    text("DELETE FROM app WHERE id=:app_id"),
                    {"app_id": app_id},
                )
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
                text("DELETE FROM usage_projection WHERE dimension_key LIKE :pattern"),
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
                        event_id=(f"usage:{reservation.reservation_id}:orphan-recovery"),
                    )
                    for reservation in reservations
                )
            ),
            timeout=10,
        )
        await asyncio.gather(
            *(service.apply_release(reservation.reservation_id) for reservation in reservations)
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


@pytest.mark.asyncio
async def test_recover_orphans_retries_stuck_release_requested() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    settings = cast(Any, SimpleNamespace(database_url=database_url))
    redis = ProjectionRedis()
    now = datetime.now(UTC).replace(second=10, microsecond=0)
    date_key, _, next_day = shanghai_day(now)
    nonce = uuid4().hex
    reservation_id: UUID | None = None
    app_id: int | None = None
    try:
        async with engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    INSERT INTO app(
                      name,dept,api_key_hash,api_key_prefix,daily_quota,created_by
                    ) VALUES(
                      :name,'死信账本部',:api_key_hash,:api_key_prefix,10,'integration'
                    ) RETURNING id
                    """
                ),
                {
                    "name": f"usage-dead-{nonce}",
                    "api_key_hash": "e" * 64,
                    "api_key_prefix": nonce[:8],
                },
            )
            app_id = int(result.scalar_one())
        service = UsageLedgerService(redis, settings, pooled=False, clock=lambda: now)
        await service.ensure_ready(now)
        reservation = await service.start_reservation(
            request_key=f"acceptance:{uuid4()}",
            app_id=app_id,
            dept="死信账本部",
            category="notice",
            now=now,
        )
        reservation_id = reservation.reservation_id
        await service.reserve_quota(
            reservation_id,
            app_id=app_id,
            dept="死信账本部",
            category="notice",
            date_key=date_key,
            cost=1,
            app_limit=10,
            dept_limit=10,
            expires_at=next_day,
        )
        assert await service.request_unlinked_release(
            reservation_id,
            event_id=f"usage:{reservation_id}:acceptance-failed",
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
                {"id": reservation_id},
            )
        assert await service.recover_orphans(older_than_seconds=600) == 1
        async with engine.connect() as connection:
            state = await connection.scalar(
                text("SELECT state FROM usage_reservation WHERE id=:id"),
                {"id": reservation_id},
            )
        assert state == "released"
    finally:
        async with engine.begin() as connection:
            if reservation_id is not None:
                await connection.execute(
                    text("DELETE FROM outbox_event WHERE aggregate_id=:id"),
                    {"id": str(reservation_id)},
                )
                await connection.execute(
                    text("DELETE FROM usage_reservation WHERE id=:id"),
                    {"id": reservation_id},
                )
            if app_id is not None:
                await connection.execute(
                    text("DELETE FROM usage_projection WHERE dimension_key LIKE :pattern"),
                    {"pattern": f"quota:%:{app_id}:%"},
                )
                await connection.execute(
                    text("DELETE FROM app WHERE id=:app_id"),
                    {"app_id": app_id},
                )
        await engine.dispose()


def _unique_digest(prefix: str) -> str:
    return (prefix + uuid4().hex + uuid4().hex)[:64]


async def _insert_frequency_projection(
    connection: Any,
    *,
    dimension_key: str,
    usage_date: date,
    window_key: str,
    value: int,
    expires_at: datetime,
) -> None:
    await connection.execute(
        text(
            """
            INSERT INTO usage_projection(
              dimension_key,kind,usage_date,window_key,value,version,expires_at
            ) VALUES(
              :dimension_key,'frequency',:usage_date,:window_key,:value,
              nextval('usage_projection_version_seq'),:expires_at
            )
            """
        ),
        {
            "dimension_key": dimension_key,
            "usage_date": usage_date,
            "window_key": window_key,
            "value": value,
            "expires_at": expires_at,
        },
    )


async def _create_test_app(connection: Any, name: str) -> int:
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
            "name": name,
            "api_key_hash": uuid4().hex + uuid4().hex[:32],
            "api_key_prefix": name[-8:],
        },
    )
    return int(result.scalar_one())


@pytest.mark.asyncio
async def test_frequency_subject_merge_combines_live_same_window_projections() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    settings = cast(Any, SimpleNamespace(database_url=database_url))
    redis = ProjectionRedis()
    now = datetime.now(UTC).replace(second=10, microsecond=0)
    date_key, usage_date, next_day = shanghai_day(now)
    previous_date = usage_date - timedelta(days=1)
    minute_window = str(int(now.timestamp() // 60))
    digest_a = _unique_digest("aa")
    digest_b = _unique_digest("bb")
    subject_a = uuid4()
    subject_b = uuid4()
    app_ids: list[int] = []
    item = FrequencyDecisionItem(
        phone_hmac=digest_b,
        hmac_aliases={1: digest_a, 2: digest_b},
    )

    async def cleanup() -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    DELETE FROM usage_frequency_entry
                    WHERE subject_id IN (
                      SELECT id FROM usage_frequency_subject
                      WHERE projection_hmac=ANY(CAST(:digests AS char(64)[]))
                    )
                    """
                ),
                {"digests": [digest_a, digest_b]},
            )
            await connection.execute(
                text(
                    """
                    DELETE FROM usage_frequency_subject
                    WHERE projection_hmac=ANY(CAST(:digests AS char(64)[]))
                    """
                ),
                {"digests": [digest_a, digest_b]},
            )
            await connection.execute(
                text(
                    """
                    DELETE FROM usage_projection
                    WHERE dimension_key LIKE ANY(CAST(:patterns AS text[]))
                    """
                ),
                {
                    "patterns": [
                        f"freq:v:{digest_a}:%",
                        f"freq:v:{digest_b}:%",
                        f"freq:m:%:{digest_a}:d",
                        f"freq:m:%:{digest_b}:d",
                    ]
                },
            )
            if app_ids:
                await connection.execute(
                    text("DELETE FROM app WHERE id=ANY(CAST(:ids AS bigint[]))"),
                    {"ids": app_ids},
                )

    try:
        async with engine.begin() as connection:
            same_app = await _create_test_app(connection, f"freq-merge-s-{uuid4().hex[:8]}")
            source_only_app = await _create_test_app(connection, f"freq-merge-o-{uuid4().hex[:8]}")
            window_app = await _create_test_app(connection, f"freq-merge-w-{uuid4().hex[:8]}")
            expired_app = await _create_test_app(connection, f"freq-merge-e-{uuid4().hex[:8]}")
            app_ids.extend((same_app, source_only_app, window_app, expired_app))
            await connection.execute(
                text(
                    """
                    INSERT INTO usage_frequency_subject(id,projection_hmac)
                    VALUES(:id_a,:hmac_a),(:id_b,:hmac_b)
                    """
                ),
                {
                    "id_a": subject_a,
                    "hmac_a": digest_a,
                    "id_b": subject_b,
                    "hmac_b": digest_b,
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO usage_frequency_alias(
                      subject_id,key_version,phone_hmac
                    ) VALUES
                      (:id_a,1,:digest_a),
                      (:id_b,2,:digest_b)
                    """
                ),
                {
                    "id_a": subject_a,
                    "id_b": subject_b,
                    "digest_a": digest_a,
                    "digest_b": digest_b,
                },
            )
            for digest in (digest_a, digest_b):
                await _insert_frequency_projection(
                    connection,
                    dimension_key=f"freq:v:{digest}:m",
                    usage_date=usage_date,
                    window_key=minute_window,
                    value=1,
                    expires_at=now + timedelta(minutes=1),
                )
                await _insert_frequency_projection(
                    connection,
                    dimension_key=f"freq:v:{digest}:d",
                    usage_date=usage_date,
                    window_key=date_key,
                    value=1,
                    expires_at=next_day,
                )
                await _insert_frequency_projection(
                    connection,
                    dimension_key=f"freq:m:{same_app}:{digest}:d",
                    usage_date=usage_date,
                    window_key=date_key,
                    value=1,
                    expires_at=next_day,
                )
            await _insert_frequency_projection(
                connection,
                dimension_key=f"freq:m:{source_only_app}:{digest_b}:d",
                usage_date=usage_date,
                window_key=date_key,
                value=1,
                expires_at=next_day,
            )
            await _insert_frequency_projection(
                connection,
                dimension_key=f"freq:m:{window_app}:{digest_a}:d",
                usage_date=usage_date,
                window_key=date_key,
                value=1,
                expires_at=next_day,
            )
            await _insert_frequency_projection(
                connection,
                dimension_key=f"freq:m:{window_app}:{digest_b}:d",
                usage_date=previous_date,
                window_key="19990101",
                value=5,
                expires_at=next_day,
            )
            await _insert_frequency_projection(
                connection,
                dimension_key=f"freq:m:{expired_app}:{digest_b}:d",
                usage_date=previous_date,
                window_key="19990101",
                value=8,
                expires_at=now - timedelta(minutes=1),
            )

        async def merge_once() -> tuple[UUID, str, int]:
            async with engine.begin() as connection:
                subject_id, hmac, rows = await _ensure_frequency_subject(connection, item)
                return subject_id, hmac, len(rows)

        first = await merge_once()
        second = await merge_once()
        assert first[0] == second[0] == subject_a
        assert first[1] == second[1] == digest_a
        assert second[2] == 0

        async with engine.connect() as connection:
            subjects = await connection.scalar(
                text(
                    """
                    SELECT count(*) FROM usage_frequency_subject
                    WHERE projection_hmac=ANY(CAST(:digests AS char(64)[]))
                    """
                ),
                {"digests": [digest_a, digest_b]},
            )
            assert int(subjects or 0) == 1
            values = {
                str(row["dimension_key"]): (
                    int(row["value"]),
                    str(row["window_key"]),
                    row["usage_date"],
                )
                for row in (
                    await connection.execute(
                        text(
                            """
                            SELECT dimension_key,value,window_key,usage_date
                            FROM usage_projection
                            WHERE dimension_key LIKE ANY(CAST(:patterns AS text[]))
                            ORDER BY dimension_key
                            """
                        ),
                        {
                            "patterns": [
                                f"freq:v:{digest_a}:%",
                                f"freq:v:{digest_b}:%",
                                f"freq:m:%:{digest_a}:d",
                                f"freq:m:%:{digest_b}:d",
                            ]
                        },
                    )
                ).mappings()
            }
        assert values[f"freq:v:{digest_a}:m"] == (2, minute_window, usage_date)
        assert values[f"freq:v:{digest_a}:d"] == (2, date_key, usage_date)
        assert values[f"freq:m:{same_app}:{digest_a}:d"] == (2, date_key, usage_date)
        assert values[f"freq:m:{source_only_app}:{digest_a}:d"] == (1, date_key, usage_date)
        assert values[f"freq:m:{window_app}:{digest_a}:d"] == (1, date_key, usage_date)
        assert f"freq:v:{digest_b}:m" not in values
        assert f"freq:v:{digest_b}:d" not in values
        assert f"freq:m:{same_app}:{digest_b}:d" not in values
        assert f"freq:m:{source_only_app}:{digest_b}:d" not in values
        assert f"freq:m:{window_app}:{digest_b}:d" not in values
        assert f"freq:m:{expired_app}:{digest_a}:d" not in values
        assert f"freq:m:{expired_app}:{digest_b}:d" not in values

        redis.fail = True
        service = UsageLedgerService(redis, settings, pooled=False, clock=lambda: now)
        with pytest.raises(UsageProjectionUnavailable):
            await service.rebuild()
        redis.fail = False
        await service.rebuild()
        assert redis.values[f"freq:v:{digest_a}:m"] == "2"
        assert redis.values[f"freq:v:{digest_a}:d"] == "2"
        assert redis.values[f"freq:m:{same_app}:{digest_a}:d"] == "2"
        assert f"freq:v:{digest_b}:m" not in redis.values

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO usage_frequency_subject(id,projection_hmac)
                    VALUES(:id,:hmac)
                    """
                ),
                {"id": subject_b, "hmac": digest_b},
            )
            await connection.execute(
                text(
                    """
                    UPDATE usage_frequency_alias
                    SET subject_id=:source_id
                    WHERE phone_hmac=:digest
                    """
                ),
                {"source_id": subject_b, "digest": digest_b},
            )
            await _insert_frequency_projection(
                connection,
                dimension_key=f"freq:v:{digest_b}:m",
                usage_date=usage_date,
                window_key=minute_window,
                value=1,
                expires_at=now + timedelta(minutes=1),
            )

        try:
            async with engine.begin() as connection:
                await _ensure_frequency_subject(connection, item)
                raise RuntimeError("synthetic-merge-abort")
        except RuntimeError:
            pass

        async with engine.connect() as connection:
            split_subjects = await connection.scalar(
                text(
                    """
                    SELECT count(*) FROM usage_frequency_subject
                    WHERE projection_hmac=ANY(CAST(:digests AS char(64)[]))
                    """
                ),
                {"digests": [digest_a, digest_b]},
            )
            source_minute = await connection.scalar(
                text("SELECT value FROM usage_projection WHERE dimension_key=:key"),
                {"key": f"freq:v:{digest_b}:m"},
            )
            canonical_minute = await connection.scalar(
                text("SELECT value FROM usage_projection WHERE dimension_key=:key"),
                {"key": f"freq:v:{digest_a}:m"},
            )
        assert int(split_subjects or 0) == 2
        assert int(source_minute or 0) == 1
        assert int(canonical_minute or 0) == 2

        concurrent = await asyncio.gather(merge_once(), merge_once())
        assert {result[0] for result in concurrent} == {subject_a}
        async with engine.connect() as connection:
            final_subjects = await connection.scalar(
                text(
                    """
                    SELECT count(*) FROM usage_frequency_subject
                    WHERE projection_hmac=ANY(CAST(:digests AS char(64)[]))
                    """
                ),
                {"digests": [digest_a, digest_b]},
            )
            final_minute = await connection.scalar(
                text("SELECT value FROM usage_projection WHERE dimension_key=:key"),
                {"key": f"freq:v:{digest_a}:m"},
            )
        assert int(final_subjects or 0) == 1
        assert int(final_minute or 0) == 3
    finally:
        await cleanup()
        await engine.dispose()


@pytest.mark.asyncio
async def test_frequency_subject_merge_keeps_newest_live_window() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    settings = cast(Any, SimpleNamespace(database_url=database_url))
    redis = ProjectionRedis()
    now = datetime.now(UTC).replace(second=10, microsecond=0)
    date_key, usage_date, next_day = shanghai_day(now)
    previous_date = usage_date - timedelta(days=1)
    previous_date_key = previous_date.strftime("%Y%m%d")
    current_minute = str(int(now.timestamp() // 60))
    previous_minute = str(int(now.timestamp() // 60) - 1)
    digest_a = _unique_digest("na")
    digest_b = _unique_digest("nb")
    subject_a = uuid4()
    subject_b = uuid4()
    app_ids: list[int] = []
    item = FrequencyDecisionItem(
        phone_hmac=digest_b,
        hmac_aliases={1: digest_a, 2: digest_b},
    )

    async def cleanup() -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    DELETE FROM usage_frequency_entry
                    WHERE subject_id IN (
                      SELECT id FROM usage_frequency_subject
                      WHERE projection_hmac=ANY(CAST(:digests AS char(64)[]))
                    )
                    """
                ),
                {"digests": [digest_a, digest_b]},
            )
            await connection.execute(
                text(
                    """
                    DELETE FROM usage_frequency_subject
                    WHERE projection_hmac=ANY(CAST(:digests AS char(64)[]))
                    """
                ),
                {"digests": [digest_a, digest_b]},
            )
            await connection.execute(
                text(
                    """
                    DELETE FROM usage_projection
                    WHERE dimension_key LIKE ANY(CAST(:patterns AS text[]))
                    """
                ),
                {
                    "patterns": [
                        f"freq:v:{digest_a}:%",
                        f"freq:v:{digest_b}:%",
                        f"freq:m:%:{digest_a}:d",
                        f"freq:m:%:{digest_b}:d",
                    ]
                },
            )
            if app_ids:
                await connection.execute(
                    text("DELETE FROM app WHERE id=ANY(CAST(:ids AS bigint[]))"),
                    {"ids": app_ids},
                )

    try:
        async with engine.begin() as connection:
            same_app = await _create_test_app(connection, f"freq-new-s-{uuid4().hex[:8]}")
            reverse_app = await _create_test_app(connection, f"freq-new-r-{uuid4().hex[:8]}")
            app_ids.extend((same_app, reverse_app))
            await connection.execute(
                text(
                    """
                    INSERT INTO usage_frequency_subject(id,projection_hmac)
                    VALUES(:id_a,:hmac_a),(:id_b,:hmac_b)
                    """
                ),
                {
                    "id_a": subject_a,
                    "hmac_a": digest_a,
                    "id_b": subject_b,
                    "hmac_b": digest_b,
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO usage_frequency_alias(
                      subject_id,key_version,phone_hmac
                    ) VALUES
                      (:id_a,1,:digest_a),
                      (:id_b,2,:digest_b)
                    """
                ),
                {
                    "id_a": subject_a,
                    "id_b": subject_b,
                    "digest_a": digest_a,
                    "digest_b": digest_b,
                },
            )
            await _insert_frequency_projection(
                connection,
                dimension_key=f"freq:v:{digest_a}:m",
                usage_date=usage_date,
                window_key=previous_minute,
                value=3,
                expires_at=now + timedelta(minutes=1),
            )
            await _insert_frequency_projection(
                connection,
                dimension_key=f"freq:v:{digest_b}:m",
                usage_date=usage_date,
                window_key=current_minute,
                value=5,
                expires_at=now + timedelta(minutes=2),
            )
            await _insert_frequency_projection(
                connection,
                dimension_key=f"freq:v:{digest_a}:d",
                usage_date=previous_date,
                window_key=previous_date_key,
                value=2,
                expires_at=next_day,
            )
            await _insert_frequency_projection(
                connection,
                dimension_key=f"freq:v:{digest_b}:d",
                usage_date=usage_date,
                window_key=date_key,
                value=4,
                expires_at=next_day,
            )
            await _insert_frequency_projection(
                connection,
                dimension_key=f"freq:m:{same_app}:{digest_a}:d",
                usage_date=usage_date,
                window_key=date_key,
                value=1,
                expires_at=next_day,
            )
            await _insert_frequency_projection(
                connection,
                dimension_key=f"freq:m:{same_app}:{digest_b}:d",
                usage_date=usage_date,
                window_key=date_key,
                value=6,
                expires_at=next_day,
            )
            await _insert_frequency_projection(
                connection,
                dimension_key=f"freq:m:{reverse_app}:{digest_a}:d",
                usage_date=previous_date,
                window_key=previous_date_key,
                value=8,
                expires_at=next_day,
            )
            await _insert_frequency_projection(
                connection,
                dimension_key=f"freq:m:{reverse_app}:{digest_b}:d",
                usage_date=usage_date,
                window_key=date_key,
                value=7,
                expires_at=next_day,
            )

        async def merge_once() -> tuple[UUID, str, int]:
            async with engine.begin() as connection:
                subject_id, hmac, rows = await _ensure_frequency_subject(connection, item)
                return subject_id, hmac, len(rows)

        first = await merge_once()
        second = await merge_once()
        assert first[0] == second[0] == subject_a
        assert first[1] == second[1] == digest_a
        assert second[2] == 0

        async with engine.connect() as connection:
            values = {
                str(row["dimension_key"]): (
                    int(row["value"]),
                    str(row["window_key"]),
                    row["usage_date"],
                )
                for row in (
                    await connection.execute(
                        text(
                            """
                            SELECT dimension_key,value,window_key,usage_date
                            FROM usage_projection
                            WHERE dimension_key LIKE ANY(CAST(:patterns AS text[]))
                            ORDER BY dimension_key
                            """
                        ),
                        {
                            "patterns": [
                                f"freq:v:{digest_a}:%",
                                f"freq:v:{digest_b}:%",
                                f"freq:m:%:{digest_a}:d",
                                f"freq:m:%:{digest_b}:d",
                            ]
                        },
                    )
                ).mappings()
            }
        assert values[f"freq:v:{digest_a}:m"] == (5, current_minute, usage_date)
        assert values[f"freq:v:{digest_a}:d"] == (4, date_key, usage_date)
        assert values[f"freq:m:{same_app}:{digest_a}:d"] == (7, date_key, usage_date)
        assert values[f"freq:m:{reverse_app}:{digest_a}:d"] == (7, date_key, usage_date)
        assert f"freq:v:{digest_b}:m" not in values
        assert f"freq:v:{digest_b}:d" not in values
        assert f"freq:m:{same_app}:{digest_b}:d" not in values
        assert f"freq:m:{reverse_app}:{digest_b}:d" not in values

        service = UsageLedgerService(redis, settings, pooled=False, clock=lambda: now)
        await service.rebuild()
        assert redis.values[f"freq:v:{digest_a}:m"] == "5"
        assert redis.values[f"freq:v:{digest_a}:d"] == "4"
        assert redis.values[f"freq:m:{same_app}:{digest_a}:d"] == "7"
        assert redis.values[f"freq:m:{reverse_app}:{digest_a}:d"] == "7"
        assert f"freq:v:{digest_b}:m" not in redis.values

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO usage_frequency_subject(id,projection_hmac)
                    VALUES(:id,:hmac)
                    """
                ),
                {"id": subject_b, "hmac": digest_b},
            )
            await connection.execute(
                text(
                    """
                    UPDATE usage_frequency_alias
                    SET subject_id=:source_id
                    WHERE phone_hmac=:digest
                    """
                ),
                {"source_id": subject_b, "digest": digest_b},
            )
            await _insert_frequency_projection(
                connection,
                dimension_key=f"freq:v:{digest_b}:m",
                usage_date=usage_date,
                window_key=current_minute,
                value=2,
                expires_at=now + timedelta(minutes=2),
            )

        concurrent = await asyncio.gather(merge_once(), merge_once())
        assert {result[0] for result in concurrent} == {subject_a}
        async with engine.connect() as connection:
            final_minute = (
                (
                    await connection.execute(
                        text(
                            """
                            SELECT value,window_key FROM usage_projection
                            WHERE dimension_key=:key
                            """
                        ),
                        {"key": f"freq:v:{digest_a}:m"},
                    )
                )
                .mappings()
                .one()
            )
            source_exists = await connection.scalar(
                text("SELECT EXISTS(SELECT 1 FROM usage_projection WHERE dimension_key=:key)"),
                {"key": f"freq:v:{digest_b}:m"},
            )
        assert int(final_minute["value"]) == 7
        assert str(final_minute["window_key"]) == current_minute
        assert not bool(source_exists)
    finally:
        await cleanup()
        await engine.dispose()


@pytest.mark.asyncio
async def test_frequency_subject_merge_rejects_future_window_clock_skew() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    now = datetime.now(UTC).replace(second=10, microsecond=0)
    date_key, usage_date, next_day = shanghai_day(now)
    current_minute = str(int(now.timestamp() // 60))
    future_minute = str(int(now.timestamp() // 60) + FREQUENCY_MERGE_FUTURE_MINUTE_SKEW + 3)
    future_date = usage_date + timedelta(days=FREQUENCY_MERGE_FUTURE_DAY_SKEW + 2)
    future_date_key = future_date.strftime("%Y%m%d")
    digest_a = _unique_digest("sk")
    digest_b = _unique_digest("sl")
    subject_a = uuid4()
    subject_b = uuid4()
    app_id: int | None = None
    item = FrequencyDecisionItem(
        phone_hmac=digest_b,
        hmac_aliases={1: digest_a, 2: digest_b},
    )

    async def cleanup() -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    DELETE FROM usage_frequency_subject
                    WHERE projection_hmac=ANY(CAST(:digests AS char(64)[]))
                    """
                ),
                {"digests": [digest_a, digest_b]},
            )
            await connection.execute(
                text(
                    """
                    DELETE FROM usage_projection
                    WHERE dimension_key LIKE ANY(CAST(:patterns AS text[]))
                    """
                ),
                {
                    "patterns": [
                        f"freq:v:{digest_a}:%",
                        f"freq:v:{digest_b}:%",
                        f"freq:m:%:{digest_a}:d",
                        f"freq:m:%:{digest_b}:d",
                    ]
                },
            )
            if app_id is not None:
                await connection.execute(
                    text("DELETE FROM app WHERE id=:app_id"),
                    {"app_id": app_id},
                )

    try:
        async with engine.begin() as connection:
            app_id = await _create_test_app(connection, f"freq-skew-{uuid4().hex[:8]}")
            await connection.execute(
                text(
                    """
                    INSERT INTO usage_frequency_subject(id,projection_hmac)
                    VALUES(:id_a,:hmac_a),(:id_b,:hmac_b)
                    """
                ),
                {
                    "id_a": subject_a,
                    "hmac_a": digest_a,
                    "id_b": subject_b,
                    "hmac_b": digest_b,
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO usage_frequency_alias(
                      subject_id,key_version,phone_hmac
                    ) VALUES
                      (:id_a,1,:digest_a),
                      (:id_b,2,:digest_b)
                    """
                ),
                {
                    "id_a": subject_a,
                    "id_b": subject_b,
                    "digest_a": digest_a,
                    "digest_b": digest_b,
                },
            )
            await _insert_frequency_projection(
                connection,
                dimension_key=f"freq:v:{digest_a}:m",
                usage_date=usage_date,
                window_key=current_minute,
                value=1,
                expires_at=now + timedelta(minutes=1),
            )
            await _insert_frequency_projection(
                connection,
                dimension_key=f"freq:v:{digest_b}:m",
                usage_date=usage_date,
                window_key=future_minute,
                value=9,
                expires_at=now + timedelta(hours=1),
            )
            await _insert_frequency_projection(
                connection,
                dimension_key=f"freq:v:{digest_a}:d",
                usage_date=usage_date,
                window_key=date_key,
                value=1,
                expires_at=next_day,
            )
            await _insert_frequency_projection(
                connection,
                dimension_key=f"freq:v:{digest_b}:d",
                usage_date=future_date,
                window_key=future_date_key,
                value=9,
                expires_at=now + timedelta(days=3),
            )
            await _insert_frequency_projection(
                connection,
                dimension_key=f"freq:m:{app_id}:{digest_a}:d",
                usage_date=usage_date,
                window_key=date_key,
                value=1,
                expires_at=next_day,
            )
            await _insert_frequency_projection(
                connection,
                dimension_key=f"freq:m:{app_id}:{digest_b}:d",
                usage_date=future_date,
                window_key=future_date_key,
                value=9,
                expires_at=now + timedelta(days=3),
            )

        with pytest.raises(UsageReservationConflict, match="frequency merge window clock skew"):
            async with engine.begin() as connection:
                await _ensure_frequency_subject(connection, item)

        async with engine.connect() as connection:
            subjects = await connection.scalar(
                text(
                    """
                    SELECT count(*) FROM usage_frequency_subject
                    WHERE projection_hmac=ANY(CAST(:digests AS char(64)[]))
                    """
                ),
                {"digests": [digest_a, digest_b]},
            )
            source_minute = await connection.scalar(
                text("SELECT value FROM usage_projection WHERE dimension_key=:key"),
                {"key": f"freq:v:{digest_b}:m"},
            )
            canonical_minute = (
                (
                    await connection.execute(
                        text(
                            """
                            SELECT value,window_key FROM usage_projection
                            WHERE dimension_key=:key
                            """
                        ),
                        {"key": f"freq:v:{digest_a}:m"},
                    )
                )
                .mappings()
                .one()
            )
        assert int(subjects or 0) == 2
        assert int(source_minute or 0) == 9
        assert int(canonical_minute["value"]) == 1
        assert str(canonical_minute["window_key"]) == current_minute
    finally:
        await cleanup()
        await engine.dispose()


async def _insert_reserved_usage(
    connection: Any,
    *,
    reservation_id: UUID,
    app_id: int,
    category: str,
    usage_date: date,
) -> None:
    await connection.execute(
        text(
            """
            INSERT INTO usage_reservation(
              id,request_key,app_id,dept,category,usage_date,state
            ) VALUES(
              :id,:request_key,:app_id,'账本测试部',:category,:usage_date,'reserved'
            )
            """
        ),
        {
            "id": reservation_id,
            "request_key": f"acceptance:{reservation_id}",
            "app_id": app_id,
            "category": category,
            "usage_date": usage_date,
        },
    )


async def _insert_counted_frequency_entry(
    connection: Any,
    *,
    reservation_id: UUID,
    subject_id: UUID,
    app_id: int | None,
    category: str,
    window_kind: str,
    window_key: str,
    usage_date: date,
    projection_key: str,
    expires_at: datetime,
) -> None:
    await connection.execute(
        text(
            """
            INSERT INTO usage_frequency_entry(
              reservation_id,subject_id,app_id,category,
              window_kind,window_key,usage_date,projection_key,
              counted,expires_at
            ) VALUES(
              :reservation_id,:subject_id,:app_id,:category,
              :window_kind,:window_key,:usage_date,:projection_key,
              TRUE,:expires_at
            )
            """
        ),
        {
            "reservation_id": reservation_id,
            "subject_id": subject_id,
            "app_id": app_id,
            "category": category,
            "window_kind": window_kind,
            "window_key": window_key,
            "usage_date": usage_date,
            "projection_key": projection_key,
            "expires_at": expires_at,
        },
    )


async def _assert_active_entries_have_projections(
    connection: Any,
    reservation_ids: Sequence[UUID],
) -> None:
    missing = await connection.scalar(
        text(
            """
            SELECT count(*) FROM usage_frequency_entry e
            JOIN usage_reservation r ON r.id=e.reservation_id
            WHERE e.reservation_id=ANY(CAST(:ids AS uuid[]))
              AND e.counted
              AND r.state=ANY(CAST(:states AS text[]))
              AND NOT EXISTS (
                SELECT 1 FROM usage_projection p
                WHERE p.dimension_key=e.projection_key
              )
            """
        ),
        {
            "ids": list(reservation_ids),
            "states": list(_ACTIVE_RESERVATION_STATES),
        },
    )
    assert int(missing or 0) == 0


async def _assert_source_deletion_follows_entry_refs(
    connection: Any,
    *,
    source_keys: Sequence[str],
    reservation_ids: Sequence[UUID],
) -> None:
    for source_key in source_keys:
        exists = bool(
            await connection.scalar(
                text("SELECT EXISTS(SELECT 1 FROM usage_projection WHERE dimension_key=:key)"),
                {"key": source_key},
            )
        )
        referenced = bool(
            await connection.scalar(
                text(
                    """
                    SELECT EXISTS(
                      SELECT 1 FROM usage_frequency_entry e
                      JOIN usage_reservation r ON r.id=e.reservation_id
                      WHERE e.projection_key=:key
                        AND e.counted
                        AND e.reservation_id=ANY(CAST(:ids AS uuid[]))
                        AND r.state=ANY(CAST(:states AS text[]))
                    )
                    """
                ),
                {
                    "key": source_key,
                    "ids": list(reservation_ids),
                    "states": list(_ACTIVE_RESERVATION_STATES),
                },
            )
        )
        assert referenced is False
        assert exists is False


@pytest.mark.asyncio
async def test_expired_source_projection_merge_completes_terminal_release() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    settings = cast(Any, SimpleNamespace(database_url=database_url))
    redis = ProjectionRedis()
    now = datetime.now(UTC).replace(second=10, microsecond=0)
    _date_key, usage_date, _next_day = shanghai_day(now)
    previous_date = usage_date - timedelta(days=1)
    previous_date_key = previous_date.strftime("%Y%m%d")
    expired_minute = str(int((now - timedelta(minutes=2)).timestamp() // 60))
    expired_at = now - timedelta(minutes=1)
    digest_a = _unique_digest("ea")
    digest_b = _unique_digest("eb")
    subject_a = uuid4()
    subject_b = uuid4()
    verify_minute_id = uuid4()
    verify_day_id = uuid4()
    market_id = uuid4()
    live_id = uuid4()
    reservation_ids = [verify_minute_id, verify_day_id, market_id, live_id]
    batch_ids: list[int] = []
    app_id: int | None = None
    item = FrequencyDecisionItem(
        phone_hmac=digest_b,
        hmac_aliases={1: digest_a, 2: digest_b},
    )
    source_keys: list[str] = []

    async def cleanup() -> None:
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
            if batch_ids:
                await connection.execute(
                    text("DELETE FROM sms_batch WHERE id=ANY(CAST(:ids AS bigint[]))"),
                    {"ids": batch_ids},
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
                text(
                    """
                    DELETE FROM usage_frequency_subject
                    WHERE projection_hmac=ANY(CAST(:digests AS char(64)[]))
                    """
                ),
                {"digests": [digest_a, digest_b]},
            )
            await connection.execute(
                text(
                    """
                    DELETE FROM usage_projection
                    WHERE dimension_key LIKE ANY(CAST(:patterns AS text[]))
                    """
                ),
                {
                    "patterns": [
                        f"freq:v:{digest_a}:%",
                        f"freq:v:{digest_b}:%",
                        f"freq:m:%:{digest_a}:d",
                        f"freq:m:%:{digest_b}:d",
                    ]
                },
            )
            if app_id is not None:
                await connection.execute(
                    text("DELETE FROM app WHERE id=:app_id"),
                    {"app_id": app_id},
                )

    try:
        async with engine.begin() as connection:
            app_id = await _create_test_app(connection, f"freq-exp-{uuid4().hex[:8]}")
            source_keys.extend(
                (
                    f"freq:v:{digest_b}:m",
                    f"freq:v:{digest_b}:d",
                    f"freq:m:{app_id}:{digest_b}:d",
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO usage_frequency_subject(id,projection_hmac)
                    VALUES(:id_a,:hmac_a),(:id_b,:hmac_b)
                    """
                ),
                {
                    "id_a": subject_a,
                    "hmac_a": digest_a,
                    "id_b": subject_b,
                    "hmac_b": digest_b,
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO usage_frequency_alias(
                      subject_id,key_version,phone_hmac
                    ) VALUES
                      (:id_a,1,:digest_a),
                      (:id_b,2,:digest_b)
                    """
                ),
                {
                    "id_a": subject_a,
                    "id_b": subject_b,
                    "digest_a": digest_a,
                    "digest_b": digest_b,
                },
            )
            await _insert_frequency_projection(
                connection,
                dimension_key=f"freq:v:{digest_b}:m",
                usage_date=previous_date,
                window_key=expired_minute,
                value=1,
                expires_at=expired_at,
            )
            await _insert_frequency_projection(
                connection,
                dimension_key=f"freq:v:{digest_b}:d",
                usage_date=previous_date,
                window_key=previous_date_key,
                value=1,
                expires_at=expired_at,
            )
            await _insert_frequency_projection(
                connection,
                dimension_key=f"freq:m:{app_id}:{digest_b}:d",
                usage_date=previous_date,
                window_key=previous_date_key,
                value=1,
                expires_at=expired_at,
            )
            for reservation_id, category in (
                (verify_minute_id, "verify"),
                (verify_day_id, "verify"),
                (market_id, "market"),
            ):
                await _insert_reserved_usage(
                    connection,
                    reservation_id=reservation_id,
                    app_id=app_id,
                    category=category,
                    usage_date=usage_date,
                )
            await _insert_counted_frequency_entry(
                connection,
                reservation_id=verify_minute_id,
                subject_id=subject_b,
                app_id=None,
                category="verify",
                window_kind="minute",
                window_key=expired_minute,
                usage_date=previous_date,
                projection_key=f"freq:v:{digest_b}:m",
                expires_at=expired_at,
            )
            await _insert_counted_frequency_entry(
                connection,
                reservation_id=verify_day_id,
                subject_id=subject_b,
                app_id=None,
                category="verify",
                window_kind="day",
                window_key=previous_date_key,
                usage_date=previous_date,
                projection_key=f"freq:v:{digest_b}:d",
                expires_at=expired_at,
            )
            await _insert_counted_frequency_entry(
                connection,
                reservation_id=market_id,
                subject_id=subject_b,
                app_id=app_id,
                category="market",
                window_kind="day",
                window_key=previous_date_key,
                usage_date=previous_date,
                projection_key=f"freq:m:{app_id}:{digest_b}:d",
                expires_at=expired_at,
            )

        async with engine.begin() as connection:
            subject_id, hmac, _rows = await _ensure_frequency_subject(connection, item)
            assert subject_id == subject_a
            assert hmac == digest_a
            await _assert_active_entries_have_projections(
                connection,
                [verify_minute_id, verify_day_id, market_id],
            )
            await _assert_source_deletion_follows_entry_refs(
                connection,
                source_keys=source_keys,
                reservation_ids=[verify_minute_id, verify_day_id, market_id],
            )
            projections = {
                str(row["dimension_key"]): (
                    int(row["value"]),
                    str(row["window_key"]),
                    row["expires_at"] <= now,
                )
                for row in (
                    await connection.execute(
                        text(
                            """
                            SELECT dimension_key,value,window_key,expires_at
                            FROM usage_projection
                            WHERE dimension_key LIKE ANY(CAST(:patterns AS text[]))
                            """
                        ),
                        {
                            "patterns": [
                                f"freq:v:{digest_a}:%",
                                f"freq:v:{digest_b}:%",
                                f"freq:m:%:{digest_a}:d",
                                f"freq:m:%:{digest_b}:d",
                            ]
                        },
                    )
                ).mappings()
            }
            entry_keys = {
                str(row["reservation_id"]): str(row["projection_key"])
                for row in (
                    await connection.execute(
                        text(
                            """
                            SELECT reservation_id,projection_key
                            FROM usage_frequency_entry
                            WHERE reservation_id=ANY(CAST(:ids AS uuid[]))
                            """
                        ),
                        {"ids": [verify_minute_id, verify_day_id, market_id]},
                    )
                ).mappings()
            }
        assert projections[f"freq:v:{digest_a}:m"] == (1, expired_minute, True)
        assert projections[f"freq:v:{digest_a}:d"] == (1, previous_date_key, True)
        assert projections[f"freq:m:{app_id}:{digest_a}:d"] == (1, previous_date_key, True)
        assert f"freq:v:{digest_b}:m" not in projections
        assert f"freq:v:{digest_b}:d" not in projections
        assert f"freq:m:{app_id}:{digest_b}:d" not in projections
        assert entry_keys[str(verify_minute_id)] == f"freq:v:{digest_a}:m"
        assert entry_keys[str(verify_day_id)] == f"freq:v:{digest_a}:d"
        assert entry_keys[str(market_id)] == f"freq:m:{app_id}:{digest_a}:d"

        service = UsageLedgerService(redis, settings, pooled=False, clock=lambda: now)
        redis.fail = True
        with pytest.raises(UsageProjectionUnavailable):
            await service.rebuild()
        async with engine.connect() as connection:
            tombstone = await connection.scalar(
                text("SELECT value FROM usage_projection WHERE dimension_key=:key"),
                {"key": f"freq:v:{digest_a}:m"},
            )
        assert int(tombstone or 0) == 1
        redis.fail = False
        await service.rebuild()
        assert f"freq:v:{digest_a}:m" not in redis.values
        assert f"freq:v:{digest_b}:m" not in redis.values

        live_reservation = await service.start_reservation(
            request_key=f"acceptance:{live_id}",
            app_id=app_id,
            dept="账本测试部",
            category="verify",
            now=now,
        )
        assert live_reservation.reservation_id != live_id
        reservation_ids.append(live_reservation.reservation_id)
        assert await service.allow_frequency(
            live_reservation.reservation_id,
            "verify",
            app_id=app_id,
            phone_hmac=digest_b,
            hmac_aliases={1: digest_a, 2: digest_b},
            limits=FrequencyLimits(2, 10, 1),
            now=now,
        )
        current_minute = str(int(now.timestamp() // 60))
        async with engine.connect() as connection:
            live_minute = (
                (
                    await connection.execute(
                        text(
                            """
                            SELECT value,window_key FROM usage_projection
                            WHERE dimension_key=:key
                            """
                        ),
                        {"key": f"freq:v:{digest_a}:m"},
                    )
                )
                .mappings()
                .one()
            )
        assert int(live_minute["value"]) == 1
        assert str(live_minute["window_key"]) == current_minute

        reject_id = int(uuid4().int % 1_000_000_000) + 1
        expire_id = reject_id + 1
        day_batch_id, _day_batch_no = await _create_batch(
            engine,
            app_id=app_id,
            reservation_id=verify_day_id,
            category="verify",
            quota_cost=0,
        )
        batch_ids.append(day_batch_id)
        market_batch_id, market_batch_no = await _create_batch(
            engine,
            app_id=app_id,
            reservation_id=market_id,
            category="market",
            quota_cost=0,
        )
        batch_ids.append(market_batch_id)

        assert await service.request_release(
            verify_minute_id,
            event_id=f"approval:{reject_id}:rejected",
        )
        async with engine.begin() as connection:
            assert await request_usage_release_for_batch(
                connection,
                batch_id=day_batch_id,
                event_id=f"approval:{expire_id}:expired",
            )
            assert await request_usage_release_for_batch(
                connection,
                batch_id=market_batch_id,
                event_id=f"batch:{market_batch_no}:cancelled",
            )
        assert not await service.request_release(
            verify_minute_id,
            event_id=f"approval:{reject_id}:rejected",
        )
        async with engine.begin() as connection:
            assert await request_usage_release_for_batch(
                connection,
                batch_id=day_batch_id,
                event_id=f"approval:{expire_id}:expired",
            )
            assert await request_usage_release_for_batch(
                connection,
                batch_id=market_batch_id,
                event_id=f"batch:{market_batch_no}:cancelled",
            )

        async with engine.connect() as connection:
            states = {
                str(row["id"]): str(row["state"])
                for row in (
                    await connection.execute(
                        text(
                            """
                            SELECT id,state FROM usage_reservation
                            WHERE id=ANY(CAST(:ids AS uuid[]))
                            """
                        ),
                        {"ids": [verify_minute_id, verify_day_id, market_id]},
                    )
                ).mappings()
            }
            live_after_release = (
                (
                    await connection.execute(
                        text(
                            """
                            SELECT value,window_key FROM usage_projection
                            WHERE dimension_key=:key
                            """
                        ),
                        {"key": f"freq:v:{digest_a}:m"},
                    )
                )
                .mappings()
                .one()
            )
            market_after_release = await connection.scalar(
                text("SELECT value FROM usage_projection WHERE dimension_key=:key"),
                {"key": f"freq:m:{app_id}:{digest_a}:d"},
            )
        assert set(states.values()) == {"release_requested"}
        assert int(live_after_release["value"]) == 1
        assert str(live_after_release["window_key"]) == current_minute
        assert int(market_after_release or 0) == 0

        redis.fail = True
        with pytest.raises(UsageProjectionUnavailable):
            await service.apply_release(verify_minute_id)
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT state FROM usage_reservation WHERE id=:id"),
                    {"id": verify_minute_id},
                )
                == "release_requested"
            )
            assert int(
                await connection.scalar(
                    text("SELECT value FROM usage_projection WHERE dimension_key=:key"),
                    {"key": f"freq:v:{digest_a}:m"},
                )
                or 0
            ) == 1
        redis.fail = False
        assert await service.apply_release(verify_minute_id) == 1
        assert await service.apply_release(verify_day_id) == 1
        assert await service.apply_release(market_id) == 1
        assert await service.apply_release(verify_minute_id) == 0
        assert redis.values[f"freq:v:{digest_a}:m"] == "1"
    finally:
        await cleanup()
        await engine.dispose()


@pytest.mark.asyncio
async def test_expired_source_merge_and_release_are_safe_under_concurrency() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    settings = cast(Any, SimpleNamespace(database_url=database_url))
    redis = ProjectionRedis()
    now = datetime.now(UTC).replace(second=10, microsecond=0)
    _date_key, usage_date, _next_day = shanghai_day(now)
    expired_minute = str(int((now - timedelta(minutes=2)).timestamp() // 60))
    expired_at = now - timedelta(minutes=1)
    digest_a = _unique_digest("ca")
    digest_b = _unique_digest("cb")
    subject_a = uuid4()
    subject_b = uuid4()
    reservation_id = uuid4()
    reservation_ids = [reservation_id]
    app_id: int | None = None
    item = FrequencyDecisionItem(
        phone_hmac=digest_b,
        hmac_aliases={1: digest_a, 2: digest_b},
    )
    event_id = f"usage:{reservation_id}:orphan-recovery"

    async def cleanup() -> None:
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
                text(
                    """
                    DELETE FROM usage_frequency_subject
                    WHERE projection_hmac=ANY(CAST(:digests AS char(64)[]))
                    """
                ),
                {"digests": [digest_a, digest_b]},
            )
            await connection.execute(
                text(
                    """
                    DELETE FROM usage_projection
                    WHERE dimension_key LIKE ANY(CAST(:patterns AS text[]))
                    """
                ),
                {
                    "patterns": [
                        f"freq:v:{digest_a}:%",
                        f"freq:v:{digest_b}:%",
                    ]
                },
            )
            if app_id is not None:
                await connection.execute(
                    text("DELETE FROM app WHERE id=:app_id"),
                    {"app_id": app_id},
                )

    try:
        async with engine.begin() as connection:
            app_id = await _create_test_app(connection, f"freq-con-{uuid4().hex[:8]}")
            await connection.execute(
                text(
                    """
                    INSERT INTO usage_frequency_subject(id,projection_hmac)
                    VALUES(:id_a,:hmac_a),(:id_b,:hmac_b)
                    """
                ),
                {
                    "id_a": subject_a,
                    "hmac_a": digest_a,
                    "id_b": subject_b,
                    "hmac_b": digest_b,
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO usage_frequency_alias(
                      subject_id,key_version,phone_hmac
                    ) VALUES
                      (:id_a,1,:digest_a),
                      (:id_b,2,:digest_b)
                    """
                ),
                {
                    "id_a": subject_a,
                    "id_b": subject_b,
                    "digest_a": digest_a,
                    "digest_b": digest_b,
                },
            )
            await _insert_frequency_projection(
                connection,
                dimension_key=f"freq:v:{digest_b}:m",
                usage_date=usage_date,
                window_key=expired_minute,
                value=1,
                expires_at=expired_at,
            )
            await _insert_reserved_usage(
                connection,
                reservation_id=reservation_id,
                app_id=app_id,
                category="verify",
                usage_date=usage_date,
            )
            await _insert_counted_frequency_entry(
                connection,
                reservation_id=reservation_id,
                subject_id=subject_b,
                app_id=None,
                category="verify",
                window_kind="minute",
                window_key=expired_minute,
                usage_date=usage_date,
                projection_key=f"freq:v:{digest_b}:m",
                expires_at=expired_at,
            )

        service = UsageLedgerService(redis, settings, pooled=False, clock=lambda: now)

        async def merge_once() -> tuple[UUID, str]:
            async with engine.begin() as connection:
                subject_id, hmac, _rows = await _ensure_frequency_subject(connection, item)
                return subject_id, hmac

        results = await asyncio.wait_for(
            asyncio.gather(
                merge_once(),
                service.request_release(reservation_id, event_id=event_id),
                merge_once(),
                service.request_release(reservation_id, event_id=event_id),
                return_exceptions=True,
            ),
            timeout=15,
        )
        errors = [result for result in results if isinstance(result, BaseException)]
        assert not errors, errors
        merges = [result for result in results if isinstance(result, tuple)]
        releases = [result for result in results if isinstance(result, bool)]
        assert {result[0] for result in merges} == {subject_a}
        assert releases.count(True) == 1
        assert releases.count(False) == 1

        async with engine.connect() as connection:
            state = await connection.scalar(
                text("SELECT state FROM usage_reservation WHERE id=:id"),
                {"id": reservation_id},
            )
            await _assert_active_entries_have_projections(connection, [reservation_id])
            await _assert_source_deletion_follows_entry_refs(
                connection,
                source_keys=[f"freq:v:{digest_b}:m"],
                reservation_ids=[reservation_id],
            )
            source_exists = await connection.scalar(
                text("SELECT EXISTS(SELECT 1 FROM usage_projection WHERE dimension_key=:key)"),
                {"key": f"freq:v:{digest_b}:m"},
            )
            canonical_value = await connection.scalar(
                text("SELECT value FROM usage_projection WHERE dimension_key=:key"),
                {"key": f"freq:v:{digest_a}:m"},
            )
        assert state == "release_requested"
        assert not bool(source_exists)
        assert int(canonical_value or 0) == 0
        assert await service.apply_release(reservation_id) == 1
        assert await service.apply_release(reservation_id) == 0
    finally:
        await cleanup()
        await engine.dispose()
