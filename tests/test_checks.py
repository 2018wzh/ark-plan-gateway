import asyncio
import inspect
import json

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

from gateway.store import Store


def completed(status="completed"):
    response = {"id": "resp_check", "status": status, "model": "real-model", "output": [],
                "usage": {"input_tokens": 5, "output_tokens": 2}}
    if status == "incomplete":
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    return response


class Stream(httpx.AsyncByteStream):
    def __init__(self, events):
        self.events, self.closed = events, False

    async def __aiter__(self):
        for event in self.events:
            yield b"data: " + json.dumps(event).encode() + b"\n\n"

    async def aclose(self):
        self.closed = True


@pytest_asyncio.fixture
async def rig(monkeypatch):
    monkeypatch.setenv("ARK_GATEWAY_ALLOW_UNCONFIGURED", "1")
    monkeypatch.setenv("ARK_GATEWAY_ADMIN_PASSWORD", "checks-admin-password")
    monkeypatch.setenv("ARK_GATEWAY_SERVICE_TOKEN", "checks-service-token-123456789012")
    monkeypatch.delenv("ARK_AGENT_PLAN_KEYS", raising=False)
    monkeypatch.delenv("ARK_CODING_PLAN_KEYS", raising=False)
    from gateway.main import create_app
    store = Store(":memory:", Fernet.generate_key().decode())
    calls = []
    handler = {"fn": lambda request: httpx.Response(200, json=completed())}

    async def dispatch(request):
        calls.append(request)
        response = handler["fn"](request)
        return await response if inspect.isawaitable(response) else response

    async with httpx.AsyncClient(transport=httpx.MockTransport(dispatch)) as remote:
        app = create_app(store, remote)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            assert (await client.post("/api/login", json={"password": "checks-admin-password"})).status_code == 200
            yield store, app, client, handler, calls
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", ["agent", "coding"])
@pytest.mark.parametrize("status", ["completed", "incomplete"])
async def test_pinned_sse_check_uses_mapping_and_keeps_disabled_state(rig, plan, status):
    store, app, client, handler, calls = rig
    account = store.add_account(plan, "check-secret-key", models=["alias", "unused-model"])
    store.add_account(plan, "other-secret-key", models=["alias"])
    store.update(account, enabled=0, auth_failed=1, model_mapping={"alias": "real-model"})
    stream = Stream([{"type": "response.created", "response": {"id": "resp_check"}},
                     {"type": "response." + status, "response": completed(status)}])
    handler["fn"] = lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
    response = await client.post(f"/api/accounts/{account}/check")
    result = response.json()
    assert response.status_code == 200 and result["status"] == "success"
    assert result["model"] == "alias" and result["upstream_model"] == "real-model"
    assert len(calls) == 1 and stream.closed
    assert calls[0].headers["authorization"] == "Bearer check-secret-key"
    assert calls[0].url.path == f"/api/{'plan' if plan == 'agent' else 'coding'}/v3/responses"
    assert json.loads(calls[0].content) == {"model": "real-model", "input": "Reply with OK.",
                                          "stream": True, "max_output_tokens": 128}
    saved = store.account(account)
    assert saved["enabled"] == 0 and saved["auth_failed"] == 1 and saved["check"] == result
    history = store.audit_history(errors_only=False)["items"][0]
    assert history["path"] == "/api/accounts/{account_id}/check" and history["attempt_count"] == 1
    assert history["request_id"] == result["request_id"] and history["outcome"] == "success"
    assert "check-secret-key" not in json.dumps(result)
    assert store.db.execute("SELECT input_tokens,output_tokens FROM request_daily").fetchone()[:] == (5, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("http_status,code", [(400, "MissingParameter"), (401, "InvalidApiKey"),
    (429, "AccountQuotaExceeded"), (500, "InternalServiceError"), (502, "BadGateway"), (503, "Unavailable")])
async def test_http_failures_disable_only_tested_account_and_keep_error_metadata(rig, http_status, code):
    store, app, client, handler, calls = rig
    account = store.add_account("agent", "failed-account-key")
    peer = store.add_account("agent", "shared-account-key")
    store.update(peer, quota_group=account)
    handler["fn"] = lambda request: httpx.Response(http_status,
        json={"error": {"code": code, "param": "input", "message": "private text failed-account-key"}},
        headers={"x-request-id": "upstream-check-id"})
    result = (await client.post(f"/api/accounts/{account}/check")).json()
    assert result["status"] == "failed" and result["disabled"] and result["code"] == code
    assert result["source"] == "upstream" and result["upstream_status"] == http_status
    assert store.account(account)["enabled"] == 0 and store.account(peer)["enabled"] == 1
    assert len(calls) == 1  # no failover or retry can hide the failed key
    history = store.audit_history()["items"][0]
    assert history["http_status"] == 200 and history["upstream_status"] == http_status
    assert history["error_param"] == "input" and history["upstream_request_id"] == "upstream-check-id"
    assert "private text" not in json.dumps(history) and "failed-account-key" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [
    {"type": "error", "code": "MissingParameter", "message": "private text"},
    {"type": "response.failed", "response": {"status": "failed", "error": {"code": "MissingParameter"}}},
    {"type": "response.completed", "response": {"id": "resp_bad", "status": "failed", "error": {"code": "MissingParameter"}}},
])
async def test_http_200_stream_error_is_a_failed_check(rig, event):
    store, app, client, handler, calls = rig
    account = store.add_account("coding", "stream-error-key")
    stream = Stream([event])
    handler["fn"] = lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
    result = (await client.post(f"/api/accounts/{account}/check")).json()
    assert result["status"] == "failed" and result["code"] == "MissingParameter"
    assert result["upstream_status"] == 200 and stream.closed and store.account(account)["enabled"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,code", [
    ("truncated", "Gateway.upstream_stream_incomplete"),
    ("invalid", "Gateway.upstream_response_invalid"),
    ("oversize", "Gateway.upstream_response_too_large"),
    ("connect", "Gateway.upstream_connection_failed"),
    ("read", "Gateway.check_timeout"),
    ("write", "Gateway.upstream_transport_ambiguous"),
])
async def test_transport_and_protocol_failures_disable_account(rig, mode, code):
    store, app, client, handler, calls = rig
    account = store.add_account("agent", "protocol-error-key")
    def reply(request):
        if mode == "connect":
            raise httpx.ConnectError("private host", request=request)
        if mode == "read":
            raise httpx.ReadTimeout("private host", request=request)
        if mode == "write":
            raise httpx.WriteError("private host", request=request)
        if mode == "truncated":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream([]))
        return httpx.Response(200, content=b"x" * 1_000_001 if mode == "oversize" else b"{}")
    handler["fn"] = reply
    result = (await client.post(f"/api/accounts/{account}/check")).json()
    assert result["status"] == "failed" and result["code"] == code and store.account(account)["enabled"] == 0
    assert "private host" not in json.dumps(store.audit_history())
    assert app.state.requests.active == 0 and not app.state.checks.running


