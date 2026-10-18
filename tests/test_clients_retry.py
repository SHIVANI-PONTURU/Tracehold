"""Phase 3 step 3: a tool server that is briefly unreachable does not stop a worker. Reads retry on transport errors
and 502/503/504; writes retry only when the request never reached the server."""
import httpx2
import pytest

from agents.common.clients import HandoffClient, HandoffError, with_retries

NO_WAIT = (0, 0, 0)


def client(handler):
    return HandoffClient("http://tool-server", "20", backoff=NO_WAIT, transport=httpx2.MockTransport(handler))


def test_reads_retry_through_refused_connections_and_503s():
    seen = []

    def handler(req):
        seen.append(req.url.path)
        if len(seen) == 1:
            raise httpx2.ConnectError("refused", request=req)
        if len(seen) == 2:
            return httpx2.Response(503, json={})
        return httpx2.Response(200, json=[{"id": "INV-0283-x"}])
    assert client(handler).investigations() == [{"id": "INV-0283-x"}]
    assert len(seen) == 3


def test_writes_retry_only_before_the_request_reached_the_server():
    seen = []

    def refused_then_ok(req):
        seen.append(1)
        if len(seen) < 3:
            raise httpx2.ConnectError("refused", request=req)
        return httpx2.Response(200, json={"investigation_id": "INV-0283-x"})
    assert client(refused_then_ok).claim("tracer")["investigation_id"] == "INV-0283-x"

    sent = []

    def read_timeout(req):
        sent.append(1)
        raise httpx2.ReadTimeout("no answer", request=req)            # the server may have acted: never resend
    with pytest.raises(httpx2.ReadTimeout):
        client(read_timeout).submit("trace", {})
    assert len(sent) == 1


def test_errors_reach_the_caller_after_the_last_try():
    def down(req):
        raise httpx2.ConnectError("refused", request=req)
    with pytest.raises(httpx2.ConnectError):
        client(down).gate("INV-0283-x")

    def conflict(req):
        return httpx2.Response(409, json={"error": {"code": "wrong_state", "message": "not waiting"}})
    with pytest.raises(HandoffError) as e:
        client(conflict).gate_decide("INV-0283-x", [], "placeholder")
    assert e.value.code == "wrong_state"


def test_a_rejected_chat_answer_is_returned_not_raised():
    def unknown(req):
        return httpx2.Response(422, json={"accepted": False, "unknown_ids": ["T999999999"]})
    assert client(unknown).chat_answer("MSG-0283-x-1", "It went to T999999999.", ["T999999999"])["unknown_ids"] == \
        ["T999999999"]


def test_with_retries_gives_up_after_the_backoff():
    calls, waits = [], []

    def send():
        calls.append(1)
        raise httpx2.ConnectError("refused")
    with pytest.raises(httpx2.ConnectError):
        with_retries(send, True, "GET /x", backoff=(1, 2), sleep=waits.append)
    assert len(calls) == 3 and waits == [1, 2]
