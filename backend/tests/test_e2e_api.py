from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import e2e_api  # noqa: E402
from e2e_api import (  # noqa: E402
    CASE_IDS,
    ComposeProbe,
    HttpResponse,
    RollbackStack,
    UatFailure,
    UatSuite,
    load_keys,
    verify_callback_signature,
    wait_until,
)


class FakeRunner:
    def __init__(self, outputs: list[bytes]) -> None:
        self.outputs = outputs
        self.calls: list[list[str]] = []

    def run(self, command: Sequence[str], *, cwd: Path | None = None) -> bytes:
        self.calls.append(list(command))
        return self.outputs.pop(0)


class FakeHttp:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def request(self, *args: object, **kwargs: object) -> HttpResponse:
        self.calls.append((args, kwargs))
        return self.responses.pop(0)


class FakeQuotaProbe:
    def __init__(self, values: list[int]) -> None:
        self.values = values
        self.executed: list[str] = []

    def redis_int(self, _key: str) -> int:
        return self.values.pop(0)

    def psql_execute(self, sql: str, **_variables: str) -> None:
        self.executed.append(sql)


def test_case_registry_is_exact_autopilot_subset() -> None:
    expected = tuple(
        [f"{value:02d}" for value in range(5, 21)] + ["24", "25", "26", "27", "29"]
    )
    assert expected == CASE_IDS


def test_all_autopilot_cases_have_concrete_methods() -> None:
    suite = UatSuite.stub(run_id="fixed-run")
    assert all(callable(getattr(suite, f"case_{case_id}", None)) for case_id in CASE_IDS)


def test_rollback_stack_runs_lifo_and_reports_only_safe_error_types() -> None:
    events: list[str] = []
    rollback = RollbackStack()
    rollback.defer(lambda: events.append("first"))

    def fail() -> None:
        events.append("second")
        raise RuntimeError("must-not-leak")

    rollback.defer(fail)
    rollback.defer(lambda: events.append("third"))

    assert rollback.restore() == ("RuntimeError",)
    assert events == ["third", "second", "first"]
    assert rollback.restore() == ()


def test_wait_until_is_bounded_and_never_leaks_predicate_values() -> None:
    ticks = iter([0.0, 0.2, 0.5, 1.1])
    with pytest.raises(UatFailure, match="UAT-05 timeout") as captured:
        wait_until(
            "05",
            lambda: None,
            timeout_s=1,
            clock=lambda: next(ticks),
            sleeper=lambda _seconds: None,
        )
    assert "must-not-leak" not in str(captured.value)


def test_key_file_requires_fixed_apps_without_echoing_values(tmp_path: Path) -> None:
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"app-iam": "secret-value"}), encoding="utf-8")
    with pytest.raises(ValueError, match="API keys are incomplete") as captured:
        load_keys(path)
    assert "secret-value" not in str(captured.value)


def test_phone_space_is_valid_unique_and_not_embedded_in_source() -> None:
    suite = UatSuite.stub(run_id="fixed-run")
    values = {suite.phone(1, index) for index in range(10)}
    assert len(values) == 10
    assert all(len(value) == 11 and value.startswith("1") and value.isdecimal() for value in values)

    source = (SCRIPTS / "e2e_api.py").read_text(encoding="utf-8")
    import re

    assert re.search(r"(?<!\d)1\d{10}(?!\d)", source) is None
    assert "dev_iam_verify_key" not in source


def test_uat_25_checks_export_filter_shape_without_scanning_hmac_digits() -> None:
    source = (SCRIPTS / "e2e_api.py").read_text(encoding="utf-8")

    assert "filters::text !~ '1[0-9]{10}'" not in source
    assert "NOT (filters ? 'phone')" in source
    assert "jsonb_array_elements_text(filters->'phone_hmacs')" in source
    assert "phone_hmac !~ '^[0-9a-f]{64}$'" in source
    assert "public_id=CAST(:'task_public_id' AS uuid)" in source
    assert "creator_account_id IS NOT NULL AND scope_resolved" in source