@pytest.mark.asyncio
async def test_batch_is_bounded_ordered_deduplicated_and_records_each_result(rig):
    store, app, client, handler, calls = rig
    ids = [store.add_account("agent", f"batch-key-{i}") for i in range(9)]
    active, maximum = 0, 0
    async def reply(request):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        if request.headers["authorization"].endswith("-3"):
            return httpx.Response(429, json={"error": {"code": "AccountQuotaExceeded"}})
        return httpx.Response(200, json=completed())
    handler["fn"] = reply
    results = (await client.post("/api/accounts/check", json={"account_ids": ids + [ids[0]]})).json()["results"]
    assert [r["account_id"] for r in results] == ids and len(calls) == len(ids)
    assert maximum == 4 and sum(r["status"] == "failed" for r in results) == 1
    assert store.account(ids[3])["enabled"] == 0
    assert all(store.account(account)["enabled"] == 1 for account in ids if account != ids[3])
    assert store.audit_history(errors_only=False)["total"] == 9
    assert app.state.requests.active == 0 and not app.state.checks.running


@pytest.mark.asyncio
async def test_duplicate_running_check_is_skipped(rig):
    store, app, client, handler, calls = rig
    account = store.add_account("agent", "concurrent-check-key")
    entered, release = asyncio.Event(), asyncio.Event()
    async def reply(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json=completed())
    handler["fn"] = reply
    task = asyncio.create_task(client.post(f"/api/accounts/{account}/check"))
    await entered.wait()
    duplicate = (await client.post(f"/api/accounts/{account}/check")).json()
    assert duplicate["status"] == "skipped" and duplicate["code"] == "Gateway.check_in_progress"
    release.set()
    assert (await task).json()["status"] == "success" and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"api_key": "replacement-key"}, {"models": ["new-model"]},
    {"model_mapping": {"ark-code-latest": "new-model"}}])
async def test_old_failure_cannot_disable_changed_credentials_or_routes(rig, change):
    store, app, client, handler, calls = rig
    account = store.add_account("agent", "original-key")
    entered, release = asyncio.Event(), asyncio.Event()
    async def reply(request):
        entered.set()
        await release.wait()
        return httpx.Response(401, json={"error": {"code": "InvalidApiKey"}})
    handler["fn"] = reply
    task = asyncio.create_task(client.post(f"/api/accounts/{account}/check"))
    await entered.wait()
    assert (await client.patch(f"/api/accounts/{account}", json=change)).status_code == 200
    release.set()
    result = (await task).json()
    assert result["status"] == "skipped" and result["code"] == "Gateway.account_changed"
    assert store.account(account)["enabled"] == 1 and store.account(account)["check"] == {}


