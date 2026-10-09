from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from perf_smoke import DrainSnapshot, HttpResponse, LoadEvent, PerformanceFailure  # noqa: E402
from perf_vendor_scale import (  # noqa: E402
    NOTICE_APP,
    PHASE_A_PHONES,
    PHASE_B_P95_LIMIT_SECONDS,
    PHASE_B_VENDOR_QPS,
    PHASE_C_BATCH_SIZE,
    RESTORE_BATCH_SIZE,
    RESTORE_RESERVED_QPS,
    RESTORE_VENDOR_QPS,
    ROW_BUDGET_TOTAL,
    NoticeAppProvisioner,
    RuntimeConfigClient,
    SmsComposeDrainProbe,
    SqlRuntimeConfigClient,
    VendorScaleConfig,
    VendorScaleSuite,
    load_test_update_ssh,
    mock_send_phone_counts,
    parse_phases,
    refuse_simultaneous_maxima,
    validate_mock_base_url,
)


class ImmediateScheduler:
    def __call__(
        self,
        events: Sequence[LoadEvent],
        handler: Any,
    ) -> list[Any]:
        return [handler(event) for event in events]


class FrozenClock:
    def __init__(self) -> None:
        self.value = 1.0

    def __call__(self) -> float:
        return self.value


class FakeProbe:
    def __init__(self) -> None:
        self.snapshots = 0
        self.poll_triggers = 0
        self.delivered = 0
        self.sending_leftover = 0
        self.uncertain = 0

    def snapshot(self) -> DrainSnapshot:
        self.snapshots += 1
        return DrainSnapshot(0, {"realtime": 0, "bulk": 0, "callback": 0})

    def message_status_counts(self) -> tuple[int, int, int]:
        return self.delivered, self.sending_leftover, self.uncertain

    def trigger_poll_report(self) -> None:
        self.poll_triggers += 1

    def worker_config(self) -> tuple[int, int]:
        return RESTORE_VENDOR_QPS, RESTORE_RESERVED_QPS


class FakeApi:
    def __init__(self, *, vendor_mode: str = "setup_required") -> None:
        self.vendor_mode = vendor_mode
        self.calls: list[tuple[str, str]] = []
        self.cancelled: list[str] = []
        self.config_updates: list[dict[str, str]] = []
        self.send_index = 0

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        self.calls.append((method, path))
        if path == "/api/v1/web/admin/apps" and method == "POST":
            return HttpResponse(200, {"id": 9, "api_key": "temporary-notice-key"})
        if path == "/api/v1/web/admin/apps/9" and method == "GET":
            return HttpResponse(
                200,
                {
                    "dept": "平台技术部",
                    "allowed_categories": ["notice"],
                    "default_sign": "demo-sign",
                    "daily_quota": 0,
                    "rate_limit_per_min": 60000,
                    "blacklist_check": False,
                    "allowed_ips": [],
                    "callback_url": None,
                    "callback_report_enabled": False,
                    "status": 1,
                },
            )
        if path == "/api/v1/web/admin/apps/9" and method == "PUT":
            assert isinstance(payload, dict) and payload.get("status") == 0
            return HttpResponse(200, {"status": 0})
        if path == "/api/v1/web/admin/vendor-test/status":
            return HttpResponse(200, {"mode": self.vendor_mode})
        if path == "/api/v1/web/admin/configs" and method == "GET":
            return HttpResponse(
                200,
                [
                    {"key": "vendor_qps", "value": "5"},
                    {"key": "reserved_realtime_qps", "value": "2"},
                    {"key": "vendor_batch_size", "value": "500"},
                ],
            )
        if path == "/api/v1/web/admin/configs" and method == "PUT":
            assert isinstance(payload, dict)
            items = payload["items"]
            update = {str(item["key"]): str(item["value"]) for item in items}
            self.config_updates.append(update)
            return HttpResponse(200, [])
        if path == "/api/v1/messages/send":
            assert isinstance(payload, dict)
            assert payload["category"] == "notice"
            assert headers is not None and headers.get("X-Api-Key") == "notice-key"
            self.send_index += 1
            status = "scheduled" if payload.get("scheduled_at") else "queued"
            return HttpResponse(
                200,
                {"status": status, "batch_no": f"batch{self.send_index:08d}"},
            )
        if path.endswith("/cancel"):
            self.cancelled.append(path)
            return HttpResponse(200, {"status": "cancelled"})
        raise AssertionError(f"unexpected {method} {path}")


