"""本地与 CI 共用的测试分区、静态检查及廉价合同清单。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

CRITICAL_MARKERS = ("property", "concurrency", "fault_injection", "authorization", "idempotency")
ISOLATED_EXTRA_NODES = frozenset(
    f"tests/test_auth_transition_integrity.py::{name}"
    for name in (
        "test_real_redis_wrong_type_does_not_block_pending_hash_tail",
        "test_real_redis_due_only_orphan_never_writes_lock_audit",
        "test_real_redis_pending_hash_persists_until_ack",
    )
)
FRONTEND_CONTRACT_TESTS = ("tests/test_frontend_contract.py",)
GATE_CONTRACT_TESTS = (
    *FRONTEND_CONTRACT_TESTS,
    "tests/test_ci_workflows.py",
    "tests/test_ci_change_classifier.py",
    "tests/test_pre_vcs_gates.py",
    "tests/test_gate_execution.py",
    "tests/test_gate_evidence.py",
    "tests/test_coverage_gates.py",
    "tests/test_frontend_coverage_gates.py",
    "tests/test_operations_coverage.py",
    "tests/test_call_order.py",
    "tests/test_single_implementation.py",
    "tests/test_size_budget.py",
    "tests/test_any_budget.py",
    "tests/test_gate_scripts.py",
)
ORDINARY_ROOT_DOCS = frozenset({"README.md", "CONTRIBUTING.md"})
# 按职责拆分的大模块：逻辑模块 = 入口文件 + 登记的拆出文件（相对 backend/app）。
# 不变量片段、pipeline 覆盖率组与 vendor-live 受保护路径都按逻辑模块整体生效。
# 拆分时必须在此登记新文件；check_invariants 会拒绝未登记的同前缀新文件。
SPLIT_MODULE_PARTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "services/pipeline.py": (
            "services/pipeline_acceptance.py",
            "services/pipeline_admission.py",
            "services/pipeline_commit.py",
            "services/pipeline_contracts.py",
            "services/pipeline_idempotency.py",
            "services/pipeline_sensitive_audit.py",
        ),
        "services/usage_ledger.py": (
            "services/usage_ledger_common.py",
            "services/usage_ledger_frequency.py",
            "services/usage_ledger_frequency_merge.py",
            "services/usage_ledger_frequency_subjects.py",
            "services/usage_ledger_projection.py",
            "services/usage_ledger_release.py",
        ),
        "tasks/send_repository.py": (
            "tasks/send_repository_contracts.py",
            "tasks/send_repository_attempt.py",
            "tasks/send_repository_failover.py",
            "tasks/send_repository_payload.py",
            "tasks/send_repository_split.py",
        ),
    }
)
# 与入口同前缀、但早于拆分就独立存在的模块，不属于对应逻辑模块。
SPLIT_MODULE_UNRELATED = frozenset({"services/pipeline_repository.py"})

# 应用源码体积预算例外（size_budget.py）：新文件 .vue ≤600 行、其余 ≤800 行。
# 存量超限文件按 max(1000, D122 当时行数) 封顶，页面按当时行数封顶；上限只许下调，
# 文件回到默认上限内时必须删除条目。新增条目需要写明理由并经评审。
_LEGACY_OVERSIZE = "D122 存量超限，待按职责拆分"
_VENDOR_LIVE_FROZEN = "vendor-live 精确保护路径，首发后随部署工具解冻再拆"
_CSS_LAYER_CONTRACT = "样式分层契约：每页一个分片，跨页共享组只在 shared.css"
SIZE_BUDGET_EXCEPTIONS: Mapping[str, tuple[int, str]] = MappingProxyType(
    {
        "frontend/src/styles/workspace/shared.css": (1612, _CSS_LAYER_CONTRACT),
        "backend/app/services/security_daily_repository.py": (1421, _LEGACY_OVERSIZE),
        "backend/app/tasks/send.py": (1408, _VENDOR_LIVE_FROZEN),
        "backend/app/services/report_repository.py": (1254, _LEGACY_OVERSIZE),
        "backend/app/services/send_inflight.py": (1247, _LEGACY_OVERSIZE),
        "backend/app/core/auth/users.py": (1241, _LEGACY_OVERSIZE),
        "backend/app/services/uncertain_resolution.py": (1217, _LEGACY_OVERSIZE),
        "frontend/src/styles/theme/public-shell.css": (1216, _CSS_LAYER_CONTRACT),
        "backend/app/core/auth/service.py": (1215, _LEGACY_OVERSIZE),
        "backend/app/services/pipeline_repository.py": (1195, _LEGACY_OVERSIZE),
        "backend/app/api/vendor_test.py": (1119, _VENDOR_LIVE_FROZEN),
        "backend/app/services/ops_repository.py": (1088, _LEGACY_OVERSIZE),
        "backend/app/services/callback_repository.py": (1078, _LEGACY_OVERSIZE),
        "backend/app/core/auth/jwt.py": (1044, _LEGACY_OVERSIZE),
        "backend/app/services/report_ingest.py": (1000, _LEGACY_OVERSIZE),
        "backend/app/services/vendor_test_operation_repository.py": (1000, _VENDOR_LIVE_FROZEN),
        "backend/app/api/web_messages.py": (1000, _VENDOR_LIVE_FROZEN),
        "frontend/src/views/ConfigView.vue": (918, _VENDOR_LIVE_FROZEN),
        "backend/app/api/messages.py": (1000, _VENDOR_LIVE_FROZEN),
        "backend/app/settings.py": (1000, _VENDOR_LIVE_FROZEN),
        "backend/app/cli.py": (1000, _VENDOR_LIVE_FROZEN),
        "frontend/src/styles/workspace/send.css": (1000, _CSS_LAYER_CONTRACT),
        "backend/app/services/import_repository.py": (1000, _LEGACY_OVERSIZE),
        "backend/app/tasks/send_repository.py": (1000, _VENDOR_LIVE_FROZEN),
        "backend/app/core/auth/runtime.py": (1000, _VENDOR_LIVE_FROZEN),
        "backend/app/tasks/send_repository_attempt.py": (1000, _VENDOR_LIVE_FROZEN),
        "backend/app/services/send_admission.py": (1000, _LEGACY_OVERSIZE),
        "backend/app/services/export_file.py": (1000, _LEGACY_OVERSIZE),
        "backend/app/services/metrics.py": (1000, _LEGACY_OVERSIZE),
        "frontend/src/styles/workspace/message-batch.css": (1000, _CSS_LAYER_CONTRACT),
        "backend/app/services/reply_ingest.py": (1000, _LEGACY_OVERSIZE),
        "frontend/src/components/VendorTestConsole.vue": (684, _VENDOR_LIVE_FROZEN),
    }
)

# backend/app 显式 Any 棘轮（any_budget.py，D123）：登记值必须等于当前计数，只许下调；
# 清零即删除条目。新文件不得使用显式 Any，新增条目或上调需经评审并写明理由。
EXPLICIT_ANY_BUDGET: Mapping[str, int] = MappingProxyType(
    {
        "backend/app/api/admin.py": 2,
        "backend/app/api/admin_step_up.py": 1,
        "backend/app/api/apps.py": 1,
        "backend/app/api/messages.py": 4,
        "backend/app/api/ops.py": 3,
        "backend/app/api/vendor_control_ready.py": 2,
        "backend/app/api/vendor_test.py": 2,
        "backend/app/api/web_messages.py": 3,
        "backend/app/cli.py": 3,
        "backend/app/core/apikey.py": 2,
        "backend/app/core/audit.py": 4,
        "backend/app/core/auth/admin_authorization.py": 3,
        "backend/app/core/auth/admission.py": 1,
        "backend/app/core/auth/jwt.py": 14,
        "backend/app/core/auth/jwt_revocation.py": 2,
        "backend/app/core/auth/jwt_tokens.py": 1,
        "backend/app/core/auth/ldap_real.py": 7,
        "backend/app/core/auth/service.py": 9,
        "backend/app/core/auth/session_policy.py": 6,
        "backend/app/core/auth/session_policy_sync.py": 7,
        "backend/app/core/bounded_executor.py": 3,
        "backend/app/core/broker_authorization.py": 2,
        "backend/app/core/jobtrack.py": 2,
        "backend/app/core/redis_eval.py": 1,  # Lua EVAL 返回形状由各脚本决定
        "backend/app/core/runtime_resources.py": 3,
        "backend/app/core/worker_probe.py": 4,
        "backend/app/main.py": 1,
        "backend/app/services/admin.py": 4,
        "backend/app/services/admin_step_up.py": 1,
        "backend/app/services/alert_repository.py": 1,
        "backend/app/services/anomaly_repository.py": 2,
        "backend/app/services/app_ratelimit_cutover.py": 2,
        "backend/app/services/auth_provider_repository.py": 2,
        "backend/app/services/blacklist.py": 4,
        "backend/app/services/callback_batch_event.py": 1,
        "backend/app/services/current_alerts_repository.py": 1,
        "backend/app/services/dashboard_repository.py": 1,
        "backend/app/services/dispatch_blacklist.py": 7,
        "backend/app/services/export.py": 1,
        "backend/app/services/idempotency.py": 3,
        "backend/app/services/idempotency_lifecycle.py": 1,
        "backend/app/services/ops_repository.py": 3,
        "backend/app/services/pipeline_acceptance.py": 1,
        "backend/app/services/pipeline_admission.py": 1,
        "backend/app/services/pipeline_idempotency.py": 2,
        "backend/app/services/pipeline_repository.py": 2,
        "backend/app/services/raw_lease.py": 2,
        "backend/app/services/reconcile_repository.py": 1,
        "backend/app/services/reply_ingest.py": 6,
        "backend/app/services/reply_repository.py": 1,
        "backend/app/services/report_ingest.py": 6,
        "backend/app/services/report_projection.py": 1,
        "backend/app/services/report_repository.py": 2,
        "backend/app/services/send_admission_repository.py": 2,
        "backend/app/services/send_inflight.py": 4,
        "backend/app/services/send_locks.py": 2,
        "backend/app/services/sensitive.py": 2,
        "backend/app/services/sign_management.py": 1,
        "backend/app/services/template_management.py": 1,
        "backend/app/services/uncertain_resolution.py": 2,
        "backend/app/services/vendor_control_state.py": 3,
        "backend/app/services/vendor_review.py": 1,
        "backend/app/services/vendor_test_guard.py": 6,
        "backend/app/services/vendor_test_operation_repository.py": 6,
        "backend/app/services/vendor_test_pause.py": 1,
        "backend/app/services/vendor_test_recipient_repository.py": 10,
        "backend/app/services/vendor_test_security_audit.py": 1,
        "backend/app/services/vendor_test_step_up.py": 3,
        "backend/app/services/vendor_test_uat.py": 3,
        "backend/app/tasks/__init__.py": 1,
        "backend/app/tasks/anomaly.py": 1,
        "backend/app/tasks/imports.py": 3,
        "backend/app/tasks/send.py": 5,
        "backend/app/tasks/send_repository.py": 4,
        "backend/app/tasks/send_repository_attempt.py": 4,
        "backend/app/tasks/worker_probe.py": 1,
        "backend/app/vendor/zhihui.py": 22,
    }
)


def logical_module_files(entry: str) -> tuple[str, ...]:
    """返回逻辑模块的全部文件（相对 backend/app），入口在前。"""
    return (entry, *SPLIT_MODULE_PARTS.get(entry, ()))


def isolated_node(nodeid: str) -> bool:
    """所有 integration 用例及显式真实 Redis 用例必须进入隔离依赖分区。"""
    return nodeid.startswith("tests/integration/") or nodeid in ISOLATED_EXTRA_NODES


def isolated_paths(root: Path) -> frozenset[str]:
    """从测试源树收集隔离文件，不依赖 shell 中易遗漏的文件枚举。"""
    return frozenset(
        [
            str(path.relative_to(root))
            for path in (root / "backend/tests/integration").glob("test_*.py")
        ]
        + [f"backend/{node.split('::')[0]}" for node in ISOLATED_EXTRA_NODES]
    )
