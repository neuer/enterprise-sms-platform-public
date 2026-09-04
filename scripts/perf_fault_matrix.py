#!/usr/bin/env python3
"""周期性故障矩阵：半成功、冷投影与 backlog 恢复的不可变断言。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FaultCase:
    name: str
    invariant: str
    auto_resend: bool
    fail_closed: bool


FAULT_CASES = (
    FaultCase(
        "vendor_success_response_lost",
        "chunk 进入 uncertain，禁止自动重发或切换供应商",
        False,
        True,
    ),
    FaultCase(
        "vendor_success_mark_submitted_failed",
        "本地落库失败后进入 uncertain，gateway 只调用一次",
        False,
        True,
    ),
    FaultCase(
        "submitting_timeout_uncertain",
        "submitting 超时只转 uncertain，不得改回 pending",
        False,
        True,
    ),
    FaultCase(
        "redis_flush_projection_rebuild",
        "投影重建期间发送失败关闭，恢复后才重新受理",
        False,
        True,
    ),
    FaultCase(
        "worker_broker_backlog_drain",
        "恢复后按 child chunk / admission 有界排空",
        False,
        True,
    ),
)


def assert_matrix() -> None:
    names = [item.name for item in FAULT_CASES]
    if len(names) != len(set(names)):
        raise RuntimeError("fault case names must be unique")
    if any(item.auto_resend for item in FAULT_CASES):
        raise RuntimeError("fault matrix must not allow automatic resend")
    if not all(item.fail_closed for item in FAULT_CASES):
        raise RuntimeError("fault matrix must fail closed")


if __name__ == "__main__":
    assert_matrix()
