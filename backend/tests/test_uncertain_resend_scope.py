"""不可信业务键只能作为数据，不能选择内部执行主体。"""

from dataclasses import replace

import pytest

from app.core.apikey import ApiAppContext
from app.core.auth.accounts import ApplicationPrincipal, SecurityPrincipal, UncertainEffectPrincipal
from app.services.idempotency import IdempotencyScope
from app.services.pipeline import SendPipeline, SendRequest
from app.services.usage_subject import UsageSubject


@pytest.mark.parametrize(
    "biz_id",
    [
        "manual-resend:4:2",
        "manual-resend:",
        "manual-resend:04:02",
        "manual-resend:4:3",
        "MANUAL-RESEND:4:2",
        "ur:4:2",
        "normal",
    ],
)
def test_external_prefix_keeps_app_and_account_scope(biz_id: str) -> None:
    request = SendRequest("notice", (), biz_id=biz_id)
    scopes = []
    for app_id in (101, 102):
        app = ApiAppContext(app_id, "app", "dept", frozenset({"notice"}))
        scopes.append(SendPipeline._idempotency_scope(request, app))
        web = replace(
            request,
            channel="web",
            actor=SecurityPrincipal(app_id, app_id + 10, "user", "dept", "admin"),
        )
        assert SendPipeline._idempotency_scope(web, app) == IdempotencyScope(
            "account", f"{app_id}:{app_id + 10}"
        )
    assert scopes == [IdempotencyScope("app", "101"), IdempotencyScope("app", "102")]


def test_internal_identity_key_is_complete_and_not_truncated() -> None:
    from app.services.idempotency import uncertain_resend_biz_id

    keys = {uncertain_resend_biz_id(2**63 - 1, generation) for generation in (1, 2, 2**31 - 1)}
    assert len(keys) == 3 and all(len(key) <= 32 for key in keys)
    app = ApiAppContext(101, "app", "dept", frozenset({"notice"}))
    actor = UncertainEffectPrincipal(4, 1, 2, 2, "dept")
    request = SendRequest(
        "notice",
        (),
        biz_id="manual-resend:4:2",
        actor=actor,
        usage_subject=UsageSubject(
            "api_app", 101, "dept", "notice", resolution_id=4, effect_generation=2
        ),
    )
    SendPipeline._validate_usage_subject(request)
    assert SendPipeline._idempotency_scope(request, app) == IdempotencyScope(
        "uncertain-resend", "4"
    )
    for bad in (None, "manual-resend:4:3", "manual-resend:04:2", "manual-resend:5:2"):
        with pytest.raises(ValueError):
            SendPipeline._validate_usage_subject(replace(request, biz_id=bad))
    with pytest.raises(ValueError):
        SendPipeline._validate_usage_subject(
            replace(request, usage_subject=replace(request.usage_subject, effect_generation=3))
        )
    with pytest.raises(ValueError):
        SendPipeline._validate_usage_subject(replace(request, actor=None))
    with pytest.raises(ValueError):
        SendPipeline._idempotency_scope(replace(request, actor=None, resend_of="source"), app)


def test_failed_resend_preserves_authorized_api_and_web_callers() -> None:
    app = ApiAppContext(101, "app", "dept", frozenset({"notice"}))
    request = SendRequest(
        "notice",
        (),
        biz_id="failed-recipients-v1",
        resend_of="source",
        actor=ApplicationPrincipal(101, "app", "dept"),
    )
    assert SendPipeline._idempotency_scope(request, app) == IdempotencyScope("resend", "source")
    web = replace(request, channel="web", actor=SecurityPrincipal(1, 11, "user", "dept", "admin"))
    assert SendPipeline._idempotency_scope(web, app) == IdempotencyScope("resend", "source")
    for actor in (None, ApplicationPrincipal(102, "other", "dept")):
        with pytest.raises(ValueError):
            SendPipeline._idempotency_scope(replace(request, actor=actor), app)
