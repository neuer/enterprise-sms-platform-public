from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from call_order import dotted_name, first_reach_precedes  # noqa: E402


def _guard(call: ast.Call) -> bool:
    return dotted_name(call.func).endswith(".require_allowed")


def _bounded(call: ast.Call) -> bool:
    return dotted_name(call.func) == "run_bounded"


def _precedes(*sources: str) -> bool:
    return first_reach_precedes(sources, entry="_accept_claimed", first=_guard, then=_bounded)


def test_direct_guard_before_bounded_passes_and_reverse_fails() -> None:
    ordered = """
class SendPipeline:
    async def _accept_claimed(self, request):
        self.recipient_guard.require_allowed(request.mobiles)
        await run_bounded(protect, request)
"""
    reversed_order = """
class SendPipeline:
    async def _accept_claimed(self, request):
        await run_bounded(protect, request)
        self.recipient_guard.require_allowed(request.mobiles)
"""
    assert _precedes(ordered)
    assert not _precedes(reversed_order)


def test_order_follows_stage_methods_and_module_functions_across_files() -> None:
    entry = """
class SendPipeline:
    async def _accept_claimed(self, request):
        self._validate_recipients(request)
        await self._filter_recipients(request)

    def _validate_recipients(self, request):
        self.recipient_guard.require_allowed(request.mobiles)
"""
    stages = """
async def protect_batched(request):
    return await run_bounded(protect, request)

class Stages:
    async def _filter_recipients(self, request):
        return await protect_batched(request)
"""
    validate = "        self._validate_recipients(request)\n"
    filtering = "        await self._filter_recipients(request)\n"
    swapped = entry.replace(validate + filtering, filtering + validate)
    assert swapped != entry
    assert _precedes(entry, stages)
    assert not _precedes(swapped, stages)


def test_missing_guard_bounded_or_entry_fails_closed() -> None:
    no_guard = """
class SendPipeline:
    async def _accept_claimed(self, request):
        await run_bounded(protect, request)
"""
    no_bounded = """
class SendPipeline:
    async def _accept_claimed(self, request):
        self.recipient_guard.require_allowed(request.mobiles)
"""
    assert not _precedes(no_guard)
    assert not _precedes(no_bounded)
    assert not _precedes("def other():\n    pass\n")


def test_protocol_stub_does_not_hide_implementation() -> None:
    source = """
class Port(Protocol):
    def _validate_recipients(self, request): ...

class SendPipeline:
    async def _accept_claimed(self, request):
        self._validate_recipients(request)
        await run_bounded(protect, request)

    def _validate_recipients(self, request):
        self.recipient_guard.require_allowed(request.mobiles)
"""
    assert _precedes(source)
