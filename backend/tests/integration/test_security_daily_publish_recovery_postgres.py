"""真实 FileControl 与 PostgreSQL：重试发布和崩溃恢复复用请求身份。"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.services.security_daily import (
    FileSecurityDailyControl,
    SecurityDailyControlResult,
    SecurityDailyService,
    recipient_set_digest,
    validate_security_daily_payload,
)
from tests.integration.test_config_authorization_postgres import (  # noqa: F401
    config_context,
    flow_owner,
)
from tests.integration.test_daily_authorization_postgres import daily_context  # noqa: F401

pytestmark = pytest.mark.skipif(
    "EXPORT_AUTH_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)

ROOT = Path(__file__).resolve().parents[3]
SAMPLE = ROOT / "deploy" / "templates" / "security_daily_report.sample.json"
SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
RECIPIENT = "security-owner@example.com"
SYNTHETIC_KEY = "re_test_value"


def _payload(day: date) -> dict[str, object]:
    value = json.loads(SAMPLE.read_text(encoding="utf-8"))
    value["report_date"] = day.isoformat()
    value["period_start"] = f"{day.isoformat()}T00:00:00+08:00"
    value["period_end"] = f"{day.isoformat()}T23:59:59+08:00"
    generated = day + timedelta(days=1)
    value["generated_at"] = f"{generated.isoformat()}T08:00:00+08:00"
    return validate_security_daily_payload(value)


async def _insert_report(owner, day: date, source: str) -> int:
    payload = _payload(day)
    async with owner.begin() as connection:
        report_id = await connection.scalar(
            text(
                """
                INSERT INTO security_daily_report(
                  report_date,period_start,period_end,status,generation_status,
                  generation_source,delivery_status,payload,generated_at,recipient_count
                ) VALUES (
                  :report_date,:period_start,:period_end,:status,'ready',:source,
                  'not_sent',CAST(:payload AS jsonb),:generated_at,1
                ) RETURNING id
                """
            ),
            {
                "report_date": day,
                "period_start": datetime.fromisoformat(str(payload["period_start"])),
                "period_end": datetime.fromisoformat(str(payload["period_end"])),
                "status": payload["status"],
                "source": source,
                "payload": json.dumps(payload, ensure_ascii=False),
                "generated_at": datetime.fromisoformat(str(payload["generated_at"])),
            },
        )
    return int(report_id)


async def _counts(owner, report_id: int) -> tuple[int, int, str]:
    async with owner.connect() as connection:
        requests = int(
            await connection.scalar(
                text("SELECT count(*) FROM security_daily_delivery_request WHERE report_id=:id"),
                {"id": report_id},
            )
        )
        row = (
            await connection.execute(
                text("SELECT retry_count,delivery_status FROM security_daily_report WHERE id=:id"),
                {"id": report_id},
            )
        ).one()
    return requests, int(row.retry_count), str(row.delivery_status)


async def _backdate(owner, request_id: UUID) -> None:
    async with owner.begin() as connection:
        await connection.execute(
            text(
                "UPDATE security_daily_delivery_request "
                "SET requested_at=now() - interval '10 minutes' "
                "WHERE request_id=:request_id"
            ),
            {"request_id": request_id},
        )


@pytest.mark.asyncio
async def test_file_control_postgres_retries_and_recovers_same_request(
    daily_context,  # noqa: F811
    tmp_path: Path,
) -> None:
    owner, principal, repo = daily_context
    digest = recipient_set_digest((RECIPIENT,))
    prior_system_key = None
    key_replaced = False
    report_ids: list[int] = []
    try:
        async with owner.begin() as connection:
            prior_system_key = await connection.scalar(
                text(
                    "SELECT key_material FROM audit_context_signing_key WHERE key_kind='system:api'"
                )
            )
            await connection.execute(
                text(
                    """
                    UPDATE sys_config SET value=CASE key
                      WHEN 'security_daily_enabled' THEN 'true'
                      WHEN 'security_daily_resend_configured' THEN 'true'
                      WHEN 'security_daily_recipient_count' THEN '1'
                      WHEN 'security_daily_resend_api_key' THEN :api_key
                      WHEN 'security_daily_recipient_set_digest' THEN :digest
                      ELSE value
                    END
                    WHERE key IN (
                      'security_daily_enabled','security_daily_resend_configured',
                      'security_daily_recipient_count','security_daily_resend_api_key',
                      'security_daily_recipient_set_digest'
                    )
                    """
                ),
                {"api_key": SYNTHETIC_KEY, "digest": digest},
            )
            await connection.execute(text("DELETE FROM security_daily_recipient"))
            await connection.execute(
                text("INSERT INTO security_daily_recipient(position,address) VALUES(1,:address)"),
                {"address": RECIPIENT},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO audit_context_signing_key(key_kind,key_material)
                    SELECT 'system:api', key_material
                    FROM audit_context_signing_key WHERE key_kind='principal'
                    ON CONFLICT (key_kind) DO UPDATE
                    SET key_material=EXCLUDED.key_material
                    """
                )
            )
        key_replaced = True
        control_dir = tmp_path / "control"
        control_dir.mkdir(mode=0o700)
        control = FileSecurityDailyControl(control_dir, tmp_path / "config")
        service = SecurityDailyService(repo, control, control_dir=control_dir)
        manual_id = await _insert_report(owner, date(2026, 7, 15), "manual")
        report_ids.append(manual_id)
        sent = await service.request_delivery(
            manual_id, "send", principal=principal, ip="127.0.0.1"
        )
        request_count, retry_count, status = await _counts(owner, manual_id)
        published = json.loads(
            (control_dir / "requests" / f"{sent.request_id}.json").read_text(encoding="utf-8")
        )
        assert request_count == 1
        assert retry_count == 0
        assert status == "pending"
        assert published["request_id"] == str(sent.request_id)
        assert published["config_version"] == sent.config_version
        assert published["delivery_generation"] == sent.delivery_generation
        assert published["recipient_set_digest"] == sent.recipient_set_digest
        assert SYNTHETIC_KEY not in json.dumps(published)
        assert RECIPIENT not in json.dumps(published)

        (control_dir / "results").mkdir(exist_ok=True)
        (control_dir / "results" / f"{sent.request_id}.json").write_text(
            json.dumps(
                {
                    "request_id": str(sent.request_id),
                    "report_date": "2026-07-15",
                    "state": "failed",
                    "completed_at": "2026-07-16T08:02:00+08:00",
                    "error": "受控投递失败",
                }
            ),
            encoding="utf-8",
        )
        await repo.apply_control_result(
            SecurityDailyControlResult(
                request_id=sent.request_id,
                report_date=date(2026, 7, 15),
                state="failed",
                completed_at=datetime(2026, 7, 16, 8, 2, tzinfo=SHANGHAI),
                error="受控投递失败",
            )
        )
        _, retry_count, status = await _counts(owner, manual_id)
        assert status == "failed"
        assert retry_count == 0

        retried = await service.request_delivery(
            manual_id, "retry", principal=principal, ip="127.0.0.1"
        )
        request_count, retry_count, status = await _counts(owner, manual_id)
        retried_body = json.loads(
            (control_dir / "requests" / f"{retried.request_id}.json").read_text(encoding="utf-8")
        )
        assert retried.request_id != sent.request_id
        assert request_count == 2
        assert retry_count == 1
        assert status == "pending"
        assert retried_body["request_id"] == str(retried.request_id)
        assert retried_body["delivery_generation"] == retried.delivery_generation
        assert retried_body["recipient_set_digest"] == retried.recipient_set_digest
        old_result = json.loads(
            (control_dir / "results" / f"{sent.request_id}.json").read_text(encoding="utf-8")
        )
        assert old_result["request_id"] == str(sent.request_id)
        assert old_result["state"] == "failed"

        (control_dir / "results" / f"{retried.request_id}.json").write_text(
            json.dumps(
                {
                    "request_id": str(retried.request_id),
                    "report_date": "2026-07-15",
                    "state": "sent",
                    "completed_at": "2026-07-16T08:06:00+08:00",
                }
            ),
            encoding="utf-8",
        )
        await repo.apply_control_result(
            SecurityDailyControlResult(
                request_id=retried.request_id,
                report_date=date(2026, 7, 15),
                state="sent",
                completed_at=datetime(2026, 7, 16, 8, 6, tzinfo=SHANGHAI),
            )
        )
        _, retry_count, status = await _counts(owner, manual_id)
        assert status == "sent"
        assert retry_count == 1
        async with owner.connect() as connection:
            audit = (
                await connection.execute(
                    text(
                        """
                        SELECT after_val::text FROM audit_log
                        WHERE action='security_daily_delivery_result'
                          AND object_id='2026-07-15'
                        ORDER BY id DESC LIMIT 1
                        """
                    )
                )
            ).scalar_one()
        assert '"state": "sent"' in audit or '"state":"sent"' in audit
        assert SYNTHETIC_KEY not in audit
        assert RECIPIENT not in audit
        assert "1" + "3800138000" not in audit

        for source, day in (("manual", date(2026, 7, 14)), ("auto", date(2026, 7, 13))):
            report_id = await _insert_report(owner, day, source)
            report_ids.append(report_id)
            record = await repo.get_report(report_id)
            assert record is not None
            pending = await repo.request_delivery(
                record,
                "send",
                principal=principal,
                ip="127.0.0.1",
                control_evidence="missing",
                recipient_set_digest=digest,
                expected_config_version=1,
            )
            await _backdate(owner, pending.request_id)
            assert not (control_dir / "requests" / f"{pending.request_id}.json").exists()
            recovered = await service.recover_pending_delivery(pending.request_id)
            assert recovered is not None
            assert recovered.request_id == pending.request_id
            restored = json.loads(
                (control_dir / "requests" / f"{pending.request_id}.json").read_text(
                    encoding="utf-8"
                )
            )
            assert restored["request_id"] == str(pending.request_id)
            assert restored["config_version"] == pending.config_version
            assert restored["delivery_generation"] == pending.delivery_generation
            assert restored["recipient_set_digest"] == digest
            request_count, _, _ = await _counts(owner, report_id)
            assert request_count == 1

        claimed_id = await _insert_report(owner, date(2026, 7, 12), "manual")
        report_ids.append(claimed_id)
        claimed_record = await repo.get_report(claimed_id)
        assert claimed_record is not None
        claimed = await repo.request_delivery(
            claimed_record,
            "send",
            principal=principal,
            ip="127.0.0.1",
            control_evidence="missing",
            recipient_set_digest=digest,
            expected_config_version=1,
        )
        await _backdate(owner, claimed.request_id)
        (control_dir / "requests").mkdir(exist_ok=True)
        (control_dir / "requests" / f".{claimed.request_id}.1.processing").write_text(
            "{}", encoding="utf-8"
        )
        assert await service.recover_pending_delivery(claimed.request_id) is None
        assert not (control_dir / "requests" / f"{claimed.request_id}.json").exists()
        assert (await _counts(owner, claimed_id))[0] == 1

        unknown_id = await _insert_report(owner, date(2026, 7, 11), "auto")
        report_ids.append(unknown_id)
        unknown_request = uuid4()
        async with owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO security_daily_delivery_request(
                      request_id,report_id,report_date,action,state,dedup_key,
                      requested_by,config_version,delivery_generation,recipient_set_digest,
                      requested_at,completed_at,error
                    ) VALUES (
                      :request_id,:report_id,:report_date,'send','unknown',:dedup_key,
                      'admin',1,1,:digest,now() - interval '10 minutes',now(),'投递结果未知'
                    )
                    """
                ),
                {
                    "request_id": unknown_request,
                    "report_id": unknown_id,
                    "report_date": date(2026, 7, 11),
                    "dedup_key": f"security-daily:unknown:{unknown_request}",
                    "digest": digest,
                },
            )
            await connection.execute(
                text("UPDATE security_daily_report SET delivery_status='unknown' WHERE id=:id"),
                {"id": unknown_id},
            )
        assert await service.recover_pending_delivery(unknown_request) is None
        assert not (control_dir / "requests" / f"{unknown_request}.json").exists()
        assert (await _counts(owner, unknown_id))[0] == 1

        fenced_id = await _insert_report(owner, date(2026, 7, 10), "manual")
        report_ids.append(fenced_id)
        fenced_record = await repo.get_report(fenced_id)
        assert fenced_record is not None
        fenced = await repo.request_delivery(
            fenced_record,
            "send",
            principal=principal,
            ip="127.0.0.1",
            control_evidence="missing",
            recipient_set_digest=digest,
            expected_config_version=1,
        )
        await _backdate(owner, fenced.request_id)
        async with owner.begin() as connection:
            await connection.execute(
                text("UPDATE sys_config SET value='2' WHERE key='security_daily_config_version'")
            )
        assert await service.recover_pending_delivery(fenced.request_id) is None
        assert not (control_dir / "requests" / f"{fenced.request_id}.json").exists()
        assert (await _counts(owner, fenced_id))[0] == 1
        async with owner.begin() as connection:
            await connection.execute(
                text("UPDATE sys_config SET value='1' WHERE key='security_daily_config_version'")
            )
            await connection.execute(
                text(
                    "UPDATE sys_config SET value=:digest "
                    "WHERE key='security_daily_recipient_set_digest'"
                ),
                {"digest": "b" * 64},
            )
        recipient_id = await _insert_report(owner, date(2026, 7, 9), "auto")
        report_ids.append(recipient_id)
        recipient_record = await repo.get_report(recipient_id)
        assert recipient_record is not None
        # 请求仍写着变更前的摘要；当前摘要已不同，恢复不能越过收件人围栏。
        async with owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO security_daily_delivery_request(
                      request_id,report_id,report_date,action,state,dedup_key,
                      requested_by,config_version,delivery_generation,recipient_set_digest,
                      requested_at
                    ) VALUES (
                      :request_id,:report_id,:report_date,'send','pending',:dedup_key,
                      'admin',1,1,:digest,now() - interval '10 minutes'
                    )
                    """
                ),
                {
                    "request_id": uuid4(),
                    "report_id": recipient_id,
                    "report_date": date(2026, 7, 9),
                    "dedup_key": f"security-daily:recipient:{recipient_id}",
                    "digest": digest,
                },
            )
            stale_request = await connection.scalar(
                text("SELECT request_id FROM security_daily_delivery_request WHERE report_id=:id"),
                {"id": recipient_id},
            )
        assert await service.recover_pending_delivery(UUID(str(stale_request))) is None
        assert not (control_dir / "requests" / f"{stale_request}.json").exists()
        assert (await _counts(owner, recipient_id))[0] == 1
    finally:
        async with owner.begin() as connection:
            if report_ids:
                await connection.execute(
                    text("DELETE FROM security_daily_delivery_request WHERE report_id = ANY(:ids)"),
                    {"ids": report_ids},
                )
                await connection.execute(
                    text("DELETE FROM security_daily_report WHERE id = ANY(:ids)"),
                    {"ids": report_ids},
                )
                await connection.execute(
                    text(
                        """
                        DELETE FROM audit_log
                        WHERE object_type='security_daily_report'
                          AND object_id = ANY(:object_ids)
                        """
                    ),
                    {
                        "object_ids": [
                            "2026-07-15",
                            "2026-07-14",
                            "2026-07-13",
                            "2026-07-12",
                            "2026-07-11",
                            "2026-07-10",
                            "2026-07-09",
                        ]
                    },
                )
            if key_replaced and prior_system_key is None:
                await connection.execute(
                    text("DELETE FROM audit_context_signing_key WHERE key_kind='system:api'")
                )
            elif key_replaced:
                await connection.execute(
                    text(
                        "UPDATE audit_context_signing_key SET key_material=:key "
                        "WHERE key_kind='system:api'"
                    ),
                    {"key": prior_system_key},
                )