class FakeMock:
    def __init__(self) -> None:
        self.reset_count = 0
        self.sends = 1

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        if method == "POST" and path == "/_mock/state":
            self.reset_count += 1
            return HttpResponse(200, {"reset": True})
        if method == "GET" and path == "/_mock/state":
            return HttpResponse(
                200,
                {
                    "pending_reports": 0,
                    "send_calls": [{"customId": "c1", "mobile": "188****0001,188****0002"}]
                    * self.sends,
                },
            )
        raise AssertionError(f"unexpected mock {method} {path}")


def _suite(
    api: FakeApi,
    mock: FakeMock | None = None,
    *,
    config: VendorScaleConfig | None = None,
) -> VendorScaleSuite:
    token_api = api
    return VendorScaleSuite(
        token_api,
        mock or FakeMock(),
        FakeProbe(),
        {NOTICE_APP: "notice-key"},
        RuntimeConfigClient(token_api, "admin-token"),
        config=config
        or VendorScaleConfig(
            phase_a_batches=2,
            phase_a_phones=2,
            phase_b_rates=(1,),
            phase_b_seconds=1,
            phase_b_row_budget=4,
            phase_c_batches=1,
            phase_c_phones=2,
            phase_c_row_budget=4,
            drain_timeout_s=2,
        ),
        scheduler=ImmediateScheduler(),
        clock=FrozenClock(),
        sleeper=lambda _seconds: None,
        run_id="abcd1234",
    )


def test_mock_base_url_accepts_only_local_http_origins() -> None:
    assert validate_mock_base_url("http://127.0.0.1:9028") == "http://127.0.0.1:9028"
    assert validate_mock_base_url("http://mock-vendor:9028/") == "http://mock-vendor:9028"
    with pytest.raises(PerformanceFailure, match="SCALE-00"):
        validate_mock_base_url("https://127.0.0.1:9028")
    with pytest.raises(PerformanceFailure, match="SCALE-00"):
        validate_mock_base_url("http://vendor.example.invalid")
    with pytest.raises(PerformanceFailure, match="SCALE-00"):
        validate_mock_base_url("http://user:pass@127.0.0.1:9028")


def test_refuses_simultaneous_vendor_maxima() -> None:
    refuse_simultaneous_maxima(vendor_qps=199, vendor_batch_size=PHASE_C_BATCH_SIZE)
    with pytest.raises(PerformanceFailure, match="SCALE-00"):
        refuse_simultaneous_maxima(
            vendor_qps=PHASE_B_VENDOR_QPS,
            vendor_batch_size=PHASE_C_BATCH_SIZE,
        )


def test_default_config_stays_within_200k_and_phase_caps() -> None:
    VendorScaleConfig().validate()
    planned = 8 * PHASE_A_PHONES + 40_000 + 60 * 1_000
    assert planned <= ROW_BUDGET_TOTAL


def test_mock_send_phone_counts_use_masked_lists_only() -> None:
    assert mock_send_phone_counts(
        {"send_calls": [{"mobile": "188****0001,188****0002,188****0003"}]}
    ) == [3]
    with pytest.raises(PerformanceFailure, match="SCALE-02"):
        mock_send_phone_counts({"send_calls": [{"mobile": ""}]})


def test_runtime_config_restore_writes_5_2_500_and_rejects_dual_maxima() -> None:
    api = FakeApi()
    client = RuntimeConfigClient(api, "admin-token")
    assert client.snapshot() == {
        "vendor_qps": "5",
        "reserved_realtime_qps": "2",
        "vendor_batch_size": "500",
    }
    with pytest.raises(PerformanceFailure, match="SCALE-00"):
        client.apply({"vendor_qps": "200", "vendor_batch_size": "1000"})
    client.restore()
    assert api.config_updates[-1] == {
        "vendor_qps": str(RESTORE_VENDOR_QPS),
        "reserved_realtime_qps": str(RESTORE_RESERVED_QPS),
        "vendor_batch_size": str(RESTORE_BATCH_SIZE),
    }


