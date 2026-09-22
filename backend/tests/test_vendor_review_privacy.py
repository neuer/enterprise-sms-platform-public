from __future__ import annotations

import json
import traceback

import pytest
from celery.backends.base import Backend

from app.services.vendor_review import map_vendor_review_state
from app.tasks import celery_app


@pytest.mark.parametrize("value", [13800138000, -13800138000, "prefix13800138000", True])
def test_review_parser_exception_and_task_result_omit_untrusted_values(value: object) -> None:
    with pytest.raises(ValueError) as captured:
        map_vendor_review_state(value, object_name="template")  # type: ignore[arg-type]
    error = captured.value
    result = Backend(app=celery_app).prepare_exception(error, serializer="json")
    assert "13800138000" not in "".join(traceback.format_exception(error)) + json.dumps(result)
    assert error.args == ("unknown vendor checkType",)


@pytest.mark.parametrize(("value", "expected"), [(0, "pending"), (1, "approved"), (2, "rejected")])
def test_documented_vendor_review_states_are_preserved(value: int, expected: str) -> None:
    assert map_vendor_review_state(value, object_name="template") == expected