def test_compose_probe_uses_argv_variables_and_safe_scalar_counts(tmp_path: Path) -> None:
    runner = FakeRunner([b"cancelled\n", b"7\n"])
    probe = ComposeProbe(
        runner,
        compose_file=tmp_path / "compose.yml",
        repository_root=tmp_path,
    )

    assert probe.psql_value("SELECT :'batch_no'", batch_no="a" * 32) == "cancelled"
    assert probe.redis_int("quota:app:2:20260712") == 7
    assert all(call[:3] == ["docker", "compose", "-f"] for call in runner.calls)
    assert "batch_no=" + "a" * 32 not in runner.calls[0]
    assert runner.calls[0][-1] == "SELECT '" + "a" * 32 + "'"
    assert runner.calls[1][-8:] == [
        "exec",
        "-T",
        "redis-control",
        "sh",
        "-ec",
        (
            'exec redis-cli --user sms_control --askpass --raw GET "$1" '
            "< /run/secrets/redis_control_password"
        ),
        "sh",
        "quota:app:2:20260712",
    ]

    with pytest.raises(ValueError, match="invalid database probe variable"):
        probe.psql_value("SELECT 1", unsafe="line\nbreak")


def test_compose_probe_count_only_and_beat_stop_registers_start_first(
    tmp_path: Path,
) -> None:
    runner = FakeRunner([b"1\n", b"", b""])
    probe = ComposeProbe(
        runner,
        compose_file=tmp_path / "compose.yml",
        repository_root=tmp_path,
    )
    rollback = RollbackStack()

    assert probe.psql_count("SELECT count(*) FROM sms_batch") == 1
    with pytest.raises(ValueError, match="count-only"):
        probe.psql_count("SELECT content FROM sms_batch")

    probe.stop_beat(rollback)
    assert runner.calls[1][-2:] == ["stop", "beat"]
    assert rollback.restore() == ()
    assert runner.calls[2][-2:] == ["start", "beat"]


def test_callback_signature_verifier_rejects_stale_timestamp() -> None:
    secret = "memory-only-secret"
    raw_body = '{"event":"batch.finished"}'
    timestamp = "1000"
    signature = hmac.new(
        secret.encode(),
        f"{timestamp}.{raw_body}".encode(),
        hashlib.sha256,
    ).hexdigest()

    assert verify_callback_signature(
        secret,
        raw_body=raw_body,
        timestamp=timestamp,
        signature=signature,
        now_s=1300,
    )
    assert not verify_callback_signature(
        secret,
        raw_body=raw_body,
        timestamp=timestamp,
        signature=signature,
        now_s=1301,
    )


def test_heartbeat_case_waits_real_interval_without_mutating_history() -> None:
    source = (SCRIPTS / "e2e_api.py").read_text(encoding="utf-8")

    assert "UPDATE job_run SET started_at" not in source
    assert 'wait_until("27", stalled_alert, timeout_s=130' in source
    assert '"provider_code": "ad"' in source


def test_role_update_uses_account_id_from_login_response() -> None:
    http = FakeHttp(
        [
            HttpResponse(
                200,
                {
                    "token": "operator-token",
                    "user": {
                        "account_id": 42,
                        "identity_id": 84,
                        "provider_code": "ad",
                        "username": "operator01",
                        "display_name": "Operator",
                        "dept": "Operations",
                        "role": "operator",
                    },
                },
            ),
            HttpResponse(200, {}),
        ]
    )
    suite = UatSuite(http, None, {})
    suite._tokens["admin01"] = "admin-token"

    suite.login("operator01")
    suite._set_role("approver", True)

    assert http.calls[1][0][:2] == (
        "PUT",
        "/api/v1/web/admin/users/42/role",
    )
    assert http.calls[1][1]["payload"] == {
        "role": "approver",
        "role_override": True,
    }


def test_callback_dead_case_waits_for_alert_after_dead_state_is_visible() -> None:
    source = (SCRIPTS / "e2e_api.py").read_text(encoding="utf-8")
    case_20 = source.split("    def case_20(self) -> None:", maxsplit=1)[1].split(
        "\n    def case_", maxsplit=1
    )[0]

    assert 'lambda: self._alerts("20", "callback_dead") or None' in case_20
    assert 'if not self._alerts("20", "callback_dead")' not in case_20