@pytest.mark.asyncio
async def test_cancelled_and_locally_busy_checks_do_not_disable(rig):
    store, app, client, handler, calls = rig
    account = store.add_account("agent", "cancelled-check-key")
    app.state.requests.maximum = 1  # ingress occupies the only slot
    result = (await client.post(f"/api/accounts/{account}/check")).json()
    assert result["status"] == "skipped" and not calls and store.account(account)["enabled"] == 1
    app.state.requests.maximum = 32
    def busy(request):
        raise httpx.PoolTimeout("busy", request=request)
    handler["fn"] = busy
    result = (await client.post(f"/api/accounts/{account}/check")).json()
    assert result["status"] == "skipped" and store.account(account)["enabled"] == 1
    entered = asyncio.Event()
    async def reply(request):
        entered.set()
        await asyncio.Event().wait()
    handler["fn"] = reply
    task = asyncio.create_task(client.post(f"/api/accounts/{account}/check"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.account(account)["enabled"] == 1 and store.account(account)["check"] == {}
    assert app.state.requests.active == 0 and not app.state.checks.running


@pytest.mark.asyncio
async def test_checks_require_admin_same_origin_and_valid_batch_before_execution(rig):
    store, app, client, handler, calls = rig
    account = store.add_account("agent", "validation-check-key")
    assert (await client.post(f"/api/accounts/{account}/check", headers={"origin": "http://evil.test"})).status_code == 403
    for ids, status in [([], 422), ([account, "unknown"], 404), ([account] * 101, 422)]:
        assert (await client.post("/api/accounts/check", json={"account_ids": ids})).status_code == status
    client.cookies.clear()
    assert (await client.post(f"/api/accounts/{account}/check")).status_code == 401
    assert (await client.post("/api/accounts/check", json={"account_ids": [account]})).status_code == 401
    assert not calls and store.account(account)["enabled"] == 1


@pytest.mark.asyncio
async def test_failed_check_removes_account_from_normal_routing(rig):
    store, app, client, handler, calls = rig
    account = store.add_account("agent", "disable-routing-key")
    peer = store.add_account("agent", "healthy-routing-key")
    handler["fn"] = lambda request: httpx.Response(401, json={"error": {"code": "InvalidApiKey"}})
    assert (await client.post(f"/api/accounts/{account}/check")).json()["status"] == "failed"
    handler["fn"] = lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"},
        stream=Stream([{"type": "response.completed", "response": completed()}]))
    response = await client.post("/v1/responses", headers={"authorization": "Bearer checks-service-token-123456789012"},
        json={"model": "ark-code-latest", "input": "hello"})
    assert response.status_code == 200 and len(calls) == 2
    assert calls[-1].headers["authorization"] == "Bearer healthy-routing-key"
    assert store.account(peer)["enabled"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["headers", "stream"])
async def test_total_deadline_closes_slow_check_and_disables(rig, monkeypatch, phase):
    from gateway import checks
    monkeypatch.setattr(checks, "CHECK_TIMEOUT", 0.02)
    store, app, client, handler, calls = rig
    account = store.add_account("agent", "timeout-check-key")
    class SlowStream(Stream):
        async def __aiter__(self):
            yield b'data: {"type":"response.created","response":{"id":"resp_slow"}}\n\n'
            await asyncio.Event().wait()
    stream = SlowStream([])
    async def reply(request):
        if phase == "headers":
            await asyncio.Event().wait()
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
    handler["fn"] = reply
    result = (await client.post(f"/api/accounts/{account}/check")).json()
    assert result["status"] == "failed" and result["code"] == "Gateway.check_timeout"
    assert store.account(account)["enabled"] == 0 and app.state.requests.active == 0
    assert not app.state.checks.running
    if phase == "stream":
        assert stream.closed


@pytest.mark.asyncio
async def test_latest_check_is_cleared_on_key_rotation(rig):
    store, app, client, handler, calls = rig
    account = store.add_account("agent", "original-check-key")
    assert (await client.post(f"/api/accounts/{account}/check")).json()["status"] == "success"
    unchanged = (await client.patch(f"/api/accounts/{account}", json={"label": "renamed",
        "models": ["ark-code-latest"], "model_mapping": {}})).json()
    assert unchanged["check"]["status"] == "success"
    result = (await client.patch(f"/api/accounts/{account}", json={"api_key": "rotated-check-key"})).json()
    assert result["check"] == {} and result["enabled"] == 1
