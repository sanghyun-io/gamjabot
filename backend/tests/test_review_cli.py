"""Source CLI command contract; no missing Rust binary gate substitution."""

from uuid import uuid4
import pytest
from app.owner_cli import command


class Client:
    def __init__(self): self.calls = []
    def call(self, *args, **kwargs):
        self.calls.append((args, kwargs)); return {"state": "fixture"}


def test_cli_sends_only_the_actually_supplied_review_without_auto_preview(capsys):
    client = Client(); token = str(uuid4())
    command(client, ["decide", "3", "action", "approve", "once", "--review-id", token,
                     "--acknowledge-unverified-context"])
    assert len(client.calls) == 1
    args, kwargs = client.calls[0]
    assert args == ("POST", "/tools/rooms/3/actions/action/decision", {
        "decision": "approve", "scope": "once", "review_id": token, "acknowledge_unverified_context": True})
    assert kwargs == {"authenticated": True}


@pytest.mark.parametrize("options", [[], ["--review-id", "bad"], ["--review-id", str(uuid4()).upper()],
    ["--review-id", str(uuid4()), "--review-id", str(uuid4())]])
def test_missing_bad_or_duplicate_cli_review_never_fetches_or_decides(options):
    client = Client()
    with pytest.raises(RuntimeError): command(client, ["decide", "3", "action", "approve", *options])
    assert client.calls == []


def test_deny_needs_no_review_or_acknowledgement():
    client = Client(); command(client, ["decide", "3", "action", "deny"])
    assert client.calls[0][0] == ("POST", "/tools/rooms/3/actions/action/decision", {"decision": "deny", "scope": "once"})
