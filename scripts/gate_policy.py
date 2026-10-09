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
    "tests/test_call_order.py",
    "tests/test_gate_scripts.py",
)
ORDINARY_ROOT_DOCS = frozenset({"README.md", "CONTRIBUTING.md"})
# 按职责拆分的大模块：逻辑模块 = 入口文件 + 登记的拆出文件（相对 backend/app）。
# 不变量片段、pipeline 覆盖率组与 vendor-live 受保护路径都按逻辑模块整体生效。
# 拆分时必须在此登记新文件；check_invariants 会拒绝未登记的同前缀新文件。
SPLIT_MODULE_PARTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "services/pipeline.py": (
            "services/pipeline_admission.py",
            "services/pipeline_contracts.py",
            "services/pipeline_idempotency.py",
        ),
        "services/usage_ledger.py": (),
        "tasks/send_repository.py": (),
    }
)
# 与入口同前缀、但早于拆分就独立存在的模块，不属于对应逻辑模块。
SPLIT_MODULE_UNRELATED = frozenset({"services/pipeline_repository.py"})


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