def test_parse_phases_rejects_unknown_and_duplicates() -> None:
    assert parse_phases("c") == ("c",)
    assert parse_phases("d") == ("d",)
    assert parse_phases("a,b,c") == ("a", "b", "c")
    with pytest.raises(PerformanceFailure, match="SCALE-00"):
        parse_phases("a,a")
    with pytest.raises(PerformanceFailure, match="SCALE-00"):
        parse_phases("e")


def test_default_phases_exclude_d() -> None:
    default = VendorScaleConfig()
    assert default.phases == ("a", "b", "c")
    assert "d" not in default.phases
    assert default.measure_throughput is False
    default.validate()


def test_phase_d_budget_counts_60k_and_refuses_acd() -> None:
    config = VendorScaleConfig(phases=("d",))
    config.validate()
    assert config.planned_phase_d_phones() == 60_000
    assert config.planned_phase_d_phones() <= ROW_BUDGET_TOTAL
    assert config.planned_phase_d_phones() <= config.phase_d_row_budget
    with pytest.raises(ValueError, match="A\\+C\\+D"):
        VendorScaleConfig(phases=("a", "c", "d")).validate()
    with pytest.raises(ValueError, match="A\\+C\\+D"):
        VendorScaleConfig(phases=("a", "b", "c", "d")).validate()
    with pytest.raises(ValueError, match="hard row cap"):
        VendorScaleConfig(phases=("b", "d"), phase_b_row_budget=160_000).validate()


def test_phase_d2_refuses_200_plus_1000() -> None:
    with pytest.raises(ValueError, match="D2"):
        VendorScaleConfig(
            phases=("d",),
            phase_d2_vendor_qps=PHASE_B_VENDOR_QPS,
            phase_d2_batch_size=PHASE_C_BATCH_SIZE,
        ).validate()
    with pytest.raises(PerformanceFailure, match="SCALE-00"):
        refuse_simultaneous_maxima(
            vendor_qps=PHASE_B_VENDOR_QPS,
            vendor_batch_size=PHASE_C_BATCH_SIZE,
        )
    with pytest.raises(ValueError, match="D3"):
        VendorScaleConfig(
            phases=("d",),
            phase_d3_batch_size=PHASE_C_BATCH_SIZE,
        ).validate()


def test_suite_can_run_phase_c_only() -> None:
    api = FakeApi()
    mock = FakeMock()
    result = _suite(
        api,
        mock,
        config=VendorScaleConfig(
            phase_a_batches=2,
            phase_a_phones=2,
            phase_b_rates=(1,),
            phase_b_seconds=1,
            phase_b_row_budget=4,
            phase_c_batches=1,
            phase_c_phones=2,
            phase_c_row_budget=4,
            drain_timeout_s=2,
            phases=("c",),
        ),
    ).run()
    assert result.phase_a_requests == 0
    assert result.cancelled_scheduled_batches == 0
    assert result.phase_b_ramps == ()
    assert result.phase_c_requests == 1
    assert not api.cancelled
    assert not any(update.get("vendor_qps") == "200" for update in api.config_updates)
    assert api.config_updates[-1]["vendor_qps"] == "5"


def test_suite_runs_notice_only_cancels_and_restores_on_success() -> None:
    api = FakeApi()
    mock = FakeMock()
    result = _suite(api, mock).run()
    assert result.cancelled_scheduled_batches == 2
    assert result.phase_a_requests == 2
    assert result.restored_vendor_qps == 5
    assert result.phase_c_max_phones_per_send == 2
    assert any(path == "/api/v1/messages/send" for _method, path in api.calls)
    assert api.cancelled
    assert api.config_updates[-1]["vendor_qps"] == "5"
    assert mock.reset_count >= 1


def test_suite_refuses_controlled_vendor_and_still_restores() -> None:
    api = FakeApi(vendor_mode="controlled")
    with pytest.raises(PerformanceFailure, match="controlled"):
        _suite(api).run()
    assert api.config_updates[-1] == {
        "vendor_qps": "5",
        "reserved_realtime_qps": "2",
        "vendor_batch_size": "500",
    }


