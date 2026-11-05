"""Closed physical-request accounting on the private Core provider wire."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.services.core_provider_transport import (
    MAX_TEST_PHYSICAL_REQUESTS,
    _valid_call,
    _valid_result_reply,
)


# Public synthetic vectors; these do not represent a native provider run.
_GOLDEN = Path(__file__).parent / "fixtures/provider-wire-synthetic-v1.json"


def _vectors():
    return json.loads(_GOLDEN.read_text())


def test_execute_budget_is_mandatory_closed_u32_with_absolute_cap():
    call = _vectors()[2]["call"]
    assert call["physical_model_request_budget"] == 1
    assert _valid_call(call)
    for value in (None, True, -1, MAX_TEST_PHYSICAL_REQUESTS + 1):
        invalid = deepcopy(call)
        if value is None:
            invalid.pop("physical_model_request_budget")
        else:
            invalid["physical_model_request_budget"] = value
        assert _valid_call(invalid) is False


def test_terminal_count_is_mandatory_strict_and_cannot_exceed_budget():
    reply = _vectors()[6]
    assert _valid_result_reply(
        reply, request_id=2, handle=reply["handle"],
        expected_model="deployment-1", expected_budget=1,
    )
    for value in (None, True, -1, 2, MAX_TEST_PHYSICAL_REQUESTS + 1):
        invalid = deepcopy(reply)
        if value is None:
            invalid["result"].pop("physical_model_request_count")
        else:
            invalid["result"]["physical_model_request_count"] = value
        assert _valid_result_reply(
            invalid, request_id=2, handle=reply["handle"],
            expected_model="deployment-1", expected_budget=1,
        ) is False


@pytest.mark.parametrize("ok", [True, False])
def test_success_and_failure_terminals_both_require_count(ok):
    reply = deepcopy(_vectors()[6])
    if not ok:
        reply["result"] = {
            "ok": False, "error_code": "provider_error", "text": "",
            "tool_calls": [], "physical_model_request_count": 1,
        }
    assert _valid_result_reply(
        reply, request_id=2, handle=reply["handle"],
        expected_model="deployment-1", expected_budget=1,
    )
