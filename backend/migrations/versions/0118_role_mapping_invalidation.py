"""目录映射修改按 Provider 和数据库事务去重安全版本推进。"""

from alembic import op

revision = "0118_role_mapping_invalidation"
down_revision = "0117_review_acceptance_facts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE IF NOT EXISTS role_mapping_invalidation (
    provider_id BIGINT PRIMARY KEY REFERENCES auth_provider(id) ON DELETE CASCADE,
    transaction_id xid8 NOT NULL
);

    """)
    op.execute("""
CREATE OR REPLACE FUNCTION bump_role_mapping_security_version()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE
  affected_provider BIGINT;
  claimed_provider BIGINT;
  affected_providers BIGINT[];
  claimed_providers BIGINT[] := ARRAY[]::BIGINT[];
BEGIN
  IF TG_OP='UPDATE' THEN
    IF (OLD.provider_id,OLD.external_group,OLD.role,OLD.dept)
       IS NOT DISTINCT FROM (NEW.provider_id,NEW.external_group,NEW.role,NEW.dept) THEN
      RETURN NEW;
    END IF;
    affected_providers := ARRAY[OLD.provider_id,NEW.provider_id];
  ELSIF TG_OP='DELETE' THEN
    -- Provider 删除引发的级联没有剩余身份；不得重建已级联删除的去重事实。
    IF NOT EXISTS (SELECT 1 FROM public.auth_provider WHERE id=OLD.provider_id) THEN
      RETURN OLD;
    END IF;
    affected_providers := ARRAY[OLD.provider_id];
  ELSE
    affected_providers := ARRAY[NEW.provider_id];
  END IF;
  FOR affected_provider IN
    SELECT DISTINCT item FROM unnest(affected_providers) AS item ORDER BY item
  LOOP
    claimed_provider := NULL;
    INSERT INTO public.role_mapping_invalidation(provider_id,transaction_id)
    VALUES(affected_provider,pg_current_xact_id())
    ON CONFLICT(provider_id) DO UPDATE SET transaction_id=EXCLUDED.transaction_id
      WHERE role_mapping_invalidation.transaction_id<>EXCLUDED.transaction_id
    RETURNING provider_id INTO claimed_provider;
    IF claimed_provider IS NOT NULL THEN
      claimed_providers := array_append(claimed_providers,claimed_provider);
    END IF;
  END LOOP;
  -- 移动映射时同一账号可同时属于新旧 Provider，合并集合后只更新一次。
  UPDATE public.user_account ua
  SET security_version=ua.security_version+1,updated_at=now()
  WHERE EXISTS (
    SELECT 1 FROM public.auth_identity ai
    WHERE ai.account_id=ua.id AND ai.provider_id=ANY(claimed_providers)
  );
  RETURN COALESCE(NEW,OLD);
END
$$;
    """)

    op.execute("REVOKE ALL ON role_mapping_invalidation FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION bump_role_mapping_security_version() FROM PUBLIC")


def downgrade() -> None:
    op.execute("""
CREATE OR REPLACE FUNCTION bump_role_mapping_security_version()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  affected_provider BIGINT;
BEGIN
  IF TG_OP='UPDATE' THEN
    UPDATE user_account ua
    SET security_version=ua.security_version+1,updated_at=now()
    FROM auth_identity ai
    WHERE ai.account_id=ua.id
      AND ai.provider_id IN (OLD.provider_id,NEW.provider_id);
    RETURN NEW;
  END IF;
  affected_provider := CASE
    WHEN TG_OP='DELETE' THEN OLD.provider_id
    ELSE NEW.provider_id
  END;
  UPDATE user_account ua
  SET security_version=ua.security_version+1,updated_at=now()
  FROM auth_identity ai
  WHERE ai.account_id=ua.id AND ai.provider_id=affected_provider;
  RETURN COALESCE(NEW,OLD);
END
$$;
    """)
    op.execute("ALTER FUNCTION bump_role_mapping_security_version() SECURITY INVOKER")
    op.execute("ALTER FUNCTION bump_role_mapping_security_version() RESET search_path")
    op.execute("DROP TABLE role_mapping_invalidation")
