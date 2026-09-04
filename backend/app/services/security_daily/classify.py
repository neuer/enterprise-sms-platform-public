"""管理审计 action 的日报分类：单一映射承载 assessment、category、tone。"""

from __future__ import annotations

from typing import NamedTuple


class AuditClassification(NamedTuple):
    """单个审计 action 的日报分类；字段缺省值即未知 action 的默认回退。"""

    assessment: str = "管理操作"
    category: str = "其他管理操作"
    tone: str = "neutral"


_DEFAULT_CLASSIFICATION = AuditClassification()

AUDIT_CLASSIFICATION_BY_ACTION: dict[str, AuditClassification] = {
    "login": AuditClassification(assessment="认证事件", category="登录认证", tone="good"),
    "session_refresh": AuditClassification(assessment="认证事件", category="登录认证", tone="good"),
    "logout": AuditClassification(assessment="认证事件", category="登录认证", tone="good"),
    "local_password_change": AuditClassification(
        assessment="密码安全操作", category="密码安全", tone="warn"
    ),
    "local_password_reset": AuditClassification(
        assessment="密码安全操作", category="密码安全", tone="warn"
    ),
    "role_override": AuditClassification(
        assessment="账号与角色管理", category="账号与角色", tone="warn"
    ),
    "account_status_change": AuditClassification(
        assessment="账号与角色管理", category="账号与角色", tone="warn"
    ),
    "force_logout": AuditClassification(
        assessment="账号与角色管理", category="账号与角色", tone="warn"
    ),
    "config_update": AuditClassification(
        assessment="系统配置变更", category="系统配置", tone="warn"
    ),
    "security_daily_config_update": AuditClassification(
        assessment="系统配置变更", category="系统配置", tone="warn"
    ),
    "auth_provider_save_draft": AuditClassification(
        assessment="认证源配置变更", category="认证源配置", tone="warn"
    ),
    "auth_provider_test": AuditClassification(
        assessment="认证源配置变更", category="认证源配置", tone="neutral"
    ),
    "auth_provider_activate": AuditClassification(
        assessment="认证源配置变更", category="认证源配置", tone="warn"
    ),
    "auth_provider_disable": AuditClassification(
        assessment="认证源配置变更", category="认证源配置", tone="warn"
    ),
    "auth_provider_role_mappings_replace": AuditClassification(
        assessment="认证源配置变更", category="认证源配置", tone="warn"
    ),
    "app_create": AuditClassification(
        assessment="应用密钥管理", category="应用密钥", tone="neutral"
    ),
    "app_update": AuditClassification(
        assessment="应用密钥管理", category="应用密钥", tone="neutral"
    ),
    "app_disable": AuditClassification(
        assessment="应用密钥管理", category="应用密钥", tone="neutral"
    ),
    "app_rotate_key": AuditClassification(
        assessment="应用密钥管理", category="应用密钥", tone="warn"
    ),
    "app_revoke_old_key": AuditClassification(
        assessment="应用密钥管理", category="应用密钥", tone="warn"
    ),
    "app_rotate_callback_secret": AuditClassification(
        assessment="应用密钥管理", category="应用密钥", tone="warn"
    ),
    "approval_decision": AuditClassification(
        assessment="审批操作", category="审批操作", tone="neutral"
    ),
    "message_send": AuditClassification(assessment="发送操作", category="发送操作", tone="neutral"),
    "batch_cancel": AuditClassification(assessment="发送操作", category="发送操作", tone="neutral"),
    "batch_reschedule": AuditClassification(
        assessment="发送操作", category="发送操作", tone="neutral"
    ),
    "batch_resend_failed": AuditClassification(
        assessment="发送操作", category="发送操作", tone="neutral"
    ),
    "uncertain_resolve_propose": AuditClassification(
        assessment="发送操作", category="发送操作", tone="warn"
    ),
    "uncertain_resolve_confirm": AuditClassification(
        assessment="发送操作", category="发送操作", tone="warn"
    ),
    "blacklist_add": AuditClassification(
        assessment="管控操作", category="管控操作", tone="neutral"
    ),
    "blacklist_delete": AuditClassification(
        assessment="管控操作", category="管控操作", tone="neutral"
    ),
    "sensitive_word_add": AuditClassification(
        assessment="管控操作", category="管控操作", tone="neutral"
    ),
    "sensitive_word_delete": AuditClassification(
        assessment="管控操作", category="管控操作", tone="neutral"
    ),
    "reply_optout": AuditClassification(assessment="退订操作", category="退订操作", tone="neutral"),
    "export_create": AuditClassification(
        assessment="管理操作", category="数据导出", tone="neutral"
    ),
    "export_download": AuditClassification(
        assessment="管理操作", category="数据导出", tone="neutral"
    ),
    "import_create": AuditClassification(
        assessment="管理操作", category="号码导入", tone="neutral"
    ),
    "import_resolve": AuditClassification(
        assessment="管理操作", category="号码导入", tone="neutral"
    ),
}


def _audit_assessment(action: str) -> str:
    return AUDIT_CLASSIFICATION_BY_ACTION.get(action, _DEFAULT_CLASSIFICATION).assessment


def _audit_category(action: str) -> str:
    """把审计 action 归为邮件可读的类别；未知动作归入其他管理操作。"""

    return AUDIT_CLASSIFICATION_BY_ACTION.get(action, _DEFAULT_CLASSIFICATION).category


def _audit_tone(action: str) -> str:
    return AUDIT_CLASSIFICATION_BY_ACTION.get(action, _DEFAULT_CLASSIFICATION).tone
