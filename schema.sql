-- ============================================================
-- 企业短信管理平台 schema.sql  (PostgreSQL 16)
-- v1.6.57  2026-08-17
-- v1.6.57：usage_projection.version 唯一；app 幂等作用域绑定 app_id
-- v1.6.56  2026-08-12
-- v1.6.56：LDAP 授权部门映射、黑名单 HMAC 轮换别名与精确查询加固
-- v1.6.55  2026-08-11
-- v1.6.55：模板正文上下文加密、敏感元数据手机号兜底约束、主体隔离用量键
-- v1.6.54  2026-08-11
-- v1.6.54：短信/回复展示正文上下文加密、拉取 raw-first HTTP 状态与密钥化回复事件键
-- v1.6.53  2026-08-11
-- v1.6.53：版本化幂等 HMAC、失败重发唯一事实、生命周期保留、callback 列权限与日报配置版本绑定
-- v1.6.52  2026-08-10
-- v1.6.52：厂商签名/模板 Bind 与单模板同步改由 realtime worker 消费精确 Outbox；API 不持凭据
-- v1.6.51  2026-08-09
-- v1.6.51：自治审计 HMAC 按 API/realtime/bulk 部署生产者域隔离
-- v1.6.50  2026-08-09
-- v1.6.50：审计事务上下文 HMAC 绑定；WeCom 使用 API 公钥/callback 私钥信封
-- v1.6.49  2026-08-09
-- v1.6.49：审计主体必须匹配事务上下文；系统事件绑定数据库角色与动作白名单
-- v1.6.48  2026-08-09
-- v1.6.48：企业微信 webhook 使用上下文绑定 AES-GCM 密文，历史明文升级时失效
-- v1.6.47  2026-08-09
-- v1.6.47：实时审计从事务上下文绑定稳定主体与关联 ID，拒绝新增 legacy_unknown
-- v1.6.46  2026-08-09
-- v1.6.46：敏感 sys_config 行启用 RLS；export 授权元数据禁止运行角色更新；
--           回调目标、Secret、明细开关或应用状态撤销时同事务隔离旧任务
-- v1.6.45  2026-08-05
-- v1.6.45：安全日报自动投递请求表补充 sms_send 最小读写授权（bulk worker 自动路径）
-- v1.6.44  2026-08-05
-- v1.6.44：approval_scan_seconds/scheduled_scan_seconds 纳入系统参数注册表（beat 启动读取）
-- v1.6.43  2026-08-03
-- v1.6.43：应用来源 IP/CIDR 白名单（allowed_ips，空数组=不限，仅 API Key 路径强制）
-- v1.6.42  2026-08-02
-- v1.6.42：安全日报记录区分自动/手动生成来源，支持手动重生成并立即投递
-- v1.6.41  2026-08-02
-- v1.6.41：安全日报管理审计证据只读视图（sms_send 最小 SELECT，不含载荷列）
-- v1.6.39  2026-08-01
-- v1.6.39：安全日报脱敏事实、独立 mailer 投递控制与管理员查询页面
-- v1.6.38  2026-07-29
-- v1.6.38：API 手动任务触发经 PostgreSQL Outbox 跨越 broker 隔离边界
-- v1.6.37  2026-07-29
-- v1.6.37：raw 解析/重放处理租约，阻止并发重复消费
-- v1.6.36  2026-07-29
-- v1.6.36：metrics 独立抓取凭据、列级只读授权与有界快照
-- v1.6.35  2026-07-29
-- v1.6.35：callback 签名材料固化与 v2 上下文绑定密文/导出帧
-- v1.6.34：首次改密令牌指纹、主体绑定与密码更新使用同一 PostgreSQL 事务
-- v1.6.33：七个运行职责数据库角色、显式最小授权与旧 sms_app 永久停用
-- v1.6.32：HTTP/任务/Outbox/callback/审计统一关联 ID 与双层载荷防泄漏
-- v1.6.31：导入源密文暂存、异步分块解析、租约恢复与 worker 背压
-- v1.6.30：Web 导入包 ready/reserved/consumed 可恢复预留与唯一批次绑定
-- v1.6.29：不可变 report/reply 事件事实、数据库去重与单调报告投影
-- v1.6.28：callback/export UUID 租约、fencing、稳定回调事件 ID 与租约事件证据
-- v1.6.27：配额/频控 PostgreSQL 事实账本、可恢复补偿与版本化 Redis 投影
-- v1.6.26：事务性 Outbox、租约/fencing、dead-letter 与确定受理语义
-- v1.6.25：授权、所有权、职责分离与审计统一使用稳定主体 ID
-- v1.6.27-hotfix：保留 auth_version 兼容列并与 security_version 事务级同步
-- v1.6.24：数据库权威安全上下文、统一 security_version 与 refresh 轮换
-- v1.6.23：导出任务不可枚举 ID、稳定主体与固化部门授权范围
-- v1.6.22：真实 UAT pre-batch 有界租约与崩溃恢复
-- v1.6.21：真实联调配置重置 operation 约束
-- v1.6.20：审批来源默认值兼容升级前 writer
-- v1.6.19：审批单固化创建时阈值；不可重建的历史记录显式标记未知
-- v1.6.18：真实联调测试号码跨 HMAC key 版本索引投影
-- v1.6.17：真实联调控制操作仅记录安全整数厂商错误码
-- v1.6.16：真实联调页面加密测试号码与无敏感控制操作
-- v1.6.15：真实厂商受控联调每日100计费条证据账本
-- v1.6.14：主体/认证源/身份/本地凭据分层账号模型
-- v1.6.13：sys_user.auth_version（角色变更与强制下线的事务级 JWT 失效依据）
-- v1.6.12：sys_config 登录账号失败阈值与锁定时长运行参数
-- v1.6.11：callback_report_event + callback_task.event_keys（报告回调重放事件快照）
-- v1.6.10：sms_chunk.retry_not_before（持久化重试到期时间）
-- v1.6.9：sms_chunk.submitting_since（提交领取时间与停滞恢复索引）
-- v1.6.8：audit_log PII 约束排除允许的 batch_no 顶层引用
-- v1.6.7：audit_log JSON 载荷 PII 数据库约束
-- v1.6.6：sms_chunk.uncertain_since（结果未知停留时间）
-- v1.6.5：sys_user 目录来源组与最近同步时间快照
-- v1.6.4：export_task.started_at（异步导出 worker 租约）
-- v1.6.3：告警 SMTP 无认证内网 relay 非敏感路由配置
-- v1.6.2：sms_sign.vendor_sign_id（BindSign/GetSignState同步依据）
-- v1.6.1：sms_batch.send_content_enc（可靠重投且OTP不落明文）
-- v1.6：owner/app 双角色 / 可过期幂等记录 / raw完整报文加密 /
--       callback无PII引用 / import_phone三列存储 / 导出密文落盘
-- v1.5：job_run(任务健康) / anomaly 异常检测参数
-- v1.4：unmatched_report(无主报告,迁移期对账) / verify_otp_mask 参数
-- v1.3：sms_template.var_specs(变量长度声明,厂商{sN}转换依据)
-- v1.1：category 分类 / 手机号三列加密 / raw_vendor_log /
--       import_task / callback_task / 双Key轮换 / uncertain /
--       audit_log 只增权限 / resend_of
-- v1.2：计费条(segments/quota_cost) / 号码级频控(freq_override,removed_freq) /
--       测试发送(is_test) / 营销同意留痕(consent_confirmed) /
--       stat_daily 计费条 / sys_config 频控与合规参数
-- 手机号规范：phone_enc BYTEA(AES-256-GCM, 应用层加密)
--            + phone_hmac CHAR(64)(HMAC-SHA256 hex, 精确查询索引)
--            + phone_mask VARCHAR(11)(如 138****0000, 列表展示)
--            + key_version SMALLINT(密钥轮换预留)
-- ============================================================