def test_phase_row_budget_fail_closed() -> None:
    suite = _suite(FakeApi())
    suite._charge(4, phase="a")
    with pytest.raises(PerformanceFailure, match="phase a"):
        suite._charge(1, phase="a")


def test_sms_compose_drain_probe_uses_status_sql_without_phones() -> None:
    class RecordingRunner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def run(self, command: Sequence[str], *, cwd: Path | None = None) -> bytes:
            argv = [str(item) for item in command]
            self.calls.append(argv)
            if "psql" in argv:
                return b"0\n"
            return b"0\n0\n0\n"

    runner = RecordingRunner()
    snapshot = SmsComposeDrainProbe(runner).snapshot()
    assert snapshot.empty
    sql = " ".join(runner.calls[0])
    assert "queued" in sql and "sending" in sql
    assert "phone" not in sql.casefold()


def test_test_update_env_loader_rejects_unexpected_keys(tmp_path: Path) -> None:
    path = tmp_path / ".env.test-update"
    path.write_text(
        "SMS_TEST_UPDATE_TARGET=user@example.test\nSMS_TEST_UPDATE_PORT=22\n",
        encoding="utf-8",
    )
    target, port = load_test_update_ssh(path)
    assert target.endswith("example.test")
    assert port == 22
    path.write_text("SMS_TEST_UPDATE_TARGET=user@example.test\nOTHER=1\n", encoding="utf-8")
    with pytest.raises(PerformanceFailure, match="SCALE-03"):
        load_test_update_ssh(path)


def test_sql_config_client_restores_5_2_500() -> None:
    class RecordingRunner:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def run(self, command: Sequence[str], *, cwd: Path | None = None) -> bytes:
            sql = str(command[-1])
            self.calls.append(sql)
            if sql.startswith("SELECT"):
                return b"vendor_qps=5\nreserved_realtime_qps=2\nvendor_batch_size=500\n"
            return b"UPDATE 1\n"

    runner = RecordingRunner()
    client = SqlRuntimeConfigClient(runner)
    client.snapshot()
    client.apply({"vendor_qps": "20", "reserved_realtime_qps": "8"})
    client.restore()
    restore_sql = " ".join(runner.calls[-3:])
    assert "vendor_qps" in restore_sql
    assert "vendor_batch_size" in restore_sql


def test_notice_app_provisioner_creates_and_disables_without_logging_key() -> None:
    api = FakeApi()
    provisioner = NoticeAppProvisioner(api, "admin-token")
    keys = provisioner.create(sign_name="demo-sign")
    assert keys[NOTICE_APP] == "temporary-notice-key"
    provisioner.disable()
    assert ("PUT", "/api/v1/web/admin/apps/9") in api.calls


def test_classify_does_not_mark_vendor_scale_as_g2_performance() -> None:
    from classify_ci_changes import classify_paths

    # 性能压测已移出日常门禁（#554），分类结果不再有 performance 维度；
    # 脚本改动仍按普通后端路径进入 G2。
    result = classify_paths(["scripts/perf_vendor_scale.py", "docs/PERFORMANCE.md"])
    assert result.g2 is True
    assert not hasattr(result, "performance")


class StepClock:
    def __init__(self, step: float) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        current = self.value
        self.value += self.step
        return current


def _phase_b_only_config(**overrides: Any) -> VendorScaleConfig:
    values: dict[str, Any] = {
        "phase_a_batches": 2,
        "phase_a_phones": 2,
        "phase_b_rates": (1,),
        "phase_b_seconds": 1,
        "phase_b_row_budget": 4,
        "phase_c_batches": 1,
        "phase_c_phones": 2,
        "phase_c_row_budget": 4,
        "drain_timeout_s": 2,
        "phases": ("b",),
    }
    values.update(overrides)
    return VendorScaleConfig(**values)


def test_phase_b_p95_default_stays_two_seconds() -> None:
    assert PHASE_B_P95_LIMIT_SECONDS == 2.0
    default = VendorScaleConfig()
    assert default.phase_b_p95_s == 2.0
    assert default.measure_phase_b is False


