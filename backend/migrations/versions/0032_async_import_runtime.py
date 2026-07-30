"""导入源密文暂存、异步解析租约与恢复索引。"""

from __future__ import annotations

from alembic import op

revision = "0032_async_import_runtime"
down_revision = "0031_import_reservation_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE import_task
          ADD COLUMN IF NOT EXISTS parse_status VARCHAR(12)
            NOT NULL DEFAULT 'ready',
          ADD COLUMN IF NOT EXISTS parse_error VARCHAR(32),
          ADD COLUMN IF NOT EXISTS source_file VARCHAR(256),
          ADD COLUMN IF NOT EXISTS source_size INTEGER,
          ADD COLUMN IF NOT EXISTS parse_lease_id UUID,
          ADD COLUMN IF NOT EXISTS parse_started_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS parse_lease_expires_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS parse_attempts SMALLINT NOT NULL DEFAULT 0
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname='ck_import_parse_status'
          ) THEN
            ALTER TABLE import_task ADD CONSTRAINT ck_import_parse_status
              CHECK (
                parse_status IN ('staging','pending','processing','ready','failed')
              );
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname='ck_import_parse_error'
          ) THEN
            ALTER TABLE import_task ADD CONSTRAINT ck_import_parse_error CHECK (
              parse_error IS NULL OR parse_error IN (
                'IMPORT_STAGE_FAILED','IMPORT_QUEUE_UNAVAILABLE',
                'IMPORT_RETRY_PENDING','IMPORT_FORMAT_INVALID',
                'IMPORT_TOO_LARGE','IMPORT_PARSE_FAILED'
              )
            );
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname='ck_import_parse_source'
          ) THEN
            ALTER TABLE import_task ADD CONSTRAINT ck_import_parse_source CHECK (
              parse_status NOT IN ('staging','pending','processing')
              OR (source_file IS NOT NULL AND source_size IS NOT NULL)
            );
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname='ck_import_parse_lease'
          ) THEN
            ALTER TABLE import_task ADD CONSTRAINT ck_import_parse_lease CHECK (
              (
                parse_status='processing'
                AND parse_lease_id IS NOT NULL
                AND parse_lease_expires_at IS NOT NULL
              )
              OR (
                parse_status<>'processing'
                AND parse_lease_id IS NULL
                AND parse_lease_expires_at IS NULL
              )
            );
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname='ck_import_source_size'
          ) THEN
            ALTER TABLE import_task ADD CONSTRAINT ck_import_source_size
              CHECK (source_size IS NULL OR source_size>=0);
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname='ck_import_parse_attempts'
          ) THEN
            ALTER TABLE import_task ADD CONSTRAINT ck_import_parse_attempts
              CHECK (parse_attempts>=0 AND parse_attempts<=3);
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_import_parse_due
          ON import_task(parse_status,parse_lease_expires_at,created_at)
          WHERE parse_status IN ('pending','processing')
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM import_task
            WHERE parse_status<>'ready'
               OR source_file IS NOT NULL
               OR parse_lease_id IS NOT NULL
          ) THEN
            RAISE EXCEPTION
              'cannot downgrade async import runtime with staged evidence';
          END IF;
        END $$;
        """
    )
    op.execute("DROP INDEX idx_import_parse_due")
    op.execute("ALTER TABLE import_task DROP CONSTRAINT ck_import_parse_attempts")
    op.execute("ALTER TABLE import_task DROP CONSTRAINT ck_import_parse_source")
    op.execute("ALTER TABLE import_task DROP CONSTRAINT ck_import_source_size")
    op.execute("ALTER TABLE import_task DROP CONSTRAINT ck_import_parse_lease")
    op.execute("ALTER TABLE import_task DROP CONSTRAINT ck_import_parse_error")
    op.execute("ALTER TABLE import_task DROP CONSTRAINT ck_import_parse_status")
    op.execute("ALTER TABLE import_task DROP COLUMN parse_attempts")
    op.execute("ALTER TABLE import_task DROP COLUMN parse_lease_expires_at")
    op.execute("ALTER TABLE import_task DROP COLUMN parse_started_at")
    op.execute("ALTER TABLE import_task DROP COLUMN parse_lease_id")
    op.execute("ALTER TABLE import_task DROP COLUMN source_size")
    op.execute("ALTER TABLE import_task DROP COLUMN source_file")
    op.execute("ALTER TABLE import_task DROP COLUMN parse_error")
    op.execute("ALTER TABLE import_task DROP COLUMN parse_status")