-- ─────────────── 数据库角色（等保：审计只增） ───────────────
-- 由独立 sms_owner 执行；运行进程按职责使用七个非 owner/非超级用户角色。
-- deploy/initdb/01-create-app-role.sh 先创建 NOLOGIN 占位角色；
-- provision-db-roles 再从独立 Docker secrets 设置 LOGIN 密码。
-- 本文件末尾显式授权，且永久禁用历史广权限 sms_app。

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ─────────────── 用户与权限 ───────────────
CREATE TABLE user_account (
    id            BIGSERIAL PRIMARY KEY,
    display_name  VARCHAR(128) NOT NULL DEFAULT '',
    dept          VARCHAR(128) NOT NULL DEFAULT '',
    role          VARCHAR(16)  NOT NULL DEFAULT 'viewer'
                  CHECK (role IN ('admin','approver','operator','viewer')),
    role_override BOOLEAN      NOT NULL DEFAULT TRUE,
    status        SMALLINT     NOT NULL DEFAULT 1 CHECK (status IN (0,1)),
    last_login_at TIMESTAMPTZ,
    auth_version     BIGINT    NOT NULL DEFAULT 1 CHECK (auth_version > 0),
    security_version BIGINT    NOT NULL DEFAULT 1 CHECK (security_version > 0),
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE auth_provider (
    id               BIGSERIAL PRIMARY KEY,
    code             VARCHAR(64)  NOT NULL UNIQUE,
    name             VARCHAR(128) NOT NULL,
    kind             VARCHAR(32)  NOT NULL,
    enabled          BOOLEAN      NOT NULL DEFAULT FALSE,
    draft_config     JSONB        NOT NULL DEFAULT '{}'::jsonb
                     CHECK (jsonb_typeof(draft_config) = 'object'),
    active_config    JSONB
                     CHECK (active_config IS NULL OR jsonb_typeof(active_config) = 'object'),
    draft_version    BIGINT       NOT NULL DEFAULT 1 CHECK (draft_version > 0),
    tested_version   BIGINT       CHECK (tested_version IS NULL OR tested_version > 0),
    active_version   BIGINT       CHECK (active_version IS NULL OR active_version > 0),
    last_tested_at   TIMESTAMPTZ,
    last_test_status VARCHAR(16)
                     CHECK (last_test_status IS NULL OR last_test_status IN ('success','failed')),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now()
);

INSERT INTO auth_provider (code, name, kind, enabled) VALUES
('local', '本地账号', 'local', TRUE),
('ad', 'AD 账号', 'ldap', FALSE);

CREATE TABLE auth_identity (
    id                    BIGSERIAL PRIMARY KEY,
    account_id            BIGINT       NOT NULL REFERENCES user_account(id) ON DELETE RESTRICT,
    provider_id           BIGINT       NOT NULL REFERENCES auth_provider(id) ON DELETE RESTRICT,
    login_name            VARCHAR(64)  NOT NULL,
    normalized_login_name VARCHAR(64)  NOT NULL,
    external_subject      VARCHAR(256) NOT NULL,
    status                SMALLINT     NOT NULL DEFAULT 1 CHECK (status IN (0,1)),
    source_groups         TEXT[]       NOT NULL DEFAULT '{}',
    last_synced_at        TIMESTAMPTZ,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_auth_identity_id_account UNIQUE (id, account_id),
    UNIQUE (normalized_login_name),
    UNIQUE (provider_id, external_subject),
    CHECK (normalized_login_name = lower(btrim(login_name)))
);

CREATE INDEX idx_auth_identity_account ON auth_identity(account_id);
CREATE INDEX idx_auth_identity_provider ON auth_identity(provider_id);

CREATE TABLE local_credential (
    identity_id         BIGINT      PRIMARY KEY REFERENCES auth_identity(id) ON DELETE RESTRICT,
    password_hash       TEXT        NOT NULL,
    must_change_password BOOLEAN    NOT NULL DEFAULT TRUE,
    password_changed_at TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE password_change_token (
    id                      BIGSERIAL PRIMARY KEY,
    token_hash              CHAR(64)    NOT NULL UNIQUE
                            CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    account_id              BIGINT      NOT NULL REFERENCES user_account(id) ON DELETE RESTRICT,
    identity_id             BIGINT      NOT NULL,
    provider_code           VARCHAR(64) NOT NULL,
    purpose                 VARCHAR(32) NOT NULL CHECK (purpose IN ('initial_password')),
    normalized_login_name   VARCHAR(64) NOT NULL,
    issued_security_version BIGINT      NOT NULL CHECK (issued_security_version > 0),
    status                  VARCHAR(12) NOT NULL DEFAULT 'available'
                            CHECK (status IN ('available','consumed','revoked','expired')),
    expires_at              TIMESTAMPTZ NOT NULL,
    consumed_at             TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_password_change_identity
      FOREIGN KEY (identity_id,account_id)
      REFERENCES auth_identity(id,account_id) ON DELETE RESTRICT,
    CONSTRAINT ck_password_change_consumed CHECK (
      (status='consumed' AND consumed_at IS NOT NULL)
      OR (status<>'consumed' AND consumed_at IS NULL)
    )
);
CREATE INDEX idx_password_change_account_available
    ON password_change_token(account_id,expires_at) WHERE status='available';

CREATE TABLE external_role_mapping (
    id             BIGSERIAL PRIMARY KEY,
    provider_id    BIGINT       NOT NULL REFERENCES auth_provider(id) ON DELETE CASCADE,
    external_group VARCHAR(256) NOT NULL,
    role           VARCHAR(16)  NOT NULL
                   CHECK (role IN ('admin','approver','operator','viewer')),
    dept           VARCHAR(128),
    CONSTRAINT ck_external_role_mapping_dept CHECK (
      dept IS NULL OR length(btrim(dept)) BETWEEN 1 AND 128
    ),
    UNIQUE (provider_id, external_group)
);

CREATE FUNCTION bump_account_security_version()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.dept IS DISTINCT FROM OLD.dept
     OR NEW.role IS DISTINCT FROM OLD.role
     OR NEW.role_override IS DISTINCT FROM OLD.role_override
     OR NEW.status IS DISTINCT FROM OLD.status THEN
    IF NEW.security_version = OLD.security_version THEN
      NEW.security_version := OLD.security_version + 1;
    END IF;
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER trg_user_account_security_version
BEFORE UPDATE ON user_account
FOR EACH ROW EXECUTE FUNCTION bump_account_security_version();

CREATE FUNCTION sync_account_security_versions()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.security_version IS DISTINCT FROM OLD.security_version
     AND NEW.auth_version IS DISTINCT FROM OLD.auth_version
     AND NEW.security_version IS DISTINCT FROM NEW.auth_version THEN
    RAISE EXCEPTION 'account security versions diverged';
  ELSIF NEW.security_version IS DISTINCT FROM OLD.security_version THEN
    NEW.auth_version := NEW.security_version;
  ELSIF NEW.auth_version IS DISTINCT FROM OLD.auth_version THEN
    NEW.security_version := NEW.auth_version;
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER zz_trg_account_security_version_sync
BEFORE UPDATE ON user_account
FOR EACH ROW EXECUTE FUNCTION sync_account_security_versions();

CREATE FUNCTION bump_identity_security_version()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.account_id IS DISTINCT FROM OLD.account_id
     OR NEW.provider_id IS DISTINCT FROM OLD.provider_id
     OR NEW.login_name IS DISTINCT FROM OLD.login_name
     OR NEW.external_subject IS DISTINCT FROM OLD.external_subject
     OR NEW.status IS DISTINCT FROM OLD.status
     OR NEW.source_groups IS DISTINCT FROM OLD.source_groups THEN
    UPDATE user_account
    SET security_version=security_version+1,updated_at=now()
    WHERE id IN (OLD.account_id,NEW.account_id);
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER trg_auth_identity_security_version
AFTER UPDATE ON auth_identity
FOR EACH ROW EXECUTE FUNCTION bump_identity_security_version();

CREATE FUNCTION bump_provider_security_version()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.enabled IS DISTINCT FROM OLD.enabled
     OR NEW.active_version IS DISTINCT FROM OLD.active_version
     OR NEW.active_config IS DISTINCT FROM OLD.active_config THEN
    UPDATE user_account ua
    SET security_version=ua.security_version+1,updated_at=now()
    FROM auth_identity ai
    WHERE ai.account_id=ua.id AND ai.provider_id=NEW.id;
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER trg_auth_provider_security_version
AFTER UPDATE ON auth_provider
FOR EACH ROW EXECUTE FUNCTION bump_provider_security_version();

CREATE FUNCTION bump_role_mapping_security_version()
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
CREATE TRIGGER trg_external_role_mapping_security_version
AFTER INSERT OR UPDATE OR DELETE ON external_role_mapping
FOR EACH ROW EXECUTE FUNCTION bump_role_mapping_security_version();

-- ─────────────── 接入应用 ───────────────
CREATE TABLE app (
    id                   BIGSERIAL PRIMARY KEY,
    name                 VARCHAR(64)  NOT NULL UNIQUE,
    dept                 VARCHAR(128) NOT NULL,
    api_key_hash         CHAR(64)     NOT NULL,           -- 当前Key SHA-256
    api_key_prefix       CHAR(8)      NOT NULL,
    api_key_prev_hash    CHAR(64),                        -- 轮换宽限期旧Key
    api_key_prev_prefix  CHAR(8),
    api_key_prev_expires TIMESTAMPTZ,                     -- 旧Key失效时间
    allowed_categories   VARCHAR(32)  NOT NULL DEFAULT 'verify,notice,market',
    default_sign         VARCHAR(32),
    daily_quota          INTEGER      NOT NULL DEFAULT 0, -- 0=不限
    rate_limit_per_min   INTEGER      NOT NULL DEFAULT 60,
    blacklist_check      BOOLEAN      NOT NULL DEFAULT TRUE, -- notice 类是否查黑名单
    freq_override        JSONB,                           -- 号码级频控应用覆盖,如 {"verify_per_minute":2,"verify_per_day":20,"market_per_day":2}
    allowed_ips          TEXT[]       NOT NULL DEFAULT '{}', -- 入站来源CIDR白名单;空=不限
    callback_url         VARCHAR(256),                    -- 结果回调(内网CIDR白名单校验)
    callback_secret_enc  BYTEA,                           -- 回调签名密钥(加密存储)
    callback_report_enabled BOOLEAN   NOT NULL DEFAULT FALSE, -- 明细级回调开关
    status               SMALLINT     NOT NULL DEFAULT 1,
    created_by           VARCHAR(64)  NOT NULL,
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX idx_app_key_prefix      ON app(api_key_prefix);
CREATE INDEX idx_app_prev_key_prefix ON app(api_key_prev_prefix)
    WHERE api_key_prev_prefix IS NOT NULL;

CREATE TABLE dept_quota (
    dept        VARCHAR(128) PRIMARY KEY,
    daily_quota INTEGER NOT NULL DEFAULT 0
);

-- ─────────────── 批次 / 分片 / 消息 ───────────────
CREATE TABLE sms_batch (
    id                BIGSERIAL PRIMARY KEY,
    batch_no          CHAR(32)     NOT NULL UNIQUE,
    category          VARCHAR(8)   NOT NULL DEFAULT 'notice'
                      CHECK (category IN ('verify','notice','market')),
    channel           VARCHAR(8)   NOT NULL CHECK (channel IN ('api','web')),
    app_id            BIGINT       REFERENCES app(id),
    creator           VARCHAR(64),                         -- 仅展示快照，不参与授权
    creator_account_id BIGINT REFERENCES user_account(id) ON DELETE RESTRICT,
    creator_identity_id BIGINT,
    dept              VARCHAR(128) NOT NULL,
    content           VARCHAR(600) NOT NULL DEFAULT '[encrypted]', -- 固定非敏感标记
    display_content_enc BYTEA       NOT NULL,             -- 掩码展示正文：版本头+上下文 AES-GCM
    send_content_enc  BYTEA        NOT NULL,             -- 实际下发内容：版本头+上下文 AES-GCM
    sign_name         VARCHAR(32),
    template_id       BIGINT,
    biz_id            VARCHAR(32),
    resend_of         BIGINT,                             -- 失败重发溯源批次id
    is_test           BOOLEAN      NOT NULL DEFAULT FALSE, -- 测试发送(≤5号码,豁免营销时间窗)
    consent_confirmed BOOLEAN      NOT NULL DEFAULT FALSE, -- market Web渠道"已获用户同意"勾选留痕
    segments          SMALLINT     NOT NULL DEFAULT 1,     -- 单条消息计费条数(含签名与退订语)
    quota_cost        INTEGER      NOT NULL DEFAULT 0,     -- 本批次配额消耗 = accepted × segments
    status            VARCHAR(20)  NOT NULL DEFAULT 'queued'
        CHECK (status IN ('pending_approval','rejected','scheduled','queued',
                          'sending','completed','cancelled','balance_blocked','expired')),
    deferred_reason   VARCHAR(64),                        -- 如 market_window
    total             INTEGER      NOT NULL DEFAULT 0,
    removed_duplicate INTEGER      NOT NULL DEFAULT 0,
    removed_blacklist INTEGER      NOT NULL DEFAULT 0,
    removed_freq      INTEGER      NOT NULL DEFAULT 0,     -- 号码级频控剔除数
    delivered         INTEGER      NOT NULL DEFAULT 0,
    failed            INTEGER      NOT NULL DEFAULT 0,
    unknown_cnt       INTEGER      NOT NULL DEFAULT 0,
    scheduled_at      TIMESTAMPTZ,
    remark            VARCHAR(200),
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT fk_sms_batch_creator_identity
      FOREIGN KEY (creator_identity_id,creator_account_id)
      REFERENCES auth_identity(id,account_id) ON DELETE RESTRICT,
    CONSTRAINT ck_sms_batch_creator_principal_pair CHECK (
      (creator_account_id IS NULL AND creator_identity_id IS NULL)
      OR (creator_account_id IS NOT NULL AND creator_identity_id IS NOT NULL)
    ),
    CONSTRAINT ck_sms_batch_content_marker CHECK (content='[encrypted]'),
    CONSTRAINT ck_sms_batch_remark_no_phone CHECK (
      remark IS NULL OR remark !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
    )
);
-- biz_id 只用于追踪；过期后允许复用，唯一性由 idempotency_record 管理。
CREATE INDEX idx_batch_app_biz ON sms_batch(app_id, biz_id)
    WHERE biz_id IS NOT NULL AND app_id IS NOT NULL;
CREATE INDEX idx_batch_created  ON sms_batch(created_at);
CREATE INDEX idx_batch_dept     ON sms_batch(dept, created_at);
CREATE INDEX idx_batch_app      ON sms_batch(app_id, created_at);
CREATE INDEX idx_batch_category ON sms_batch(category, created_at);
CREATE INDEX idx_sms_batch_creator_account
    ON sms_batch(creator_account_id, created_at DESC);
CREATE INDEX idx_batch_active   ON sms_batch(status)
    WHERE status IN ('scheduled','queued','sending','balance_blocked');

-- 失败重发一代一事实：历史重复子批次保留，但新请求只能原子认领一次。
CREATE TABLE sms_resend_action (
    source_batch_id BIGINT PRIMARY KEY REFERENCES sms_batch(id) ON DELETE RESTRICT,
    child_batch_id  BIGINT NOT NULL UNIQUE REFERENCES sms_batch(id) ON DELETE RESTRICT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_sms_resend_action_distinct CHECK (source_batch_id <> child_batch_id)
);

-- 幂等 DB 兜底：仅在同键 expires_at<=now() 且关联批次不再具有外部副作用时删除，
-- 再以同事务创建 batch+record；唯一冲突时回查仍受保护的原批次。
-- request_hash 是版本化 HMAC 指纹，生命周期至少覆盖批次活跃期和
-- scheduled_at + 安全窗口，避免明文摘要离线枚举与延迟任务重复发送。
-- scope_kind/scope_id：app=<app_id>；account=<account_id>:<identity_id>；
-- web-legacy/global 仅为迁移前 app_id IS NULL 旧记录的短期兼容作用域。
-- ck_idem_app_scope：仅 app 作用域要求 scope_id 绑定 app_id，不误伤其它 kind。
CREATE TABLE idempotency_record (
    id         BIGSERIAL PRIMARY KEY,
    app_id     BIGINT       REFERENCES app(id),
    scope_kind VARCHAR(16)  NOT NULL,
    scope_id   VARCHAR(64)  NOT NULL,
    biz_id     VARCHAR(32)  NOT NULL,
    request_hash VARCHAR(64),
    request_hash_key_version SMALLINT,
    batch_id   BIGINT       NOT NULL UNIQUE REFERENCES sms_batch(id),
    expires_at TIMESTAMPTZ  NOT NULL,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uk_idem_app_biz UNIQUE (scope_kind, scope_id, biz_id),
    CONSTRAINT ck_idem_app_scope CHECK (
      scope_kind <> 'app'
      OR (app_id IS NOT NULL AND scope_id = app_id::text)
    ),
    CONSTRAINT ck_idem_request_fingerprint CHECK (
      (request_hash IS NULL AND request_hash_key_version IS NULL)
      OR (
        request_hash ~ '^[0-9a-f]{64}$'
        AND request_hash_key_version BETWEEN 1 AND 32767
      )
    )
);
CREATE INDEX idx_idem_expire ON idempotency_record(expires_at);

CREATE TABLE sms_chunk (
    id             BIGSERIAL PRIMARY KEY,
    batch_id       BIGINT      NOT NULL REFERENCES sms_batch(id),
    chunk_no       INTEGER     NOT NULL,
    custom_id      CHAR(32)    NOT NULL UNIQUE,           -- 批次前缀24+序号8
    vendor_task_id VARCHAR(64),
    phone_count    INTEGER     NOT NULL,
    status         VARCHAR(16) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','submitting','submitted','failed','retrying','uncertain')),
    vendor_code    INTEGER,
    vendor_msg     VARCHAR(256),
    retry_count    SMALLINT    NOT NULL DEFAULT 0,
    vendor_attempt_count SMALLINT NOT NULL DEFAULT 0,
    retry_not_before TIMESTAMPTZ,
    submitted_at   TIMESTAMPTZ,
    submitting_since TIMESTAMPTZ,
    uncertain_since TIMESTAMPTZ,
    UNIQUE (batch_id, chunk_no),
    CONSTRAINT chk_chunk_vendor_attempt_count CHECK (vendor_attempt_count >= 0),
    CONSTRAINT ck_sms_chunk_vendor_task_pseudonym CHECK (
      vendor_task_id IS NULL OR vendor_task_id ~ '^[0-9a-f]{64}$'
    )
);
CREATE INDEX idx_chunk_taskid    ON sms_chunk(vendor_task_id);
CREATE INDEX idx_chunk_retry_due ON sms_chunk(retry_not_before) WHERE status = 'retrying';
CREATE INDEX idx_chunk_submitting ON sms_chunk(submitting_since) WHERE status = 'submitting';
CREATE INDEX idx_chunk_uncertain ON sms_chunk(status) WHERE status = 'uncertain';

-- ─────────────── 真实厂商受控联调每日额度（无PII证据账本） ───────────────
CREATE TABLE vendor_test_daily_usage (
    usage_date          DATE        PRIMARY KEY,
    in_flight_segments  INTEGER     NOT NULL DEFAULT 0
                        CHECK (in_flight_segments >= 0),
    confirmed_segments  INTEGER     NOT NULL DEFAULT 0
                        CHECK (confirmed_segments >= 0),
    uncertain_segments  INTEGER     NOT NULL DEFAULT 0
                        CHECK (uncertain_segments >= 0),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_vendor_test_daily_total CHECK (
      in_flight_segments + confirmed_segments + uncertain_segments <= 100
    )
);

CREATE TABLE vendor_test_send_attempt (
    id          BIGSERIAL PRIMARY KEY,
    usage_date  DATE        NOT NULL
                REFERENCES vendor_test_daily_usage(usage_date) ON DELETE RESTRICT,
    chunk_id    BIGINT      NOT NULL REFERENCES sms_chunk(id) ON DELETE RESTRICT,
    attempt_no  SMALLINT    NOT NULL CHECK (attempt_no > 0),
    segments    INTEGER     NOT NULL CHECK (segments > 0),
    status      VARCHAR(16) NOT NULL
                CHECK (status IN ('reserved','confirmed','uncertain','released')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at  TIMESTAMPTZ,
    UNIQUE (chunk_id, attempt_no),
    CONSTRAINT chk_vendor_test_attempt_settlement CHECK (
      (status = 'reserved' AND settled_at IS NULL)
      OR (status <> 'reserved' AND settled_at IS NOT NULL)
    )
);
CREATE UNIQUE INDEX uk_vendor_test_reserved_chunk
    ON vendor_test_send_attempt(chunk_id) WHERE status = 'reserved';
CREATE INDEX idx_vendor_test_attempt_usage_status
    ON vendor_test_send_attempt(usage_date, status);

-- ─────────────── 真实联调页面控制面 ───────────────
CREATE TABLE vendor_test_recipient (
    id             BIGSERIAL PRIMARY KEY,
    label          VARCHAR(64) NOT NULL,
    phone_enc      BYTEA       NOT NULL,
    phone_hmac     CHAR(64)    NOT NULL,
    phone_mask     VARCHAR(32) NOT NULL,
    key_version    SMALLINT    NOT NULL CHECK (key_version > 0),
    status         VARCHAR(16) NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active','disabled')),
    created_by     VARCHAR(64) NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    disabled_by    VARCHAR(64),
    disabled_at    TIMESTAMPTZ,
    UNIQUE (key_version, phone_hmac),
    CONSTRAINT chk_vendor_test_recipient_disabled CHECK (
      (status = 'active' AND disabled_by IS NULL AND disabled_at IS NULL)
      OR (status = 'disabled' AND disabled_by IS NOT NULL AND disabled_at IS NOT NULL)
    )
);
CREATE INDEX idx_vendor_test_recipient_active
    ON vendor_test_recipient(id) WHERE status = 'active';

CREATE TABLE vendor_test_recipient_hmac_alias (
    recipient_id BIGINT   NOT NULL
                 REFERENCES vendor_test_recipient(id) ON DELETE CASCADE,
    hmac_key_version SMALLINT NOT NULL CHECK (hmac_key_version > 0),
    hmac_digest      CHAR(64) NOT NULL CHECK (hmac_digest ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (recipient_id, hmac_key_version),
    UNIQUE (hmac_key_version, hmac_digest)
);
CREATE INDEX idx_vendor_test_recipient_hmac_alias_lookup
    ON vendor_test_recipient_hmac_alias(hmac_key_version, hmac_digest);

CREATE TABLE vendor_test_operation (
    id             UUID        PRIMARY KEY,
    operation_type VARCHAR(32) NOT NULL
                   CHECK (operation_type IN (
                     'install_credentials','rotate_credentials','activate','pause','resume','uat_send',
                     'reset_configuration'
                   )),
    actor          VARCHAR(64) NOT NULL,                 -- 事件时登录名快照
    actor_account_id BIGINT REFERENCES user_account(id) ON DELETE RESTRICT,
    actor_identity_id BIGINT,
    status         VARCHAR(16) NOT NULL
                   CHECK (status IN ('requested','running','succeeded','failed')),
    safe_code      VARCHAR(64),
    vendor_code    INTEGER CHECK (vendor_code BETWEEN 1 AND 99999),
    batch_no       VARCHAR(64),
    checkpoint_id  VARCHAR(128),
    lease_expires_at TIMESTAMPTZ,
    requested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at   TIMESTAMPTZ,
    CONSTRAINT fk_vendor_test_operation_actor_identity
      FOREIGN KEY (actor_identity_id,actor_account_id)
      REFERENCES auth_identity(id,account_id) ON DELETE RESTRICT,
    CONSTRAINT ck_vendor_test_operation_actor_principal_pair CHECK (
      (actor_account_id IS NULL AND actor_identity_id IS NULL)
      OR (actor_account_id IS NOT NULL AND actor_identity_id IS NOT NULL)
    ),
    CONSTRAINT chk_vendor_test_operation_completion CHECK (
      (status IN ('requested','running') AND completed_at IS NULL)
      OR (status IN ('succeeded','failed') AND completed_at IS NOT NULL)
    ),
    CONSTRAINT chk_vendor_test_operation_vendor_code CHECK (
      vendor_code IS NULL OR (status = 'failed' AND safe_code = 'VENDOR_ERROR')
    ),
    CONSTRAINT chk_vendor_test_operation_uat_lease CHECK (
      (operation_type <> 'uat_send' AND lease_expires_at IS NULL)
      OR (
        operation_type = 'uat_send'
        AND (
          (
            status IN ('requested','running')
            AND (
              (batch_no IS NULL AND lease_expires_at IS NOT NULL)
              OR (batch_no IS NOT NULL AND lease_expires_at IS NULL)
            )
          )
          OR (
            status IN ('succeeded','failed')
            AND lease_expires_at IS NULL
          )
        )
      )
    )
);
CREATE INDEX idx_vendor_test_operation_status_time
    ON vendor_test_operation(status, requested_at);
CREATE INDEX idx_vendor_test_operation_actor_account
    ON vendor_test_operation(actor_account_id, requested_at DESC);
CREATE INDEX idx_vendor_test_operation_uat_lease
    ON vendor_test_operation(lease_expires_at, id)
    WHERE operation_type = 'uat_send'
      AND status IN ('requested','running')
      AND batch_no IS NULL;

-- 消息明细（按月分区，保留12个月）
CREATE TABLE sms_message (
    id             BIGSERIAL,
    batch_id       BIGINT      NOT NULL,
    chunk_id       BIGINT,
    phone_enc      BYTEA       NOT NULL,
    phone_hmac     CHAR(64)    NOT NULL,
    phone_mask     VARCHAR(11) NOT NULL,
    key_version    SMALLINT    NOT NULL DEFAULT 1,
    status         VARCHAR(10) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','sent','delivered','failed','unknown','other')),
    report_status  SMALLINT,
    report_desc    VARCHAR(128),
    report_time    TIMESTAMPTZ,
    report_event_key CHAR(64),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

CREATE INDEX idx_msg_batch  ON sms_message(batch_id);
CREATE INDEX idx_msg_hmac   ON sms_message(phone_hmac, created_at);
CREATE INDEX idx_msg_active ON sms_message(status) WHERE status IN ('pending','sent');

CREATE TABLE sms_message_2026_07 PARTITION OF sms_message
    FOR VALUES FROM ('2026-07-01+08') TO ('2026-08-01+08');
CREATE TABLE sms_message_2026_08 PARTITION OF sms_message
    FOR VALUES FROM ('2026-08-01+08') TO ('2026-09-01+08');

-- 上行回复（按月分区，保留12个月）
CREATE TABLE sms_reply (
    id             BIGSERIAL,
    event_key      CHAR(64)    NOT NULL,
    vendor_task_id VARCHAR(64),
    batch_id       BIGINT,
    phone_enc      BYTEA       NOT NULL,
    phone_hmac     CHAR(64)    NOT NULL,
    phone_mask     VARCHAR(11) NOT NULL,
    key_version    SMALLINT    NOT NULL DEFAULT 1,
    ext_code       VARCHAR(8) NOT NULL DEFAULT '',
    content        VARCHAR(500) NOT NULL DEFAULT '[encrypted]',
    reply_time     TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, created_at),
    CONSTRAINT ck_sms_reply_content_marker CHECK (content='[encrypted]'),
    CONSTRAINT ck_sms_reply_vendor_task_pseudonym CHECK (
      vendor_task_id IS NULL OR vendor_task_id ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_sms_reply_ext_code_redacted CHECK (ext_code='')
) PARTITION BY RANGE (created_at);
CREATE INDEX idx_reply_hmac ON sms_reply(phone_hmac, created_at);
CREATE INDEX idx_reply_event ON sms_reply(event_key);

CREATE TABLE sms_reply_2026_07 PARTITION OF sms_reply
    FOR VALUES FROM ('2026-07-01+08') TO ('2026-08-01+08');

-- ─────────────── 厂商原始报文落地（拉走即消费的兜底） ───────────────
CREATE TABLE raw_vendor_log (
    id             BIGSERIAL PRIMARY KEY,
    source         VARCHAR(8)  NOT NULL CHECK (source IN ('report','reply')),
    payload_enc    BYTEA       NOT NULL, -- 完整原始响应字节的 AES-256-GCM 密文
    payload_sha256 CHAR(64)    NOT NULL, -- 密文落库前对原始字节计算，重放校验完整性
    key_version    SMALLINT    NOT NULL DEFAULT 1,
    http_status    SMALLINT    NOT NULL DEFAULT 200,
    content_encoding VARCHAR(16) NOT NULL DEFAULT 'identity',
    custom_ids     TEXT[]      NOT NULL DEFAULT '{}', -- 只含 customId，不含 phone/content
    item_count     INTEGER     NOT NULL DEFAULT 0,
    processed      BOOLEAN     NOT NULL DEFAULT FALSE,
    processing_started_at TIMESTAMPTZ,
    error          VARCHAR(256),
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_raw_vendor_http_status CHECK (http_status BETWEEN 100 AND 599),
    CONSTRAINT ck_raw_vendor_content_encoding
      CHECK (content_encoding IN ('identity','unsupported'))
);
CREATE INDEX idx_raw_unprocessed ON raw_vendor_log(processed, fetched_at)
    WHERE processed = FALSE;
CREATE INDEX idx_raw_processing_lease ON raw_vendor_log(processing_started_at, id)
    WHERE processed = FALSE;
CREATE INDEX idx_raw_fetched ON raw_vendor_log(fetched_at);
-- GIN 索引支持 uncertain 分片按无PII customId 元数据比对
CREATE INDEX idx_raw_custom_ids ON raw_vendor_log USING GIN (custom_ids);

CREATE OR REPLACE FUNCTION enforce_raw_vendor_custom_ids()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $$
DECLARE candidate TEXT;
BEGIN
  FOREACH candidate IN ARRAY NEW.custom_ids LOOP
    IF candidate !~ '^[A-Za-z0-9]{32}$'
       OR NOT EXISTS (
         SELECT 1 FROM public.sms_chunk c WHERE trim(c.custom_id)=candidate
       ) THEN
      RAISE EXCEPTION 'raw vendor customId is not a known platform identifier'
        USING ERRCODE='23514';
    END IF;
  END LOOP;
  RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION enforce_raw_vendor_custom_ids() FROM PUBLIC;
CREATE TRIGGER trg_raw_vendor_custom_ids
BEFORE INSERT OR UPDATE OF custom_ids ON raw_vendor_log
FOR EACH ROW EXECUTE FUNCTION enforce_raw_vendor_custom_ids();

-- ─────────────── 厂商事件事实与单调投影（v1.6.29） ───────────────
-- 两张 event 表只追加；raw_id=NULL 仅用于升级时从历史投影回填的兼容事实。
CREATE TABLE report_event (
    event_key       CHAR(64) PRIMARY KEY
                    CHECK (event_key ~ '^[0-9a-f]{64}$'),
    raw_id          BIGINT, -- raw 90天清理后允许成为仅审计用历史引用
    vendor_task_id  VARCHAR(64) NOT NULL,
    custom_id       VARCHAR(64) NOT NULL,
    phone_enc       BYTEA       NOT NULL,
    phone_hmac      CHAR(64)    NOT NULL
                    CHECK (phone_hmac ~ '^[0-9a-f]{64}$'),
    phone_mask      VARCHAR(11) NOT NULL,
    key_version     SMALLINT    NOT NULL CHECK (key_version>0),
    report_status   SMALLINT    NOT NULL,
    message_status  VARCHAR(10) NOT NULL
                    CHECK (message_status IN ('delivered','failed','unknown','other')),
    report_desc     VARCHAR(128) NOT NULL DEFAULT '',
    report_time     TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_report_event_vendor_task_pseudonym
      CHECK (vendor_task_id ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_report_event_custom_pseudonym
      CHECK (custom_id ~ '^[0-9a-f]{64}$')
);
CREATE INDEX idx_report_event_raw ON report_event(raw_id);
CREATE INDEX idx_report_event_custom ON report_event(custom_id,report_time);

CREATE TABLE report_event_projection (
    event_key          CHAR(64) PRIMARY KEY
                       REFERENCES report_event(event_key) ON DELETE RESTRICT,
    batch_id           BIGINT NOT NULL,
    message_id         BIGINT NOT NULL,
    message_created_at TIMESTAMPTZ NOT NULL,
    projection_changed BOOLEAN NOT NULL,
    projected_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY(message_id,message_created_at)
      REFERENCES sms_message(id,created_at) ON DELETE RESTRICT
);
CREATE INDEX idx_report_projection_message
    ON report_event_projection(message_id,message_created_at,projected_at);

CREATE TABLE reply_event (
    event_key       CHAR(64) PRIMARY KEY
                    CHECK (event_key ~ '^[0-9a-f]{64}$'),
    event_key_version SMALLINT NOT NULL,
    raw_id          BIGINT, -- raw 90天清理后允许成为仅审计用历史引用
    vendor_task_id  VARCHAR(64) NOT NULL,
    custom_id       VARCHAR(64),
    phone_enc       BYTEA       NOT NULL,
    phone_hmac      CHAR(64)    NOT NULL
                    CHECK (phone_hmac ~ '^[0-9a-f]{64}$'),
    phone_mask      VARCHAR(11) NOT NULL,
    key_version     SMALLINT    NOT NULL CHECK (key_version>0),
    ext_code        VARCHAR(8) NOT NULL DEFAULT '',
    content         VARCHAR(500) NOT NULL DEFAULT '[encrypted]',
    content_enc     BYTEA NOT NULL,
    reply_time      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_reply_event_content_marker CHECK (content='[encrypted]'),
    CONSTRAINT ck_reply_event_key_version CHECK (event_key_version>0),
    CONSTRAINT ck_reply_event_vendor_task_pseudonym
      CHECK (vendor_task_id ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_reply_event_custom_pseudonym
      CHECK (custom_id IS NULL OR custom_id ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_reply_event_ext_code_redacted CHECK (ext_code='')
);
CREATE INDEX idx_reply_event_raw ON reply_event(raw_id);
CREATE INDEX idx_reply_event_time ON reply_event(reply_time);

ALTER TABLE sms_message ADD CONSTRAINT fk_message_report_event
  FOREIGN KEY(report_event_key) REFERENCES report_event(event_key) ON DELETE RESTRICT;
ALTER TABLE sms_reply ADD CONSTRAINT fk_reply_event
  FOREIGN KEY(event_key) REFERENCES reply_event(event_key) ON DELETE RESTRICT;

-- ─────────────── 无主报告（并行迁移期，v1.4） ───────────────
-- customId 匹配不到平台分片的报告：不丢弃，供未迁移直连系统人工对账
CREATE TABLE unmatched_report (
    id            BIGSERIAL PRIMARY KEY,
    event_key     CHAR(64) NOT NULL,
    vendor_task_id VARCHAR(64),
    custom_id     VARCHAR(64),
    phone_enc     BYTEA       NOT NULL,
    phone_hmac    CHAR(64)    NOT NULL,
    phone_mask    VARCHAR(11) NOT NULL,
    key_version   SMALLINT    NOT NULL DEFAULT 1,
    report_status SMALLINT,
    report_desc   VARCHAR(128),
    report_time   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uk_unmatched_report_event UNIQUE(event_key),
    CONSTRAINT fk_unmatched_report_event
      FOREIGN KEY(event_key) REFERENCES report_event(event_key) ON DELETE RESTRICT,
    CONSTRAINT ck_unmatched_vendor_task_pseudonym CHECK (
      vendor_task_id IS NULL OR vendor_task_id ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_unmatched_custom_pseudonym CHECK (
      custom_id IS NULL OR custom_id ~ '^[0-9a-f]{64}$'
    )
);
CREATE INDEX idx_unmatched_hmac ON unmatched_report(phone_hmac, created_at);
CREATE INDEX idx_unmatched_time ON unmatched_report(created_at);

-- ─────────────── 后台任务健康（v1.5） ───────────────
CREATE TABLE job_run (
    id          BIGSERIAL PRIMARY KEY,
    job_name    VARCHAR(48)  NOT NULL,      -- poll_report / reconcile / anomaly_scan ...
    started_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    duration_ms INTEGER,
    status      VARCHAR(10)  NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','success','failed')),
    items       INTEGER      NOT NULL DEFAULT 0,   -- 本次处理条数
    error       VARCHAR(512)
);
CREATE INDEX idx_job_name_time ON job_run(job_name, started_at DESC);
CREATE INDEX idx_job_failed ON job_run(status, started_at) WHERE status = 'failed';

-- ─────────────── Web 导入任务 ───────────────
CREATE TABLE import_task (
    id           BIGSERIAL PRIMARY KEY,
    import_id    UUID        NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    creator      VARCHAR(64) NOT NULL,                    -- 事件时登录名快照
    creator_account_id BIGINT REFERENCES user_account(id) ON DELETE RESTRICT,
    creator_identity_id BIGINT,
    filename     VARCHAR(256) NOT NULL,
    valid_cnt    INTEGER     NOT NULL DEFAULT 0,
    invalid_cnt  INTEGER     NOT NULL DEFAULT 0,
    dup_cnt      INTEGER     NOT NULL DEFAULT 0,
    black_cnt    INTEGER     NOT NULL DEFAULT 0,
    invalid_file VARCHAR(256),                            -- 仅phone_mask+原行号+原因的CSV
    parse_status VARCHAR(12) NOT NULL DEFAULT 'ready',
    parse_error  VARCHAR(32),
    source_file  VARCHAR(256),                            -- SMSI1 分帧 AES-GCM 密文
    source_size  INTEGER,
    parse_lease_id UUID,
    parse_started_at TIMESTAMPTZ,
    parse_lease_expires_at TIMESTAMPTZ,
    parse_attempts SMALLINT NOT NULL DEFAULT 0,
    used         BOOLEAN     NOT NULL DEFAULT FALSE,    -- 滚动升级兼容列；新代码不再读写
    state        VARCHAR(10) NOT NULL DEFAULT 'ready',
    reservation_id UUID,
    reserved_by_account_id BIGINT,
    reserved_at  TIMESTAMPTZ,
    reservation_expires_at TIMESTAMPTZ,
    consumed_batch_id BIGINT,
    consumed_at  TIMESTAMPTZ,
    payload_purged_at TIMESTAMPTZ,                       -- 过期后仅保留无PII消费绑定
    expires_at   TIMESTAMPTZ NOT NULL,                    -- 创建+24h
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_import_task_creator_identity
      FOREIGN KEY (creator_identity_id,creator_account_id)
      REFERENCES auth_identity(id,account_id) ON DELETE RESTRICT,
    CONSTRAINT fk_import_reserved_account
      FOREIGN KEY (reserved_by_account_id)
      REFERENCES user_account(id) ON DELETE RESTRICT,
    CONSTRAINT fk_import_consumed_batch
      FOREIGN KEY (consumed_batch_id)
      REFERENCES sms_batch(id) ON DELETE RESTRICT,
    CONSTRAINT uk_import_reservation_id UNIQUE (reservation_id),
    CONSTRAINT uk_import_consumed_batch UNIQUE (consumed_batch_id),
    CONSTRAINT ck_import_task_canonical_filename
      CHECK (filename IN ('upload.csv','upload.xlsx')),
    CONSTRAINT ck_import_task_state
      CHECK (state IN ('ready','reserved','consumed')),
    CONSTRAINT ck_import_parse_status
      CHECK (parse_status IN ('staging','pending','processing','ready','failed')),
    CONSTRAINT ck_import_source_size
      CHECK (source_size IS NULL OR source_size>=0),
    CONSTRAINT ck_import_parse_source CHECK (
      parse_status NOT IN ('staging','pending','processing')
      OR (source_file IS NOT NULL AND source_size IS NOT NULL)
    ),
    CONSTRAINT ck_import_parse_attempts
      CHECK (parse_attempts>=0 AND parse_attempts<=3),
    CONSTRAINT ck_import_parse_error
      CHECK (
        parse_error IS NULL OR parse_error IN (
          'IMPORT_STAGE_FAILED','IMPORT_QUEUE_UNAVAILABLE',
          'IMPORT_RETRY_PENDING','IMPORT_FORMAT_INVALID',
          'IMPORT_TOO_LARGE','IMPORT_PARSE_FAILED'
        )
      ),
    CONSTRAINT ck_import_parse_lease CHECK (
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
    ),
    CONSTRAINT ck_import_task_creator_principal_pair CHECK (
      (creator_account_id IS NULL AND creator_identity_id IS NULL)
      OR (creator_account_id IS NOT NULL AND creator_identity_id IS NOT NULL)
    ),
    CONSTRAINT ck_import_task_reservation_state CHECK (
      (
        state='ready'
        AND reservation_id IS NULL
        AND reserved_by_account_id IS NULL
        AND reserved_at IS NULL
        AND reservation_expires_at IS NULL
        AND consumed_batch_id IS NULL
        AND consumed_at IS NULL
      )
      OR (
        state='reserved'
        AND reservation_id IS NOT NULL
        AND reserved_by_account_id IS NOT NULL
        AND reserved_at IS NOT NULL
        AND reservation_expires_at IS NOT NULL
        AND consumed_batch_id IS NULL
        AND consumed_at IS NULL
      )
      OR (
        state='consumed'
        AND reservation_id IS NOT NULL
        AND reserved_by_account_id IS NOT NULL
        AND reserved_at IS NOT NULL
        AND reservation_expires_at IS NOT NULL
        AND consumed_batch_id IS NOT NULL
        AND consumed_at IS NOT NULL
      )
    )
);
CREATE INDEX idx_import_expire ON import_task(expires_at);
CREATE INDEX idx_import_creator_account
    ON import_task(creator_account_id, created_at DESC);
CREATE INDEX idx_import_reservation_expiry
    ON import_task(state, reservation_expires_at)
    WHERE state='reserved';
CREATE INDEX idx_import_parse_due
    ON import_task(parse_status, parse_lease_expires_at, created_at)
    WHERE parse_status IN ('pending','processing');

CREATE TABLE import_phone (
    id             BIGSERIAL PRIMARY KEY,
    import_task_id BIGINT      NOT NULL REFERENCES import_task(id) ON DELETE CASCADE,
    phone_enc      BYTEA       NOT NULL,
    phone_hmac     CHAR(64)    NOT NULL,
    phone_mask     VARCHAR(11) NOT NULL,
    key_version    SMALLINT    NOT NULL DEFAULT 1,
    source_row     INTEGER     NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uk_import_phone UNIQUE (import_task_id, phone_hmac)
);
CREATE INDEX idx_import_phone_task ON import_phone(import_task_id);

-- ─────────────── 审批 ───────────────
CREATE TABLE approval (
    id         BIGSERIAL PRIMARY KEY,
    batch_id   BIGINT      NOT NULL UNIQUE REFERENCES sms_batch(id),
    applicant  VARCHAR(64) NOT NULL,      -- 事件时登录名快照，不参与回避判断
    applicant_account_id BIGINT REFERENCES user_account(id) ON DELETE RESTRICT,
    applicant_identity_id BIGINT,
    dept       VARCHAR(128) NOT NULL,
    trigger_threshold INTEGER,
    trigger_threshold_source VARCHAR(16) NOT NULL DEFAULT 'legacy_unknown',
    status     VARCHAR(10) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','approved','rejected','expired')),
    approver   VARCHAR(64),               -- 事件时登录名快照
    approver_account_id BIGINT REFERENCES user_account(id) ON DELETE RESTRICT,
    approver_identity_id BIGINT,
    reason     VARCHAR(256),
    decided_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_approval_trigger_threshold_source
        CHECK (trigger_threshold_source IN ('snapshot','legacy_unknown')),
    CONSTRAINT chk_approval_trigger_threshold CHECK (
        (trigger_threshold_source='snapshot' AND trigger_threshold > 0)
        OR (trigger_threshold_source='legacy_unknown' AND trigger_threshold IS NULL)
    ),
    CONSTRAINT fk_approval_applicant_identity
      FOREIGN KEY (applicant_identity_id,applicant_account_id)
      REFERENCES auth_identity(id,account_id) ON DELETE RESTRICT,
    CONSTRAINT fk_approval_approver_identity
      FOREIGN KEY (approver_identity_id,approver_account_id)
      REFERENCES auth_identity(id,account_id) ON DELETE RESTRICT,
    CONSTRAINT ck_approval_applicant_principal_pair CHECK (
      (applicant_account_id IS NULL AND applicant_identity_id IS NULL)
      OR (applicant_account_id IS NOT NULL AND applicant_identity_id IS NOT NULL)
    ),
    CONSTRAINT ck_approval_approver_principal_pair CHECK (
      (approver_account_id IS NULL AND approver_identity_id IS NULL)
      OR (approver_account_id IS NOT NULL AND approver_identity_id IS NOT NULL)
    ),
    CONSTRAINT chk_no_self_approve CHECK (
      approver_account_id IS NULL
      OR applicant_account_id IS NULL
      OR approver_account_id <> applicant_account_id
    ),
    CONSTRAINT ck_approval_reason_no_phone CHECK (
      reason IS NULL OR reason !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
    )
);
CREATE INDEX idx_approval_status ON approval(status, created_at);
CREATE INDEX idx_approval_applicant_account
    ON approval(applicant_account_id, created_at DESC);
CREATE INDEX idx_approval_approver_account
    ON approval(approver_account_id, decided_at DESC);

-- ─────────────── 模板与签名 ───────────────
CREATE TABLE sms_template (
    id                   BIGSERIAL PRIMARY KEY,
    name                 VARCHAR(64)  NOT NULL DEFAULT '[encrypted]', -- 固定非敏感标记
    name_enc             BYTEA        NOT NULL,           -- 模板 ID 绑定 AES-GCM
    content              VARCHAR(500) NOT NULL DEFAULT '[encrypted]', -- 固定非敏感标记
    content_enc          BYTEA        NOT NULL,           -- 模板定义：版本头+上下文 AES-GCM
    var_specs            JSONB,                           -- 变量声明,如 [{"pos":1,"max_len":10},{"pos":2,"max_len":6}]
                                                          -- 提交厂商时按序转为 {s<max_len>}；渲染时校验参数长度
    dept                 VARCHAR(128) NOT NULL,
    vendor_template_id   VARCHAR(64),
    vendor_state         VARCHAR(10)  NOT NULL DEFAULT 'draft'
        CHECK (vendor_state IN ('draft','pending','approved','rejected')),
    vendor_reject_reason VARCHAR(256),
    created_by           VARCHAR(64)  NOT NULL,
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_sms_template_content_marker CHECK (content='[encrypted]'),
    CONSTRAINT ck_sms_template_name_marker CHECK (name='[encrypted]'),
    CONSTRAINT ck_sms_template_reject_reason_no_phone CHECK (
      vendor_reject_reason IS NULL
      OR vendor_reject_reason !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
    )
);

CREATE TABLE sms_sign (
    id                   BIGSERIAL PRIMARY KEY,
    name                 VARCHAR(32) NOT NULL UNIQUE,
    vendor_sign_id       VARCHAR(64),
    vendor_state         VARCHAR(10) NOT NULL DEFAULT 'pending'
        CHECK (vendor_state IN ('pending','approved','rejected')),
    vendor_reject_reason VARCHAR(256),
    created_by           VARCHAR(64) NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_sms_sign_reject_reason_no_phone CHECK (
      vendor_reject_reason IS NULL
      OR vendor_reject_reason !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
    )
);

-- ─────────────── 管控 ───────────────
CREATE TABLE blacklist (
    phone_hmac  CHAR(64)    PRIMARY KEY,
    phone_enc   BYTEA       NOT NULL,
    phone_mask  VARCHAR(11) NOT NULL,
    key_version SMALLINT    NOT NULL DEFAULT 1,
    source      VARCHAR(16) NOT NULL DEFAULT 'manual'
        CHECK (source IN ('manual','reply_optout','import')),
    remark      VARCHAR(128),
    created_by  VARCHAR(64) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_blacklist_remark_no_phone CHECK (
      remark IS NULL OR remark !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
    )
);

CREATE TABLE blacklist_hmac_alias (
    blacklist_digest CHAR(64) NOT NULL,
    hmac_key_version     SMALLINT NOT NULL
        CONSTRAINT ck_blacklist_hmac_alias_version
        CHECK (hmac_key_version BETWEEN 1 AND 32767),
    hmac_digest          CHAR(64) NOT NULL
        CONSTRAINT ck_blacklist_hmac_alias_digest
        CHECK (hmac_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT pk_blacklist_hmac_alias
      PRIMARY KEY (hmac_key_version,hmac_digest),
    CONSTRAINT uq_blacklist_hmac_alias_owner_version
      UNIQUE (blacklist_digest,hmac_key_version)
);
ALTER TABLE blacklist_hmac_alias
  ADD CONSTRAINT fk_blacklist_hmac_alias_owner
  FOREIGN KEY(blacklist_digest) REFERENCES blacklist(phone_hmac)
  ON UPDATE CASCADE ON DELETE CASCADE;

-- 历史自由文本命中手机号时，迁移先加密保存原值，再收敛在线字段。
-- 该表不授予任何运行角色，只允许 sms_owner 受控恢复。
CREATE TABLE sensitive_metadata_archive (
    source_table  VARCHAR(32)  NOT NULL,
    source_row    VARCHAR(128) NOT NULL,
    source_column VARCHAR(32)  NOT NULL,
    value_enc     BYTEA       NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_table, source_row, source_column),
    CONSTRAINT ck_sensitive_metadata_archive_source CHECK (
      (source_table='sms_batch' AND source_column='remark')
      OR (source_table='blacklist' AND source_column='remark')
      OR (source_table='approval' AND source_column='reason')
      OR (source_table='sms_template' AND source_column='vendor_reject_reason')
      OR (source_table='sms_sign' AND source_column='vendor_reject_reason')
    )
);
REVOKE ALL ON sensitive_metadata_archive FROM PUBLIC;

CREATE TABLE sensitive_word (
    id         BIGSERIAL PRIMARY KEY,
    word       VARCHAR(64) NOT NULL UNIQUE,
    created_by VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─────────────── 结果回调任务 ───────────────
CREATE TABLE callback_report_event (
    event_key          CHAR(64) PRIMARY KEY,
    batch_id           BIGINT NOT NULL REFERENCES sms_batch(id),
    message_id         BIGINT NOT NULL,
    message_created_at TIMESTAMPTZ NOT NULL,
    message_status     VARCHAR(10) NOT NULL,
    report_desc        VARCHAR(128) NOT NULL DEFAULT '',
    report_time        TIMESTAMPTZ NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_cb_report_event_batch ON callback_report_event(batch_id,created_at);
CREATE INDEX idx_cb_report_event_created ON callback_report_event(created_at);

CREATE TABLE callback_task (
    id            BIGSERIAL PRIMARY KEY,
    event_id      UUID NOT NULL DEFAULT gen_random_uuid(),
    correlation_id UUID NOT NULL DEFAULT gen_random_uuid(),
    app_id        BIGINT      NOT NULL REFERENCES app(id),
    event         VARCHAR(24) NOT NULL,   -- batch.finished / message.report
    batch_id      BIGINT,
    source_report_event_key CHAR(64),
    message_ids   BIGINT[]    NOT NULL DEFAULT '{}', -- message.report引用，严禁phone/body
    message_times TIMESTAMPTZ[] NOT NULL DEFAULT '{}', -- 消息分区created_at，与message_ids复合定位
    event_keys    CHAR(64)[] NOT NULL DEFAULT '{}', -- 无PII报告事件SHA-256，与message_ids等长
    url           VARCHAR(256) NOT NULL,
    callback_secret_enc BYTEA NOT NULL, -- 创建时固化；仅当前配置仍匹配时允许重试
    callback_secret_key_version SMALLINT NOT NULL
      CONSTRAINT chk_callback_secret_key_version CHECK (callback_secret_key_version > 0),
    signature_version SMALLINT NOT NULL DEFAULT 1
      CONSTRAINT chk_callback_signature_version CHECK (signature_version = 1),
    status        VARCHAR(10) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','retrying','done','dead')),
    retry_count   SMALLINT    NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ,
    lease_id      UUID,
    lease_expires_at TIMESTAMPTZ,
    takeover_count INTEGER NOT NULL DEFAULT 0,
    last_http_code INTEGER,
    last_error    VARCHAR(256),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    CONSTRAINT chk_cb_message_refs CHECK (
      cardinality(message_ids) = cardinality(message_times)
      AND cardinality(message_ids) = cardinality(event_keys)
    ),
    CONSTRAINT chk_cb_lease_pair CHECK (
      (lease_id IS NULL AND lease_expires_at IS NULL)
      OR (lease_id IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CONSTRAINT chk_cb_lease_state CHECK (
      lease_id IS NULL OR status='retrying'
    ),
    CONSTRAINT chk_cb_takeover_count CHECK (
      takeover_count>=0
    ),
    CONSTRAINT fk_cb_source_report_event
      FOREIGN KEY(source_report_event_key)
      REFERENCES report_event(event_key) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX uk_callback_task_event_id ON callback_task(event_id);
CREATE INDEX idx_callback_correlation ON callback_task(correlation_id,created_at);
CREATE UNIQUE INDEX uk_cb_batch_source_event
    ON callback_task(batch_id,source_report_event_key)
    WHERE event='batch.finished' AND source_report_event_key IS NOT NULL;
CREATE INDEX idx_cb_pending ON callback_task(status, next_retry_at)
    WHERE status IN ('pending','retrying');
CREATE INDEX idx_cb_lease_expiry ON callback_task(lease_expires_at,id)
    WHERE lease_id IS NOT NULL;
CREATE INDEX idx_cb_event_keys ON callback_task USING GIN(event_keys);

CREATE TABLE callback_authority_lease (
    app_id      BIGINT PRIMARY KEY REFERENCES app(id) ON DELETE CASCADE,
    task_id     BIGINT NOT NULL UNIQUE REFERENCES callback_task(id) ON DELETE CASCADE,
    lease_id    UUID NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
REVOKE ALL ON callback_authority_lease FROM PUBLIC;

-- 回调配置撤销与旧任务隔离必须和 app 更新处于同一数据库事务；
-- SECURITY DEFINER 只由触发器调用，避免给 sms_accept callback_task UPDATE 权限。
CREATE OR REPLACE FUNCTION revoke_callback_tasks_on_app_change()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
  IF OLD.callback_url IS DISTINCT FROM NEW.callback_url
     OR OLD.callback_secret_enc IS DISTINCT FROM NEW.callback_secret_enc
     OR (
       OLD.callback_report_enabled=true
       AND NEW.callback_report_enabled=false
     )
     OR (OLD.status=1 AND NEW.status<>1)
  THEN
    UPDATE public.callback_task SET status='dead',retry_count=0,
      next_retry_at=NULL,lease_id=NULL,lease_expires_at=NULL,
      last_http_code=NULL,last_error='CallbackConfigRevoked',
      finished_at=now()
    WHERE app_id=NEW.id AND status IN ('pending','retrying')
      AND (
        OLD.callback_url IS DISTINCT FROM NEW.callback_url
        OR OLD.callback_secret_enc IS DISTINCT FROM NEW.callback_secret_enc
        OR (OLD.status=1 AND NEW.status<>1)
        OR (
          OLD.callback_report_enabled=true
          AND NEW.callback_report_enabled=false
          AND event='message.report'
        )
      );
  END IF;
  RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION revoke_callback_tasks_on_app_change() FROM PUBLIC;

CREATE TRIGGER trg_app_revoke_callback_tasks
AFTER UPDATE OF callback_url,callback_secret_enc,
  callback_report_enabled,status ON app
FOR EACH ROW EXECUTE FUNCTION revoke_callback_tasks_on_app_change();

-- callback/export worker 的无 PII 租约事实；只追加，用于停滞/接管/CAS 观测。
CREATE TABLE worker_lease_event (
    id          BIGSERIAL PRIMARY KEY,
    task_kind   VARCHAR(16) NOT NULL
                CHECK (task_kind IN ('callback','export')),
    task_id     BIGINT NOT NULL CHECK (task_id>0),
    event_type  VARCHAR(24) NOT NULL
                CHECK (event_type IN (
                  'acquired','takeover','heartbeat_lost',
                  'fencing_miss','dead','manual_retry'
                )),
    lease_id    UUID,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_worker_lease_event_metrics
    ON worker_lease_event(task_kind,event_type,created_at);

-- ─────────────── 余额 / 告警 ───────────────
CREATE TABLE balance_snapshot (
    id         BIGSERIAL PRIMARY KEY,
    balance    INTEGER     NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_balance_time ON balance_snapshot(fetched_at);

CREATE TABLE alert_log (
    id         BIGSERIAL PRIMARY KEY,
    alert_type VARCHAR(32) NOT NULL,
    level      VARCHAR(8)  NOT NULL DEFAULT 'warn' CHECK (level IN ('info','warn','crit')),
    title      VARCHAR(128) NOT NULL,
    detail     JSONB,       -- 禁止phone/mobile/mobiles及任何号码列表
    channels   VARCHAR(64) NOT NULL,
    dedup_key  VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_alert_dedup ON alert_log(dedup_key, created_at);

-- ─────────────── 事务性 Outbox ───────────────
-- args 只能保存稳定引用和无 PII 元数据；Celery task_id 固定为 id。
CREATE TABLE outbox_event (
    id             UUID PRIMARY KEY,
    correlation_id UUID NOT NULL DEFAULT gen_random_uuid(),
    dedup_key      VARCHAR(192) NOT NULL UNIQUE,
    event_type     VARCHAR(64)  NOT NULL,
    aggregate_type VARCHAR(32)  NOT NULL,
    aggregate_id   VARCHAR(128) NOT NULL,
    task_name      VARCHAR(128) NOT NULL
                   CONSTRAINT ck_outbox_task_name
                   CHECK (task_name IN (
                     'app.tasks.bind_sign',
                     'app.tasks.bind_template',
                     'app.tasks.sync_template',
                     'app.tasks.send.process_batch',
                     'app.tasks.deliver_callback',
                     'app.tasks.outbox.compensate_quota',
                     'app.tasks.outbox.deliver_alert',
                     'app.tasks.outbox.release_usage',
                     'app.tasks.outbox.trigger_job'
                   )),
    queue          VARCHAR(16)  NOT NULL
                   CHECK (queue IN ('realtime','bulk','callback')),
    args           JSONB        NOT NULL DEFAULT '[]'::jsonb
                   CHECK (jsonb_typeof(args)='array')
                   CONSTRAINT ck_outbox_args_scalar_refs CHECK (
                     NOT (
                       args @? '$[*] ? (
                         @.type() != "string" && @.type() != "number"
                       )'
                     )
                   ),
    state          VARCHAR(16)  NOT NULL DEFAULT 'pending'
                   CHECK (state IN (
                     'pending','leased','published','processing','completed','dead'
                   )),
    attempts       SMALLINT     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    failure_count  INTEGER      NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    max_attempts   SMALLINT     NOT NULL DEFAULT 12
                   CHECK (max_attempts BETWEEN 1 AND 100),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_id       UUID,
    lease_expires_at TIMESTAMPTZ,
    published_at   TIMESTAMPTZ,
    completed_at   TIMESTAMPTZ,
    last_error     VARCHAR(64),
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_outbox_lease_pair CHECK (
      (lease_id IS NULL AND lease_expires_at IS NULL)
      OR (lease_id IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CONSTRAINT ck_outbox_args_no_pii CHECK (
      (
        args::text !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
        OR (
          task_name IN (
            'app.tasks.bind_sign',
            'app.tasks.bind_template',
            'app.tasks.sync_template'
          )
          AND jsonb_array_length(args)=1
          AND jsonb_typeof(args->0)='number'
          AND args->>0 ~ '^[1-9][0-9]*$'
        )
        OR (
          task_name='app.tasks.send.process_batch'
          AND jsonb_array_length(args)=1
          AND jsonb_typeof(args->0)='string'
          AND args->>0 ~ '^[0-9a-f]{32}$'
        )
        OR (
          task_name='app.tasks.deliver_callback'
          AND jsonb_array_length(args)=1
          AND jsonb_typeof(args->0)='number'
          AND args->>0 ~ '^[0-9]+$'
        )
        OR (
          task_name='app.tasks.outbox.deliver_alert'
          AND jsonb_array_length(args)=2
          AND jsonb_typeof(args->0)='number'
          AND args->>0 ~ '^[0-9]+$'
          AND args->>1 IN ('wecom','smtp')
        )
        OR (
          task_name='app.tasks.outbox.compensate_quota'
          AND jsonb_array_length(args)=6
          AND jsonb_typeof(args->0)='number'
          AND args->>0 ~ '^[0-9]+$'
          AND jsonb_typeof(args->1)='string'
          AND args->>1 !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
          AND args->>2 IN ('verify','notice','market')
          AND args->>3 ~ '^[0-9]{8}$'
          AND jsonb_typeof(args->4)='number'
          AND args->>4 ~ '^[0-9]+$'
          AND args->>5 ~ (
            '^(batch[:][0-9a-f]{32}[:]cancelled|'
            || 'approval[:][1-9][0-9]*[:](rejected|expired))$'
          )
        )
        OR (
          task_name='app.tasks.outbox.release_usage'
          AND jsonb_array_length(args)=1
          AND jsonb_typeof(args->0)='string'
          AND args->>0 ~ (
            '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-'
            || '[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
          )
        )
      )
      AND args::text !~ (
        '"(phone|phones|mobile|mobiles|phone_enc|phone_hmac|'
        || 'content|body|secret|password)"[[:space:]]*:'
      )
    ),
    CONSTRAINT ck_outbox_refs_no_pii CHECK (
      (
        aggregate_id !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
        OR (
          task_name IN (
            'app.tasks.bind_sign',
            'app.tasks.bind_template',
            'app.tasks.sync_template'
          )
          AND aggregate_id ~ '^[1-9][0-9]*$'
        )
        OR (
          task_name='app.tasks.send.process_batch'
          AND aggregate_id ~ '^[0-9a-f]{32}$'
        )
        OR (
          task_name='app.tasks.outbox.compensate_quota'
          AND aggregate_id ~ '^([0-9a-f]{32}|[0-9]+)$'
        )
        OR (
          task_name IN (
            'app.tasks.deliver_callback',
            'app.tasks.outbox.deliver_alert'
          )
          AND aggregate_id ~ '^[0-9]+$'
        )
        OR (
          task_name='app.tasks.outbox.release_usage'
          AND aggregate_id ~ (
            '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-'
            || '[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
          )
        )
      )
      AND (
        dedup_key !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
        OR dedup_key ~ (
          '^(batch[.]ready[:][0-9a-f]{32}|'
          || 'scheduled[:][0-9a-f]{32}[:]ready|'
          || 'batch[:][0-9a-f]{32}[:]cancelled|'
          || 'approval[:][1-9][0-9]*[:](approved|rejected|expired)|'
          || 'callback[:][1-9][0-9]*[:]attempt[:][0-9]+|'
          || 'alert[:][1-9][0-9]*[:](wecom|smtp)|'
          || 'template[.]sync[:][1-9][0-9]*[:][1-9][0-9]*|'
          || '(template[.]bind|sign[.]bind)[:][1-9][0-9]*[:]'
          || '[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-'
          || '[89ab][0-9a-f]{3}-[0-9a-f]{12}|'
          || 'usage[.]release[:][0-9a-f]{8}-[0-9a-f]{4}-'
          || '[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-'
          || '[0-9a-f]{12})$'
        )
      )
    )
);
CREATE INDEX idx_outbox_correlation ON outbox_event(correlation_id,created_at);
CREATE INDEX idx_outbox_dispatch_due
    ON outbox_event(next_attempt_at,created_at)
    WHERE state IN ('pending','leased','published');
CREATE INDEX idx_outbox_processing_lease
    ON outbox_event(lease_expires_at)
    WHERE state='processing';
CREATE INDEX idx_outbox_backlog
    ON outbox_event(state,created_at)
    WHERE state<>'completed';

-- ─────────────── 配额/频控事实账本 ───────────────
-- PostgreSQL 是唯一事实源；Redis 只保存带版本的绝对值投影。
CREATE SEQUENCE usage_projection_version_seq;

CREATE TABLE usage_reservation (
    id              UUID PRIMARY KEY,
    request_key     VARCHAR(192) NOT NULL,
    app_id          BIGINT NOT NULL CHECK (app_id>=0),
    dept            VARCHAR(128) NOT NULL,
    category        VARCHAR(8) NOT NULL
                    CHECK (category IN ('verify','notice','market')),
    usage_date      DATE NOT NULL,
    state           VARCHAR(20) NOT NULL DEFAULT 'reserved'
                    CHECK (state IN (
                      'reserved','committed','release_requested',
                      'released','uncertain'
                    )),
    quota_cost      INTEGER NOT NULL DEFAULT 0 CHECK (quota_cost>=0),
    app_limit       INTEGER NOT NULL DEFAULT 0 CHECK (app_limit>=0),
    dept_limit      INTEGER NOT NULL DEFAULT 0 CHECK (dept_limit>=0),
    release_event_id VARCHAR(192) UNIQUE,
    last_error      VARCHAR(64),
    reserved_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    committed_at    TIMESTAMPTZ,
    release_requested_at TIMESTAMPTZ,
    released_at     TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_usage_request_key_no_pii CHECK (
      request_key ~ (
        '^(acceptance[:](v2[:][0-9a-f]{64}[:][0-9]{8}|'
        ||'[0-9]+[:][0-9a-f]{64}[:][0-9]{8}|'
        ||'[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-'
        ||'[89ab][0-9a-f]{3}-[0-9a-f]{12})|'
        ||'legacy[:]batch[:][0-9a-f]{32})$'
      )
    ),
    CONSTRAINT ck_usage_release_event_no_pii CHECK (
      release_event_id IS NULL
      OR release_event_id ~ (
        '^(batch[:][0-9a-f]{32}[:]cancelled|'
        ||'approval[:][1-9][0-9]*[:](rejected|expired)|usage[:]'
        ||'[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-'
        ||'[89ab][0-9a-f]{3}-[0-9a-f]{12}[:]'
        ||'(acceptance-failed|all-filtered|idempotent-reuse|'
        ||'orphan-recovery))$'
      )
    )
);
CREATE UNIQUE INDEX uk_usage_reservation_active_request
    ON usage_reservation(request_key) WHERE state<>'released';
CREATE INDEX idx_usage_reservation_recovery
    ON usage_reservation(updated_at)
    WHERE state IN ('reserved','uncertain','release_requested');
CREATE INDEX idx_usage_reservation_retention
    ON usage_reservation(usage_date,state);

CREATE TABLE usage_frequency_subject (
    id              UUID PRIMARY KEY,
    projection_hmac CHAR(64) NOT NULL UNIQUE
                    CHECK (projection_hmac ~ '^[0-9a-f]{64}$'),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE usage_frequency_alias (
    subject_id  UUID NOT NULL
                REFERENCES usage_frequency_subject(id) ON DELETE CASCADE,
    key_version SMALLINT NOT NULL CHECK (key_version>0),
    phone_hmac  CHAR(64) NOT NULL UNIQUE
                CHECK (phone_hmac ~ '^[0-9a-f]{64}$'),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(subject_id,key_version)
);

CREATE TABLE usage_quota_entry (
    reservation_id UUID NOT NULL
                   REFERENCES usage_reservation(id) ON DELETE CASCADE,
    dimension_kind VARCHAR(8) NOT NULL
                   CHECK (dimension_kind IN ('app','dept','volume')),
    dimension_value VARCHAR(160) NOT NULL,
    usage_date     DATE NOT NULL,
    amount         INTEGER NOT NULL CHECK (amount>=0),
    projection_key VARCHAR(256) NOT NULL,
    expires_at     TIMESTAMPTZ NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(reservation_id,dimension_kind),
    CONSTRAINT ck_usage_quota_projection_key CHECK (
      projection_key ~ '^quota:(app|dept|volume:app):'
    )
);
CREATE INDEX idx_usage_quota_dimension
    ON usage_quota_entry(projection_key,usage_date);

CREATE TABLE usage_frequency_entry (
    reservation_id UUID NOT NULL
                   REFERENCES usage_reservation(id) ON DELETE CASCADE,
    subject_id     UUID NOT NULL
                   REFERENCES usage_frequency_subject(id) ON DELETE RESTRICT,
    app_id         BIGINT,
    category       VARCHAR(8) NOT NULL CHECK (category IN ('verify','market')),
    window_kind    VARCHAR(8) NOT NULL CHECK (window_kind IN ('minute','day')),
    window_key     VARCHAR(16) NOT NULL,
    usage_date     DATE NOT NULL,
    projection_key VARCHAR(256) NOT NULL,
    counted        BOOLEAN NOT NULL,
    expires_at     TIMESTAMPTZ NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(reservation_id,subject_id,window_kind),
    CONSTRAINT ck_usage_frequency_scope CHECK (
      (category='verify' AND app_id IS NULL)
      OR (category='market' AND app_id IS NOT NULL AND window_kind='day')
    ),
    CONSTRAINT ck_usage_frequency_projection_key CHECK (
      projection_key ~ '^freq:(v|m):[0-9a-f:]+'
    )
);
CREATE INDEX idx_usage_frequency_dimension
    ON usage_frequency_entry(projection_key,usage_date) WHERE counted;
CREATE INDEX idx_usage_frequency_subject_window
    ON usage_frequency_entry(
      subject_id,category,app_id,window_kind,window_key
    ) WHERE counted;

CREATE TABLE usage_projection (
    dimension_key VARCHAR(256) PRIMARY KEY,
    kind          VARCHAR(16) NOT NULL CHECK (kind IN ('quota','frequency')),
    usage_date    DATE NOT NULL,
    window_key    VARCHAR(24) NOT NULL,
    value         BIGINT NOT NULL DEFAULT 0 CHECK (value>=0),
    version       BIGINT NOT NULL DEFAULT nextval('usage_projection_version_seq')
                  CHECK (version>0),
    expires_at    TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uk_usage_projection_version UNIQUE (version)
);
CREATE INDEX idx_usage_projection_rebuild
    ON usage_projection(usage_date,expires_at);

CREATE TABLE usage_projection_drift (
    kind                  VARCHAR(16) PRIMARY KEY
                          CHECK (kind IN ('quota','frequency')),
    mismatched_dimensions INTEGER NOT NULL DEFAULT 0
                          CHECK (mismatched_dimensions>=0),
    absolute_delta        BIGINT NOT NULL DEFAULT 0 CHECK (absolute_delta>=0),
    checked_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO usage_projection_drift(kind) VALUES('quota'),('frequency');

ALTER TABLE sms_batch ADD COLUMN usage_reservation_id UUID
    REFERENCES usage_reservation(id) ON DELETE SET NULL;
CREATE UNIQUE INDEX uk_sms_batch_usage_reservation
    ON sms_batch(usage_reservation_id)
    WHERE usage_reservation_id IS NOT NULL;

-- ─────────────── 统计 ───────────────
CREATE TABLE stat_daily (
    stat_date   DATE         NOT NULL,
    dim_type    VARCHAR(8)   NOT NULL CHECK (dim_type IN ('app','dept','all')),
    dim_value   VARCHAR(128) NOT NULL DEFAULT '',
    category    VARCHAR(8)   NOT NULL DEFAULT 'all'
                CHECK (category IN ('verify','notice','market','all')),
    total       INTEGER      NOT NULL DEFAULT 0,
    total_segments INTEGER   NOT NULL DEFAULT 0,   -- 计费条汇总(与厂商账单对账)
    delivered   INTEGER      NOT NULL DEFAULT 0,
    failed      INTEGER      NOT NULL DEFAULT 0,
    unknown_cnt INTEGER      NOT NULL DEFAULT 0,
    PRIMARY KEY (stat_date, dim_type, dim_value, category)
);

-- ─────────────── 服务器安全日报 ───────────────
-- payload 只允许 render_security_daily_report.py 契约定义的脱敏 JSON；
-- Resend Key 与收件地址由安全日报管理员页面维护，独立 mailer 只读取同步配置。
-- v1.6.43：日报记录逐条保留；自动路径每天仅一条（部分唯一索引），
-- 手动“立即生成”每次新增记录，历史记录不覆盖。
CREATE TABLE security_daily_report (
    id                BIGSERIAL PRIMARY KEY,
    report_date       DATE NOT NULL,
    period_start      TIMESTAMPTZ NOT NULL,
    period_end        TIMESTAMPTZ NOT NULL,
    status             VARCHAR(16) NOT NULL
                      CHECK (status IN ('normal','attention','high')),
    generation_status  VARCHAR(16) NOT NULL DEFAULT 'unavailable'
                      CHECK (generation_status IN ('pending','ready','failed','unavailable')),
    generation_source  VARCHAR(8)  NOT NULL DEFAULT 'auto'
                      CHECK (generation_source IN ('auto','manual')),
    delivery_status    VARCHAR(16) NOT NULL DEFAULT 'not_sent'
                      CHECK (delivery_status IN ('not_sent','pending','sending','sent','failed')),
    payload            JSONB,
    generated_at       TIMESTAMPTZ,
    delivered_at       TIMESTAMPTZ,
    recipient_count    SMALLINT NOT NULL DEFAULT 0
                      CHECK (recipient_count BETWEEN 0 AND 3),
    retry_count        SMALLINT NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    last_error         VARCHAR(256),
    last_error_at      TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_security_daily_period CHECK (period_start < period_end),
    CONSTRAINT ck_security_daily_payload_ready CHECK (
      generation_status <> 'ready' OR payload IS NOT NULL
    )
);
CREATE INDEX idx_security_daily_report_status
    ON security_daily_report(status,report_date DESC);
CREATE INDEX idx_security_daily_report_delivery
    ON security_daily_report(delivery_status,report_date DESC);
CREATE UNIQUE INDEX security_daily_report_report_date_key
    ON security_daily_report(report_date)
    WHERE generation_source='auto';

CREATE TABLE security_daily_delivery_request (
    request_id       UUID PRIMARY KEY,
    report_id        BIGINT NOT NULL
                     REFERENCES security_daily_report(id) ON DELETE RESTRICT,
    report_date      DATE NOT NULL,
    action           VARCHAR(8) NOT NULL CHECK (action IN ('send','retry')),
    state            VARCHAR(8) NOT NULL DEFAULT 'pending'
                     CHECK (state IN ('pending','sent','failed')),
    dedup_key        VARCHAR(192) NOT NULL UNIQUE,
    requested_by     VARCHAR(64) NOT NULL,
    config_version   BIGINT NOT NULL,
    requested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at     TIMESTAMPTZ,
    error            VARCHAR(256),
    CONSTRAINT ck_security_daily_request_config_version CHECK (config_version > 0),
    CONSTRAINT ck_security_daily_request_completion CHECK (
      (state='pending' AND completed_at IS NULL)
      OR (state IN ('sent','failed') AND completed_at IS NOT NULL)
    )
);
CREATE INDEX idx_security_daily_request_pending
    ON security_daily_delivery_request(state,requested_at)
    WHERE state='pending';
CREATE INDEX idx_security_daily_request_report
    ON security_daily_delivery_request(report_id,requested_at DESC);

CREATE TABLE security_daily_recipient (
    position    SMALLINT PRIMARY KEY CHECK (position BETWEEN 1 AND 3),
    address     VARCHAR(254) NOT NULL CHECK (address <> ''),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_security_daily_recipient_address
    ON security_daily_recipient (lower(address));

-- ─────────────── 系统配置 / 审计 ───────────────
CREATE TABLE sys_config (
    key         VARCHAR(64) PRIMARY KEY,
    value       VARCHAR(512) NOT NULL,
    value_type  VARCHAR(8)  NOT NULL DEFAULT 'str' CHECK (value_type IN ('str','int','bool','json')),
    description VARCHAR(256),
    updated_by  VARCHAR(64),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO sys_config (key, value, value_type, description) VALUES
('approval_threshold',        '100',   'int',  'Web通知类触发审批的号码数阈值'),
('market_approval_threshold', '50',    'int',  'Web营销类触发审批的号码数阈值(独立)'),
('approval_expire_hours',     '24',    'int',  '审批单过期时长(小时)'),
('market_send_window',        '08:00-21:00', 'str', '营销类发送时间窗,窗外自动转次日窗口起点定时'),
('vendor_batch_size',         '500',   'int',  '单次厂商Send的号码分片大小'),
('vendor_qps',                '5',     'int',  '对厂商的全局调用QPS上限'),
('reserved_realtime_qps',     '2',     'int',  '为realtime队列(验证码/通知)预留的QPS'),
('report_poll_seconds',       '60',    'int',  'GetReport轮询间隔(秒)'),
('reply_poll_seconds',        '300',   'int',  'GetReply轮询间隔(秒)'),
('balance_poll_seconds',      '600',   'int',  'GetBalance巡检间隔(秒)'),
('balance_alert_threshold',   '10000', 'int',  '余额低告警阈值(条)'),
('fail_rate_threshold',       '20',    'int',  '批次失败率告警阈值(%)'),
('fail_rate_min_total',       '50',    'int',  '失败率告警的最小批量'),
('report_timeout_hours',      '48',    'int',  '无报告置unknown的时长(小时)'),
('uncertain_alert_hours',     '24',    'int',  'uncertain分片转人工告警时长(小时)'),
('reconcile_interval_min',    '5',     'int',  '对账任务间隔(分钟)'),
('msg_retention_months',      '12',    'int',  '消息/回复保留月数'),
('audit_retention_months',    '36',    'int',  '审计日志保留月数'),
('raw_log_retention_days',    '90',    'int',  '厂商原始报文保留天数'),
('import_expire_hours',       '24',    'int',  '导入任务与号码包有效期(小时)'),
('export_retention_days',     '7',     'int',  '导出文件保留天数'),
('sensitive_hit_action',      'block', 'str',  '敏感词命中策略: block/audit'),
('key_grace_hours',           '72',    'int',  'APIKey轮换旧Key宽限期(小时)'),
('login_fail_limit',          '5',     'int',  '同账号15分钟内失败次数上限'),
('login_lock_minutes',        '15',    'int',  '账号锁定时长(分钟)'),
('login_ip_fail_limit',       '20',    'int',  '同IP5分钟内失败次数上限'),
('login_ip_ban_minutes',      '15',    'int',  'IP封禁时长(分钟)'),
('callback_timeout_seconds',  '5',     'int',  '回调HTTP超时(秒)'),
('callback_retry_schedule',   '60,300,900,3600,3600', 'str', '回调重试间隔(秒,逗号分隔)'),
('callback_allow_cidrs',      '10.0.0.0/8,172.16.0.0/12,192.168.0.0/16', 'str', '回调地址内网白名单'),
('alert_wecom_webhook',       '',      'str',  '企业微信机器人Webhook'),
('alert_mail_to',             '',      'str',  '告警邮件收件人,逗号分隔'),
('alert_smtp_host',           'smtp',  'str',  '告警邮件内网relay主机'),
('alert_smtp_port',           '25',    'int',  '告警邮件内网relay端口'),
('alert_mail_from',           'sms-platform@localhost', 'str', '告警邮件发件人'),
-- v1.2 新增
('unsubscribe_suffix',        '回T退订', 'str', '营销退订语'),
('unsubscribe_auto_append',   'true',  'bool', '营销内容缺退订语时自动追加'),
('verify_freq_per_minute',    '1',     'int',  '验证码同号码每分钟条数上限(全局跨应用)'),
('verify_freq_per_day',       '10',    'int',  '验证码同号码每日条数上限(全局跨应用)'),
('market_freq_per_day',       '1',     'int',  '营销同号码同应用每日条数上限'),
('import_max_mb',             '10',    'int',  'Web导入单文件大小上限(MB)'),
('import_max_rows',           '50000', 'int',  'Web导入号码数上限'),
('test_send_max',             '5',     'int',  '测试发送号码数上限'),
-- v1.4 新增
('verify_otp_mask',           'true',  'bool', 'verify类内容入库前对4-8位连续数字等长打码'),
('unmatched_retention_days',  '90',    'int',  '无主报告保留天数(并行迁移期对账)'),
-- v1.5 新增
('anomaly_enabled',           'true',  'bool', '发送量异常检测开关'),
('anomaly_multiplier',        '3',     'int',  '异常倍数阈值(当日量>基线×N)'),
('anomaly_min_total',         '500',   'int',  '异常检测最小绝对量(双条件防小基数误报)'),
('anomaly_scan_minutes',      '60',    'int',  '异常扫描间隔(分钟)'),
('job_history_days',          '30',    'int',  'job_run 任务运行记录保留天数'),
-- v1.6.27 新增
('usage_projection_reconcile_seconds','300','int','配额/频控投影漂移巡检间隔(重启beat生效)'),
('usage_ledger_retention_days','90',   'int',  '已过期配额/频控事实账本保留天数'),
-- v1.6.44 新增
('approval_scan_seconds',   '300',   'int',  '审批过期扫描间隔(秒,重启beat生效)'),
('scheduled_scan_seconds',  '60',    'int',  '定时批次扫描间隔(秒,重启beat生效)'),
-- v1.6.39 新增
('security_daily_enabled','false','bool','服务器安全日报生成与手动投递开关'),
('security_daily_recipient_count','0','int','独立 mailer 当前收件人数，仅保存数量'),
('security_daily_resend_configured','false','bool','独立 mailer Resend Key 与收件人配置状态'),
('security_daily_config_version','1','int','安全日报发信配置单调版本'),
-- v1.6.40 新增：安全日报配置页允许管理员维护 Resend Key。
('security_daily_resend_api_key','','str','安全日报 Resend API Key（管理员配置页）');

-- v1.6.49：API 只持公钥封装，只有 callback worker 持私钥解封。
ALTER TABLE sys_config ADD CONSTRAINT ck_sys_config_wecom_ciphertext
CHECK (
  key<>'alert_wecom_webhook'
  OR btrim(value)=''
  OR value LIKE 'sealed:v1:%'
);

-- v1.6.46：两类 reusable notification credential 不再随 sys_config 全表权限横向暴露。
ALTER TABLE sys_config ENABLE ROW LEVEL SECURITY;
CREATE POLICY sys_config_accept_all ON sys_config
    FOR ALL TO sms_accept USING (true) WITH CHECK (true);
CREATE POLICY sys_config_callback_select ON sys_config
    FOR SELECT TO sms_callback
    USING (key <> 'security_daily_resend_api_key');
CREATE POLICY sys_config_nonsecret_select ON sys_config
    FOR SELECT TO sms_auth, sms_send, sms_export, sms_scheduler
    USING (key NOT IN ('alert_wecom_webhook','security_daily_resend_api_key'));

CREATE FUNCTION alert_channel_availability()
RETURNS TABLE(wecom_configured boolean, smtp_configured boolean)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT
    EXISTS(
      SELECT 1 FROM public.sys_config
      WHERE key='alert_wecom_webhook' AND btrim(value)<>''
    ),
    EXISTS(
      SELECT 1 FROM public.sys_config
      WHERE key='alert_mail_to' AND btrim(value)<>''
    )
$$;
REVOKE ALL ON FUNCTION alert_channel_availability() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION alert_channel_availability() TO
    sms_auth, sms_accept, sms_send, sms_callback, sms_export, sms_scheduler;

CREATE TABLE audit_log (
    id          BIGSERIAL PRIMARY KEY,
    correlation_id UUID NOT NULL DEFAULT COALESCE(
                NULLIF(current_setting('sms.correlation_id', TRUE),'')::uuid,
                gen_random_uuid()
              ),
    actor       VARCHAR(64)  NOT NULL,
    actor_subject_kind VARCHAR(16) NOT NULL DEFAULT 'legacy_unknown'
                CHECK (actor_subject_kind IN ('human','api_app','system','legacy_unknown')),
    actor_account_id BIGINT REFERENCES user_account(id) ON DELETE RESTRICT,
    actor_identity_id BIGINT,
    actor_app_id BIGINT REFERENCES app(id) ON DELETE RESTRICT,
    role        VARCHAR(16),
    ip          INET,
    action      VARCHAR(48)  NOT NULL,
    object_type VARCHAR(32),
    object_id   VARCHAR(64),
    before_val  JSONB,      -- 禁止任何手机号（明文/密文/HMAC）列表
    after_val   JSONB,      -- 只允许号码数量与batch_no引用
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT fk_audit_actor_identity
      FOREIGN KEY (actor_identity_id,actor_account_id)
      REFERENCES auth_identity(id,account_id) ON DELETE RESTRICT,
    CONSTRAINT ck_audit_actor_subject CHECK (
      (
        actor_subject_kind='human'
        AND actor_account_id IS NOT NULL
        AND actor_app_id IS NULL
      )
      OR (
        actor_subject_kind='api_app'
        AND actor_account_id IS NULL
        AND actor_identity_id IS NULL
        AND actor_app_id IS NOT NULL
      )
      OR (
        actor_subject_kind IN ('system','legacy_unknown')
        AND actor_account_id IS NULL
        AND actor_identity_id IS NULL
        AND actor_app_id IS NULL
      )
    ),
    CONSTRAINT ck_audit_payload_no_pii CHECK (
      (COALESCE((before_val - 'batch_no')::text,'') ||
       COALESCE((after_val - 'batch_no')::text,''))
        !~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
      AND (COALESCE(before_val::text,'') || COALESCE(after_val::text,''))
        !~* '"(phones?|mobiles?|phone_enc|phone_hmac|phone_list|mobile_list|'
             '[^"]*_(enc|hmac)|[^"]*token[^"]*|[^"]*secret[^"]*|'
             '[^"]*password[^"]*|body|request|request_body|content|'
             'ciphertext|encrypted(_list)?)"[[:space:]]*:'
    )
);
CREATE INDEX idx_audit_actor  ON audit_log(actor, created_at);
CREATE INDEX idx_audit_actor_account ON audit_log(actor_account_id, created_at DESC);
CREATE INDEX idx_audit_correlation ON audit_log(correlation_id, created_at DESC);
CREATE INDEX idx_audit_action ON audit_log(action, created_at);
CREATE INDEX idx_audit_time   ON audit_log(created_at);

-- 仅 sms_owner 可读；业务角色只提交绑定 txid/session_user 的 HMAC，不能读取 key。
CREATE TABLE audit_context_signing_key (
    key_kind text PRIMARY KEY CHECK (key_kind IN (
      'principal','system:api','system:realtime','system:bulk',
      'legacy-system-disabled'
    )),
    key_material bytea NOT NULL CHECK (octet_length(key_material)=32),
    updated_at timestamptz NOT NULL DEFAULT now()
);
REVOKE ALL ON audit_context_signing_key FROM PUBLIC;

CREATE OR REPLACE FUNCTION enforce_live_audit_principal()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  context_kind text := NULLIF(current_setting('sms.audit_subject_kind', TRUE),'');
  context_actor text := NULLIF(current_setting('sms.audit_actor_name', TRUE),'');
  context_account bigint := NULLIF(
    current_setting('sms.audit_account_id', TRUE),''
  )::bigint;
  context_identity bigint := NULLIF(
    current_setting('sms.audit_identity_id', TRUE),''
  )::bigint;
  context_app bigint := NULLIF(current_setting('sms.audit_app_id', TRUE),'')::bigint;
  context_correlation text := NULLIF(
    current_setting('sms.correlation_id', TRUE),''
  );
  context_signature text := NULLIF(
    current_setting('sms.audit_context_signature', TRUE),''
  );
  context_action text := NULLIF(current_setting('sms.audit_action', TRUE),'');
  context_domain text := NULLIF(
    current_setting('sms.audit_producer_domain', TRUE),''
  );
  audit_key bytea;
  expected_signature text;
  signature_payload text;
  stable_context boolean := FALSE;
  system_allowed boolean := FALSE;
BEGIN
  stable_context := (
    context_actor IS NOT NULL
    AND context_correlation IS NOT NULL
    AND (
      (context_kind='human' AND context_account IS NOT NULL
       AND context_identity IS NOT NULL AND context_app IS NULL)
      OR
      (context_kind='api_app' AND context_account IS NULL
       AND context_identity IS NULL AND context_app IS NOT NULL)
    )
  );

  IF stable_context THEN
    IF session_user<>'sms_owner' THEN
      SELECT key_material INTO audit_key
      FROM public.audit_context_signing_key WHERE key_kind='principal';
      IF audit_key IS NULL THEN
        RAISE EXCEPTION 'audit context signing key is unavailable'
          USING ERRCODE='23514';
      END IF;
      signature_payload := concat_ws(E'\n',
        'v2',txid_current()::text,
        encode(convert_to(session_user,'UTF8'),'hex'),
        encode(convert_to(context_correlation,'UTF8'),'hex'),
        encode(convert_to(context_kind,'UTF8'),'hex'),
        encode(convert_to(context_actor,'UTF8'),'hex'),
        coalesce(context_account::text,''),
        coalesce(context_identity::text,''),
        coalesce(context_app::text,'')
      );
      expected_signature := encode(
        public.hmac(convert_to(signature_payload,'UTF8'),audit_key,'sha256'),
        'hex'
      );
      IF context_signature IS NULL
         OR length(context_signature)<>64
         OR context_signature IS DISTINCT FROM expected_signature
      THEN
        RAISE EXCEPTION 'audit context signature is invalid'
          USING ERRCODE='23514';
      END IF;
    END IF;
    IF NEW.actor_subject_kind<>'legacy_unknown'
       AND (
         NEW.actor_subject_kind IS DISTINCT FROM context_kind
         OR NEW.actor IS DISTINCT FROM context_actor
         OR NEW.actor_account_id IS DISTINCT FROM context_account
         OR NEW.actor_identity_id IS DISTINCT FROM context_identity
         OR NEW.actor_app_id IS DISTINCT FROM context_app
       )
    THEN
      RAISE EXCEPTION 'audit subject does not match authenticated context'
        USING ERRCODE='23514';
    END IF;
    NEW.correlation_id := context_correlation::uuid;
    NEW.actor_subject_kind := context_kind;
    NEW.actor := context_actor;
    NEW.actor_account_id := context_account;
    NEW.actor_identity_id := context_identity;
    NEW.actor_app_id := context_app;
    RETURN NEW;
  END IF;

  IF NEW.actor_subject_kind IN ('human','api_app','legacy_unknown') THEN
    RAISE EXCEPTION 'live audit event has no authenticated actor context'
      USING ERRCODE='23514';
  END IF;

  IF NEW.actor_subject_kind='system' THEN
    IF NEW.actor_account_id IS NOT NULL OR NEW.actor_identity_id IS NOT NULL
       OR NEW.actor_app_id IS NOT NULL
    THEN
      RAISE EXCEPTION 'system audit event cannot name a stable user or app'
        USING ERRCODE='23514';
    END IF;

    IF context_kind IS DISTINCT FROM 'system'
       OR context_actor IS DISTINCT FROM NEW.actor
       OR context_action IS DISTINCT FROM NEW.action
       OR context_domain NOT IN ('api','realtime','bulk')
       OR context_correlation IS NULL
    THEN
      RAISE EXCEPTION 'system audit event has no authenticated producer context'
        USING ERRCODE='23514';
    END IF;

    IF session_user<>'sms_owner' THEN
      SELECT key_material INTO audit_key
      FROM public.audit_context_signing_key
      WHERE key_kind='system:' || context_domain;
      IF audit_key IS NULL THEN
        RAISE EXCEPTION 'system audit signing key is unavailable'
          USING ERRCODE='23514';
      END IF;
      signature_payload := concat_ws(E'\n',
        'system-v2',txid_current()::text,
        encode(convert_to(session_user,'UTF8'),'hex'),
        encode(convert_to(context_correlation,'UTF8'),'hex'),
        encode(convert_to(context_domain,'UTF8'),'hex'),
        encode(convert_to(context_actor,'UTF8'),'hex'),
        encode(convert_to(context_action,'UTF8'),'hex')
      );
      expected_signature := encode(
        public.hmac(convert_to(signature_payload,'UTF8'),audit_key,'sha256'),
        'hex'
      );
      IF context_signature IS NULL
         OR length(context_signature)<>64
         OR context_signature IS DISTINCT FROM expected_signature
      THEN
        RAISE EXCEPTION 'system audit context signature is invalid'
          USING ERRCODE='23514';
      END IF;
    END IF;

    system_allowed := session_user='sms_owner'
      OR (context_domain='api' AND (
          (session_user='sms_auth' AND NEW.actor='auth-system'
           AND NEW.action='account_source_conflict')
          OR (session_user='sms_accept' AND (
            (NEW.actor='vendor-test-reconciler' AND NEW.action IN (
              'vendor_test_operation_completed','vendor_test_operation_batch_attached'))
            OR (NEW.actor='security-report-collector' AND NEW.action IN (
              'security_daily_generated','security_daily_generation_unavailable'))
            OR (NEW.actor='security-report-mailer'
                AND NEW.action='security_daily_delivery_result')
            OR (NEW.actor IN (
                  'system:usage-projection','system:usage-projection-auto',
                  'operator:usage-projection-cli')
                AND NEW.action='usage_projection_rebuild')))))
      OR (context_domain='realtime' AND session_user='sms_send' AND (
          (NEW.actor='vendor-state-sync'
           AND NEW.action IN ('template_sync','sign_sync'))
          OR (NEW.actor='vendor-test-reconciler' AND NEW.action IN (
            'vendor_test_operation_completed','vendor_test_operation_batch_attached'))
          OR (NEW.actor IN ('system:usage-projection','system:usage-projection-auto')
              AND NEW.action='usage_projection_rebuild')))
      OR (context_domain='bulk' AND session_user='sms_send' AND (
          (NEW.actor='security-report-collector' AND NEW.action IN (
            'security_daily_generated','security_daily_generation_unavailable'))
          OR (NEW.actor='import-parser' AND NEW.action='message_import')
          OR (NEW.actor='security-report-scheduler'
              AND NEW.action IN ('security_daily_send','security_daily_retry'))
          OR (NEW.actor='security-report-mailer'
              AND NEW.action='security_daily_delivery_result')
          OR (NEW.actor IN ('system:usage-projection','system:usage-projection-auto')
              AND NEW.action='usage_projection_rebuild')));

    IF NOT system_allowed THEN
      RAISE EXCEPTION 'system audit producer/action is not authorized for database role'
        USING ERRCODE='23514';
    END IF;
    NEW.correlation_id := context_correlation::uuid;
    RETURN NEW;
  END IF;

  RAISE EXCEPTION 'unsupported live audit subject kind'
    USING ERRCODE='23514';
END
$$;
REVOKE ALL ON FUNCTION enforce_live_audit_principal() FROM PUBLIC;
CREATE TRIGGER trg_audit_require_live_principal
BEFORE INSERT ON audit_log
FOR EACH ROW EXECUTE FUNCTION enforce_live_audit_principal();

-- v1.6.41：日报采集器不获得审计主表访问权；只读视图只暴露非载荷列，
-- 且仅对生成日报的 sms_send 角色授 SELECT。
CREATE VIEW security_daily_audit_evidence AS
SELECT created_at, actor, actor_subject_kind, role, ip, action, object_type, object_id
FROM audit_log;
GRANT SELECT ON security_daily_audit_evidence TO sms_send;
GRANT SELECT ON security_daily_audit_evidence TO sms_accept;

-- ─────────────── 导出任务 ───────────────
CREATE TABLE export_task (
    id          BIGSERIAL PRIMARY KEY,
    public_id   UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    creator     VARCHAR(64) NOT NULL,                 -- 仅展示/兼容，不参与授权
    creator_account_id BIGINT REFERENCES user_account(id) ON DELETE RESTRICT,
    creator_identity_id BIGINT,
    scope_dept  VARCHAR(128),                         -- NULL仅表示已确认的管理员全局范围
    scope_resolved BOOLEAN NOT NULL DEFAULT FALSE,    -- 历史范围不明时禁止读取/下载
    filters     JSONB       NOT NULL,                  -- 规范化过滤条件；phone仅存phone_hmac，禁明文
    decrypted   BOOLEAN     NOT NULL DEFAULT FALSE,  -- 是否含明文手机号(审计关注)
    status      VARCHAR(10) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','running','done','failed')),
    file_path   VARCHAR(256),                         -- AES-GCM密文文件；下载时流式解密
    row_count   INTEGER,
    started_at  TIMESTAMPTZ,                          -- 兼容/观测；不再用作 fencing token
    lease_id    UUID,
    lease_expires_at TIMESTAMPTZ,
    takeover_count INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    CONSTRAINT chk_export_lease_pair CHECK (
      (lease_id IS NULL AND lease_expires_at IS NULL)
      OR (lease_id IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CONSTRAINT chk_export_lease_state CHECK (
      lease_id IS NULL OR status='running'
    ),
    CONSTRAINT chk_export_takeover_count CHECK (
      takeover_count>=0
    ),
    CONSTRAINT fk_export_creator_identity
      FOREIGN KEY (creator_identity_id,creator_account_id)
      REFERENCES auth_identity(id,account_id) ON DELETE RESTRICT
);
CREATE INDEX idx_export_creator_account
    ON export_task(creator_account_id, created_at DESC);
CREATE INDEX idx_export_lease_expiry
    ON export_task(lease_expires_at,id) WHERE lease_id IS NOT NULL;

-- ─────────────── 运行态最小权限 ───────────────
-- 七个 NOINHERIT 登录角色由 provision-db-roles 在迁移前创建/轮换密码；
-- sms_owner 保持唯一对象所有权，运行身份永不获得 DDL、TRUNCATE 或角色管理能力。
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO
    sms_auth, sms_accept, sms_send, sms_callback,
    sms_export, sms_scheduler, sms_metrics;

-- Web 账号、Provider、登录防护与权威会话。
GRANT SELECT ON
    user_account, auth_provider, auth_identity, local_credential,
    external_role_mapping, password_change_token, sys_config, app
TO sms_auth;
GRANT INSERT, UPDATE ON
    user_account, auth_provider, auth_identity, local_credential,
    password_change_token
TO sms_auth;
GRANT INSERT, UPDATE, DELETE ON external_role_mapping TO sms_auth;
GRANT INSERT ON audit_log TO sms_auth;
GRANT USAGE, SELECT ON SEQUENCE
    user_account_id_seq, auth_provider_id_seq, auth_identity_id_seq,
    external_role_mapping_id_seq, password_change_token_id_seq, audit_log_id_seq
TO sms_auth;

-- API 消息受理、管理页面与运行控制；账号表写入仍只属于 sms_auth。
GRANT SELECT ON
    app, dept_quota, sms_batch, sms_resend_action, idempotency_record, sms_chunk, sms_message,
    sms_reply, raw_vendor_log, report_event, report_event_projection, reply_event,
    unmatched_report, job_run, import_task, import_phone, approval, sms_template,
    sms_sign, blacklist, blacklist_hmac_alias, sensitive_word, callback_report_event, callback_task,
    callback_authority_lease,
    worker_lease_event, balance_snapshot, alert_log, outbox_event,
    usage_reservation, usage_frequency_subject, usage_frequency_alias,
    usage_quota_entry, usage_frequency_entry, usage_projection,
    usage_projection_drift, stat_daily, sys_config, audit_log, export_task,
    security_daily_report, security_daily_delivery_request,
    security_daily_recipient,
    vendor_test_daily_usage, vendor_test_send_attempt, vendor_test_recipient,
    vendor_test_recipient_hmac_alias, vendor_test_operation, alembic_version
TO sms_accept;
GRANT INSERT, UPDATE, DELETE ON
    app, dept_quota, sms_batch, idempotency_record, sms_message,
    import_task, import_phone, approval, sms_template, sms_sign, blacklist,
    blacklist_hmac_alias,
    sensitive_word, usage_reservation, usage_frequency_subject,
    usage_frequency_alias, usage_quota_entry, usage_frequency_entry,
    usage_projection, usage_projection_drift, sys_config, vendor_test_recipient
TO sms_accept;
GRANT INSERT ON sms_resend_action TO sms_accept;
GRANT INSERT, UPDATE ON
    security_daily_report, security_daily_delivery_request TO sms_accept;
GRANT SELECT, INSERT, DELETE, UPDATE ON security_daily_recipient TO sms_accept;
GRANT INSERT ON callback_task, alert_log TO sms_accept;
GRANT DELETE ON callback_authority_lease TO sms_accept;
GRANT INSERT, DELETE ON vendor_test_recipient_hmac_alias TO sms_accept;
GRANT INSERT, UPDATE ON
    outbox_event, vendor_test_daily_usage, vendor_test_send_attempt,
    vendor_test_operation
TO sms_accept;
GRANT SELECT, INSERT ON audit_log TO sms_accept;
GRANT USAGE, SELECT ON SEQUENCE
    app_id_seq, sms_batch_id_seq, idempotency_record_id_seq,
    sms_message_id_seq, import_task_id_seq,
    import_phone_id_seq, approval_id_seq, sms_template_id_seq, sms_sign_id_seq,
    sensitive_word_id_seq, audit_log_id_seq,
    vendor_test_send_attempt_id_seq, vendor_test_recipient_id_seq,
    callback_task_id_seq, alert_log_id_seq,
    usage_projection_version_seq, security_daily_report_id_seq
TO sms_accept;

-- 发送、拉取、对账、统计与业务 worker。
GRANT SELECT ON
    user_account, app, dept_quota, sms_batch, idempotency_record, sms_chunk, sms_message,
    sms_reply, raw_vendor_log, report_event, report_event_projection, reply_event,
    unmatched_report, job_run, import_task, import_phone, approval, sms_template, sms_sign, blacklist,
    blacklist_hmac_alias,
    sensitive_word, callback_report_event, callback_task, worker_lease_event,
    balance_snapshot, alert_log, outbox_event, usage_reservation,
    usage_frequency_subject, usage_frequency_alias, usage_quota_entry,
    usage_frequency_entry, usage_projection, usage_projection_drift, stat_daily,
    sys_config, security_daily_report, security_daily_delivery_request,
    vendor_test_daily_usage, vendor_test_send_attempt,
    security_daily_recipient,
    vendor_test_recipient, vendor_test_recipient_hmac_alias, vendor_test_operation
TO sms_send;
GRANT UPDATE (vendor_template_id,vendor_state,vendor_reject_reason,updated_at)
ON sms_template TO sms_send;
GRANT UPDATE (vendor_sign_id,vendor_state,vendor_reject_reason)
ON sms_sign TO sms_send;
GRANT INSERT, UPDATE, DELETE ON
    sms_batch, sms_chunk, sms_message, sms_reply, raw_vendor_log,
    unmatched_report, job_run, approval, balance_snapshot, alert_log,
    outbox_event, usage_reservation, usage_frequency_subject,
    usage_frequency_alias, usage_quota_entry, usage_frequency_entry,
    usage_projection, usage_projection_drift, stat_daily
TO sms_send;
GRANT INSERT, UPDATE ON security_daily_report TO sms_send;
GRANT SELECT, INSERT, UPDATE ON security_daily_delivery_request TO sms_send;
GRANT UPDATE, DELETE ON import_task TO sms_send;
GRANT INSERT, DELETE ON import_phone TO sms_send;
GRANT INSERT ON
    report_event, reply_event, worker_lease_event, audit_log
TO sms_send;
GRANT INSERT, DELETE ON callback_report_event TO sms_send;
GRANT INSERT, UPDATE, DELETE ON callback_task TO sms_send;
GRANT INSERT, UPDATE ON
    report_event_projection, vendor_test_daily_usage,
    vendor_test_send_attempt, vendor_test_operation
TO sms_send;
GRANT USAGE, SELECT ON SEQUENCE
    sms_batch_id_seq, sms_chunk_id_seq, sms_message_id_seq, sms_reply_id_seq,
    raw_vendor_log_id_seq, unmatched_report_id_seq, job_run_id_seq,
    approval_id_seq, balance_snapshot_id_seq, alert_log_id_seq,
    worker_lease_event_id_seq, vendor_test_send_attempt_id_seq,
    audit_log_id_seq, callback_task_id_seq, import_phone_id_seq,
    usage_projection_version_seq, security_daily_report_id_seq
TO sms_send;

-- 回调 worker 的横向影响限制在回调事实、租约、告警与 Outbox。
GRANT SELECT ON
    app, sms_message, callback_report_event, callback_task, callback_authority_lease,
    worker_lease_event, alert_log, outbox_event, sys_config, job_run
TO sms_callback;
GRANT SELECT (
    id, batch_no, category, app_id, biz_id, status, total,
    delivered, failed, unknown_cnt, updated_at
) ON sms_batch TO sms_callback;
GRANT INSERT, UPDATE, DELETE ON callback_report_event, callback_task TO sms_callback;
GRANT INSERT, DELETE ON callback_authority_lease TO sms_callback;
GRANT UPDATE (expires_at) ON callback_authority_lease TO sms_callback;
GRANT INSERT, UPDATE ON outbox_event, alert_log, job_run TO sms_callback;
GRANT INSERT ON worker_lease_event, audit_log TO sms_callback;
GRANT USAGE, SELECT ON SEQUENCE
    callback_task_id_seq, worker_lease_event_id_seq,
    alert_log_id_seq, audit_log_id_seq, job_run_id_seq
TO sms_callback;

-- 导出 API/worker 只能读取批准的数据集并维护导出任务。
GRANT SELECT ON
    app, sms_batch, sms_message, sms_reply, report_event_projection,
    unmatched_report,
    export_task, sys_config, outbox_event, worker_lease_event
TO sms_export;
GRANT INSERT, DELETE ON export_task TO sms_export;
GRANT UPDATE (
    status,file_path,row_count,started_at,lease_id,lease_expires_at,
    takeover_count,finished_at
) ON export_task TO sms_export;
GRANT INSERT, UPDATE ON outbox_event TO sms_export;
GRANT INSERT ON worker_lease_event, audit_log TO sms_export;
GRANT USAGE, SELECT ON SEQUENCE
    export_task_id_seq, worker_lease_event_id_seq, audit_log_id_seq
TO sms_export;

-- beat 与 durable Outbox dispatcher。
GRANT SELECT ON sys_config, job_run, outbox_event, alert_log TO sms_scheduler;
GRANT INSERT, UPDATE, DELETE ON job_run TO sms_scheduler;
GRANT INSERT, UPDATE ON outbox_event, alert_log TO sms_scheduler;
GRANT INSERT ON audit_log TO sms_scheduler;
GRANT USAGE, SELECT ON SEQUENCE
    job_run_id_seq, alert_log_id_seq, audit_log_id_seq
TO sms_scheduler;

-- Prometheus 聚合只读身份；仅允许查询聚合 SQL 实际使用的非敏感列。
GRANT SELECT (id, category, created_at, removed_freq) ON sms_batch TO sms_metrics;
GRANT SELECT (batch_id, phone_count, submitted_at, vendor_code, status)
    ON sms_chunk TO sms_metrics;
GRANT SELECT (status, lease_id, lease_expires_at)
    ON callback_task TO sms_metrics;
GRANT SELECT (task_kind, event_type)
    ON worker_lease_event TO sms_metrics;
GRANT SELECT (job_name, status, finished_at)
    ON job_run TO sms_metrics;
GRANT SELECT (kind, mismatched_dimensions, absolute_delta)
    ON usage_projection_drift TO sms_metrics;
GRANT SELECT (lease_id, lease_expires_at)
    ON export_task TO sms_metrics;
GRANT SELECT (queue, state)
    ON outbox_event TO sms_metrics;

-- 新表/序列默认不向任何运行身份授权；迁移必须显式更新本矩阵。
ALTER DEFAULT PRIVILEGES FOR ROLE sms_owner IN SCHEMA public
    REVOKE ALL ON TABLES FROM
    sms_auth, sms_accept, sms_send, sms_callback,
    sms_export, sms_scheduler, sms_metrics;
ALTER DEFAULT PRIVILEGES FOR ROLE sms_owner IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM
    sms_auth, sms_accept, sms_send, sms_callback,
    sms_export, sms_scheduler, sms_metrics;
ALTER DEFAULT PRIVILEGES FOR ROLE sms_owner IN SCHEMA public
    REVOKE ALL ON TABLES FROM sms_app;
ALTER DEFAULT PRIVILEGES FOR ROLE sms_owner IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM sms_app;

-- 升级路径永久撤销旧的广权限登录角色。
DO $legacy_role$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='sms_app') THEN
    REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM sms_app;
    REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM sms_app;
    REVOKE ALL PRIVILEGES ON SCHEMA public FROM sms_app;
    ALTER ROLE sms_app NOLOGIN;
  END IF;
END
$legacy_role$;