def test_phase_b_default_p95_fail_closes_on_high_latency() -> None:
    api = FakeApi()
    suite = _suite(api, config=_phase_b_only_config())
    suite.config_client.snapshot()
    suite.clock = StepClock(2.1)
    with pytest.raises(PerformanceFailure, match="SCALE-01 single-number P95"):
        suite._phase_b()


def test_phase_b_p95_override_records_high_latency_without_raising() -> None:
    api = FakeApi()
    suite = _suite(api, config=_phase_b_only_config(phase_b_p95_s=999.0))
    suite.config_client.snapshot()
    suite.clock = StepClock(2.1)
    ramps = suite._phase_b()
    assert len(ramps) == 1
    assert ramps[0].accept_p95_s >= 2.0
    assert ramps[0].accept_p50_s == ramps[0].accept_p95_s
    assert ramps[0].accept_p99_s == ramps[0].accept_max_s
    assert ramps[0].p95_gate_applied is True
    assert ramps[0].http_errors == 0


def test_phase_b_measure_mode_skips_inter_ramp_reset_and_keeps_ramps() -> None:
    api = FakeApi()
    mock = FakeMock()
    suite = _suite(
        api,
        mock,
        config=_phase_b_only_config(measure_phase_b=True, phase_b_rates=(1, 2)),
    )
    suite.config_client.snapshot()
    before = mock.reset_count
    ramps = suite._phase_b()
    assert mock.reset_count == before
    assert [ramp.target_rps for ramp in ramps] == [1, 2]
    assert suite.phase_b_ramps_recorded == ramps


def test_phase_b_measure_mode_does_not_raise_on_high_p95() -> None:
    api = FakeApi()
    suite = _suite(api, config=_phase_b_only_config(measure_phase_b=True))
    suite.config_client.snapshot()
    suite.clock = StepClock(2.1)
    ramps = suite._phase_b()
    assert ramps[0].accept_p95_s >= PHASE_B_P95_LIMIT_SECONDS
    assert ramps[0].p95_gate_applied is False
    assert ramps[0].accepted == 1
    assert ramps[0].mock_sends == 0


def _phase_d_only_config(**overrides: Any) -> VendorScaleConfig:
    values: dict[str, Any] = {
        "phase_a_batches": 2,
        "phase_a_phones": 2,
        "phase_b_rates": (1,),
        "phase_b_seconds": 1,
        "phase_b_row_budget": 4,
        "phase_c_batches": 1,
        "phase_c_phones": 2,
        "phase_c_row_budget": 4,
        "drain_timeout_s": 2,
        "phases": ("d",),
        "measure_throughput": True,
        "phase_d_include_d3": False,
        "phase_d_wave_phones": 2,
        "phase_d1_batches": 2,
        "phase_d1_phones": 2,
        "phase_d2_batches": 2,
        "phase_d2_phones": 2,
        "phase_d_row_budget": 8,
    }
    values.update(overrides)
    return VendorScaleConfig(**values)


def test_phase_d_only_restores_and_skips_2s_p95_gate() -> None:
    api = FakeApi()
    mock = FakeMock()
    result = _suite(api, mock, config=_phase_d_only_config()).run()
    assert result.phase_a_requests == 0
    assert result.phase_c_requests == 0
    assert result.phase_b_ramps == ()
    assert len(result.phase_d_stages) == 2
    assert [stage.name for stage in result.phase_d_stages] == ["d1", "d2"]
    assert all(stage.p95_gate_applied is False for stage in result.phase_d_stages)
    assert result.measure_throughput is True
    assert result.restored_vendor_qps == RESTORE_VENDOR_QPS
    assert result.restored_reserved_qps == RESTORE_RESERVED_QPS
    assert result.restored_batch_size == RESTORE_BATCH_SIZE
    assert api.config_updates[-1] == {
        "vendor_qps": str(RESTORE_VENDOR_QPS),
        "reserved_realtime_qps": str(RESTORE_RESERVED_QPS),
        "vendor_batch_size": str(RESTORE_BATCH_SIZE),
    }
    assert not any(
        update.get("vendor_qps") == "200" and update.get("vendor_batch_size") == "1000"
        for update in api.config_updates
    )
    assert any(update.get("vendor_qps") == "20" for update in api.config_updates)
    assert not api.cancelled