def test_approval_expiration_case_waits_for_alert_after_expired_state_is_visible() -> None:
    source = (SCRIPTS / "e2e_api.py").read_text(encoding="utf-8")
    case_12 = source.split("    def case_12(self) -> None:", maxsplit=1)[1].split(
        "\n    def case_", maxsplit=1
    )[0]

    assert 'wait_until("12", quota_refunded, timeout_s=15' in case_12
    assert 'wait_until("12", expiration_alert, timeout_s=15' in case_12
    assert "redis_int(quota_key) != before" not in case_12
    assert 'raise UatFailure("UAT-12 expiration alert missing")' not in case_12


def test_scheduled_cancel_case_waits_for_durable_usage_release_projection() -> None:
    source = (SCRIPTS / "e2e_api.py").read_text(encoding="utf-8")
    case_15 = source.split("    def case_15(self) -> None:", maxsplit=1)[1].split(
        "\n    def case_", maxsplit=1
    )[0]

    assert 'wait_until("15", quota_refunded, timeout_s=15' in case_15
    assert "redis_int(quota_key) != before" not in case_15
    assert 'raise UatFailure("UAT-15 quota was not refunded")' not in case_15


@pytest.mark.parametrize(
    ("quota_values", "alert_items", "should_pass"),
    [
        ([10, 11, 10], [{"detail": {"batch_no": "b" * 32}}], True),
        ([10, 10], [{"detail": {"batch_no": "b" * 32}}], False),
        ([10, 11, 11], [{"detail": {"batch_no": "b" * 32}}], False),
        ([10, 11, 10], [], False),
    ],
)
def test_approval_expiration_case_requires_reserve_refund_and_alert(
    monkeypatch: pytest.MonkeyPatch,
    quota_values: list[int],
    alert_items: list[dict[str, object]],
    should_pass: bool,
) -> None:
    batch_no = "b" * 32
    http = FakeHttp(
        [
            HttpResponse(
                200,
                [{"key": "market_approval_threshold", "value": "50"}],
            ),
            HttpResponse(200, []),
            HttpResponse(
                200,
                {
                    "batch_no": batch_no,
                    "status": "pending_approval",
                    "quota_cost": 1,
                },
            ),
            HttpResponse(202, {}),
            HttpResponse(200, {"status": "expired"}),
            HttpResponse(200, {"items": alert_items}),
        ]
    )
    probe = FakeQuotaProbe(quota_values)
    suite = UatSuite(http, None, {}, probe=probe, run_id="fixed-run")  # type: ignore[arg-type]
    suite._tokens.update({"admin01": "admin-token", "operator01": "operator-token"})

    def immediate_wait(
        case_id: str,
        predicate: object,
        **_bounds: object,
    ) -> object:
        value = predicate()  # type: ignore[operator]
        if value is None:
            raise UatFailure(f"UAT-{case_id} timeout")
        return value

    monkeypatch.setattr(e2e_api, "wait_until", immediate_wait)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    if should_pass:
        suite.case_12()
    else:
        with pytest.raises(UatFailure, match="UAT-12"):
            suite.case_12()


def test_market_window_is_outside_now_and_returns_exact_next_start() -> None:
    shanghai = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 7, 12, 21, 30, 20, tzinfo=shanghai)

    window, expected = e2e_api.closed_market_window(now)

    assert window == "21:32-21:33"
    assert expected == datetime(2026, 7, 12, 21, 32, tzinfo=shanghai)

    rollover = datetime(2026, 7, 12, 23, 58, tzinfo=shanghai)
    window, expected = e2e_api.closed_market_window(rollover)
    assert window == "00:00-00:01"
    assert expected == datetime(2026, 7, 13, 0, 0, tzinfo=shanghai)


