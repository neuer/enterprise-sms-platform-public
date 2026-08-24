from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "deploy" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from continuity_manager import (  # noqa: E402
    CONSUMER_SERVICES,
    ContinuityError,
    ContinuityManager,
    main,
)

NOW = datetime(2026, 8, 24, 2, 0, tzinfo=UTC)


class FakeRunner:
    def __init__(self, *, stop_fails: bool = False, running: set[str] | None = None) -> None:
        self.stop_fails = stop_fails
        self.running = set(running or ())
        self.calls: list[list[str]] = []

    def run(self, command: list[str] | tuple[str, ...]) -> bytes:
        argv = list(command)
        self.calls.append(argv)
        if "stop" in argv:
            if self.stop_fails:
                raise ContinuityError("simulated stop failure")
            self.running.difference_update(CONSUMER_SERVICES)
            return b""
        if "ps" in argv:
            return b"container-id\n" if argv[-1] in self.running else b""
        raise AssertionError(argv)


def write_evidence(path: Path, value: dict[str, object]) -> str:
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    return hashlib.sha256(raw).hexdigest()


def engage_value() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "production_continuity_engage",
        "evidence_id": "1" * 32,
        "approved_at": "2026-08-24T01:30:00Z",
        "outage_start": "2026-08-24T01:00:00Z",
        "business_rto_seconds": 43200,
        "old_system_fallback_allowed": True,
    }


def release_value(state: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "production_continuity_release",
        "fence_id": state["fence_id"],
        "engage_evidence_sha256": state["engage_evidence_sha256"],
        "outage_start": state["outage_start"],
        "approved_at": "2026-08-24T01:45:00Z",
        "change_record_sha256": "2" * 64,
        "approver_one_subject_sha256": "3" * 64,
        "approver_two_subject_sha256": "4" * 64,
        "approver_one_controlled": True,
        "approver_two_controlled": True,
        "old_route_disabled": True,
        "new_route_exclusive": True,
        "inflight_reconciled": True,
        "uncertain_no_auto_resend": True,
    }


def manager(tmp_path: Path, runner: FakeRunner, *, clock=lambda: NOW) -> ContinuityManager:
    state_root = tmp_path / "continuity"
    state_root.mkdir(mode=0o700, exist_ok=True)
    return ContinuityManager(
        root=tmp_path,
        state_root=state_root,
        runner=runner,
        clock=clock,
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )


def test_engage_persists_intent_before_failed_stop_and_blocks_after_restart(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "engage.json"
    digest = write_evidence(evidence, engage_value())
    first = manager(tmp_path, FakeRunner(stop_fails=True))

    with pytest.raises(ContinuityError, match="stop verification"):
        first.engage(evidence, digest)

    persisted = json.loads(first.state_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "intent"
    assert persisted["outage_start"] == "2026-08-24T01:00:00Z"
    assert persisted["failure_code"] == "consumer_stop_failed"
    assert first.state_path.stat().st_mode & 0o777 == 0o600
    restarted = manager(tmp_path, FakeRunner())
    with pytest.raises(ContinuityError, match="fence is active"):
        restarted.gate()


def test_release_requires_bound_two_party_evidence_and_then_allows_start(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(running=set(CONSUMER_SERVICES))
    service = manager(tmp_path, runner)
    engage = tmp_path / "engage.json"
    engage_digest = write_evidence(engage, engage_value())

    engaged = service.engage(engage, engage_digest)

    assert engaged["status"] == "engaged"
    with pytest.raises(ContinuityError, match="fence is active"):
        service.gate()
    state = json.loads(service.state_path.read_text(encoding="utf-8"))
    release = tmp_path / "release.json"
    value = release_value(state)
    value["approver_two_subject_sha256"] = value["approver_one_subject_sha256"]
    invalid_digest = write_evidence(release, value)
    calls_before = len(runner.calls)
    with pytest.raises(ContinuityError, match="current fence"):
        service.release(release, invalid_digest)
    assert len(runner.calls) == calls_before

    release_digest = write_evidence(release, release_value(state))
    released = service.release(release, release_digest)

    assert released["status"] == "released"
    assert service.gate()["blocked"] is False
    much_later = manager(tmp_path, FakeRunner(), clock=lambda: NOW + timedelta(days=30))
    assert much_later.gate()["status"] == "released"


def test_bad_release_assertion_fails_before_any_compose_call(tmp_path: Path) -> None:
    runner = FakeRunner()
    service = manager(tmp_path, runner)
    engage = tmp_path / "engage.json"
    engage_digest = write_evidence(engage, engage_value())
    service.engage(engage, engage_digest)
    state = json.loads(service.state_path.read_text(encoding="utf-8"))
    release = tmp_path / "release.json"
    value = release_value(state)
    value["uncertain_no_auto_resend"] = False
    digest = write_evidence(release, value)
    runner.calls.clear()

    with pytest.raises(ContinuityError, match="assertions are incomplete"):
        service.release(release, digest)

    assert runner.calls == []
    assert service.status()["blocked"] is True


def test_development_cli_rejects_without_touching_state_or_docker(tmp_path: Path) -> None:
    with pytest.raises(ContinuityError, match="production-only"):
        main(["--root", str(tmp_path), "--mode", "development", "status"])
    assert list(tmp_path.iterdir()) == []
