"""完成幂等结果的到期证明及重新建立保护状态的批次锁。"""
from __future__ import annotations

from alembic import op

revision = "0115_idempotency_result_lifecycle"
down_revision = "0114_send_app_policy_privileges"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 不扫描或猜测历史孤儿；有可信记录时由受锁保护的清理逐页保留证明。
    op.execute("ALTER TABLE idempotency_claim ADD COLUMN IF NOT EXISTS "
               "result_expires_at TIMESTAMPTZ")
    op.execute("""CREATE OR REPLACE FUNCTION lock_idempotency_result_batch()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
  IF TG_TABLE_NAME='sms_chunk' THEN
    IF OLD.status NOT IN ('submitted','failed') OR NEW.status IN ('submitted','failed') THEN
      RETURN NEW;
    END IF;
  ELSIF TG_TABLE_NAME='callback_task' THEN
    IF NEW.status IN ('done','dead') THEN RETURN NEW; END IF;
    IF TG_OP='UPDATE' THEN
      IF OLD.status NOT IN ('done','dead') AND OLD.batch_id IS NOT DISTINCT FROM NEW.batch_id THEN
        RETURN NEW;
      END IF;
    END IF;
  END IF;
  PERFORM id FROM public.sms_batch WHERE id=NEW.batch_id FOR UPDATE;
  RETURN NEW;
END
$$;""")
    op.execute("""CREATE OR REPLACE FUNCTION lock_callback_idempotency_batch(p_task_id BIGINT)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
  PERFORM b.id FROM public.sms_batch b
    JOIN public.callback_task t ON t.batch_id=b.id
    WHERE t.id=p_task_id FOR UPDATE OF b;
END
$$;""")
    op.execute("REVOKE ALL ON FUNCTION lock_callback_idempotency_batch(BIGINT) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION lock_callback_idempotency_batch(BIGINT) TO sms_callback")
    op.execute("REVOKE ALL ON FUNCTION lock_idempotency_result_batch() FROM PUBLIC")
    op.execute("DROP TRIGGER IF EXISTS trg_chunk_idempotency_protection ON sms_chunk")
    op.execute("""CREATE TRIGGER trg_chunk_idempotency_protection
BEFORE UPDATE ON sms_chunk FOR EACH ROW
EXECUTE FUNCTION lock_idempotency_result_batch();""")
    op.execute("DROP TRIGGER IF EXISTS trg_callback_idempotency_protection ON callback_task")
    op.execute("""CREATE TRIGGER trg_callback_idempotency_protection
BEFORE INSERT OR UPDATE ON callback_task FOR EACH ROW
EXECUTE FUNCTION lock_idempotency_result_batch();""")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_callback_idempotency_protection ON callback_task")
    op.execute("DROP TRIGGER IF EXISTS trg_chunk_idempotency_protection ON sms_chunk")
    op.execute("DROP FUNCTION IF EXISTS lock_idempotency_result_batch()")
    op.execute("DROP FUNCTION IF EXISTS lock_callback_idempotency_batch(BIGINT)")
    op.execute("ALTER TABLE idempotency_claim DROP COLUMN IF EXISTS result_expires_at")