def test_case08_restores_original_market_window_before_case09(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shanghai = ZoneInfo("Asia/Shanghai")
    expected_start = (datetime.now(shanghai) + timedelta(minutes=2)).replace(
        second=0,
        microsecond=0,
    )
    expected_end = expected_start + timedelta(minutes=1)
    window = (
        f"{expected_start.hour:02d}:{expected_start.minute:02d}-"
        f"{expected_end.hour:02d}:{expected_end.minute:02d}"
    )
    previous = "07:13-19:47"
    http = FakeHttp(
        [
            HttpResponse(
                200,
                [{"key": "market_send_window", "value": previous}],
            ),
            HttpResponse(200, []),
            HttpResponse(
                200,
                {
                    "batch_no": "b" * 32,
                    "status": "scheduled",
                    "deferred_reason": "market_window",
                    "scheduled_at": expected_start.isoformat(),
                },
            ),
            HttpResponse(200, {}),
            HttpResponse(200, []),
            HttpResponse(200, []),
        ]
    )
    suite = UatSuite(http, None, {"app-mkt": "memory-key"}, run_id="fixed-run")
    suite._tokens["admin01"] = "memory-token"
    monkeypatch.setattr(
        e2e_api,
        "closed_market_window",
        lambda _now: (window, expected_start),
    )
    events: list[str] = []

    def case09() -> None:
        latest_config = [call for call in http.calls if call[0][1] == "/api/v1/web/admin/configs"][
            -1
        ]
        assert latest_config[1]["payload"] == {
            "items": [{"key": "market_send_window", "value": previous}]
        }
        events.append("case09")

    suite.case_09 = case09  # type: ignore[method-assign]

    suite.run(("08", "09"))

    config_calls = [call for call in http.calls if call[0][1] == "/api/v1/web/admin/configs"]
    assert config_calls[1][1]["payload"] == {
        "items": [{"key": "market_send_window", "value": window}]
    }
    assert config_calls[2][1]["payload"] == {
        "items": [{"key": "market_send_window", "value": previous}]
    }
    assert events == ["case09"]
    assert suite.rollback.restore() == ()


def test_safe_stat_snapshot_roundtrip() -> None:
    stat = "2026-07-05|1|2|3|4|5;2026-07-06|6|7|8|9|10"

    assert e2e_api.parse_stat_snapshot(stat) == (
        ("2026-07-05", 1, 2, 3, 4, 5),
        ("2026-07-06", 6, 7, 8, 9, 10),
    )


def test_cleanup_http_rejects_silent_failure() -> None:
    suite = UatSuite(FakeHttp([HttpResponse(500, None)]), None, {})

    with pytest.raises(UatFailure, match="cleanup HTTP 500"):
        suite._cleanup_http("14", "DELETE", "/resource")


def test_run_emits_safe_per_case_progress(capsys: pytest.CaptureFixture[str]) -> None:
    suite = UatSuite.stub(run_id="fixed-run")
    suite.case_05 = lambda: None  # type: ignore[method-assign]

    assert suite.run(("05",)) == ["05"]
    assert json.loads(capsys.readouterr().out) == {"case": "05", "status": "success"}


def test_fault_barrier_waits_for_queued_batches_and_chunks_that_can_still_send() -> None:
    source = (SCRIPTS / "e2e_api.py").read_text(encoding="utf-8")

    barrier = source[source.index("def _wait_send_pipeline_idle") : source.index("def case_05")]
    assert "FROM sms_batch b" in barrier
    assert "b.status = 'queued'" in barrier
    assert "c.batch_id = b.id" in barrier
    assert "c.status IN ('pending','submitting','retrying')" in barrier
    assert "b.status = 'sending'" not in barrier


def test_force_resume_cleanup_verifies_pause_codes_are_cleared() -> None:
    http = FakeHttp(
        [
            HttpResponse(200, {"resumed_batches": 1, "paused_codes": ["999"]}),
            HttpResponse(
                200,
                {
                    "realtime_code": None,
                    "bulk_code": None,
                    "balance": 5000,
                    "threshold": 10000,
                },
            ),
        ]
    )
    suite = UatSuite(http, None, {})
    suite._tokens["admin01"] = "safe-token"

    suite._force_resume_and_verify_unpaused("16")

    assert http.calls[0][0][:2] == ("POST", "/api/v1/web/admin/queue/resume?force=true")

    paused = FakeHttp(
        [
            HttpResponse(200, {"resumed_batches": 0, "paused_codes": ["999"]}),
            HttpResponse(
                200,
                {
                    "realtime_code": "999",
                    "bulk_code": None,
                    "balance": 5000,
                    "threshold": 10000,
                },
            ),
        ]
    )
    failed = UatSuite(paused, None, {})
    failed._tokens["admin01"] = "safe-token"
    with pytest.raises(UatFailure, match="queue pause cleanup failed"):
        failed._force_resume_and_verify_unpaused("16")
