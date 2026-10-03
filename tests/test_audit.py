import asyncio
import json
import time

import httpx
import anyio
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from gateway.audit import RequestAudit, audited_request
from gateway.store import Store

TOKEN = "audit-service-token-123456789012"
AUTH = {"authorization": "Bearer " + TOKEN}


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("ARK_GATEWAY_ALLOW_UNCONFIGURED", "1")
    monkeypatch.setenv("ARK_GATEWAY_ADMIN_PASSWORD", "audit-admin-password")
    monkeypatch.setenv("ARK_GATEWAY_SERVICE_TOKEN", TOKEN)
    monkeypatch.delenv("ARK_AGENT_PLAN_KEYS", raising=False)
    monkeypatch.delenv("ARK_CODING_PLAN_KEYS", raising=False)
    store = Store(":memory:", Fernet.generate_key().decode())
    yield store
    store.close()


@pytest.mark.asyncio
async def test_compact_error_is_logged_without_rewriting_or_private_data(setup):
    from gateway.main import create_app
    store = setup
    account = store.add_account("agent", "secret-upstream-key", models=["m"])
    body = {"error": {"code": "InvalidParameter.ContextWindow", "type": "BadRequest",
        "param": "input[0].content", "message": "private prompt and secret-upstream-key"}}
    def upstream(request):
        return httpx.Response(400, json=body, headers={"x-request-id": "upstream-123", "retry-after": "8"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        app = create_app(store, remote)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.post("/v1/responses/compact?private=do-not-log", headers=AUTH,
                json={"model": "m", "input": "private prompt"})
            assert response.status_code == 400 and response.json() == body
            assert response.headers["x-request-id"] == "upstream-123"
            assert response.headers["retry-after"] == "8"
            assert (await client.get("/api/audit")).status_code == 401
            await client.post("/api/login", json={"password": "audit-admin-password"})
            history = (await client.get("/api/audit?error_code=InvalidParameter.ContextWindow")).json()
            assert history["total"] == 1 and len(history["summary"]) == 1
            entry = history["items"][0]
            assert entry["path"] == "/v1/responses/compact" and entry["http_status"] == 400
            assert entry["source"] == "upstream" and entry["error_code"] == body["error"]["code"]
            assert entry["error_param"] == "input[0].content" and entry["account_id"] == account
            assert entry["upstream_request_id"] == "upstream-123"
            assert entry["attempts"][0]["upstream_status"] == 400
            assert "private prompt" not in json.dumps(history)
            assert "secret-upstream-key" not in json.dumps(history) and TOKEN not in json.dumps(history)
            assert (await client.get("/api/audit?limit=100000")).status_code == 422
            assert store.audit_history(errors_only=False)["total"] == 1  # no self-logging


@pytest.mark.asyncio
async def test_recovered_quota_attempt_is_searchable_separately_from_final_success(setup):
    from gateway.main import create_app
    store = setup
    for key in ("first-account-key", "second-account-key"):
        store.add_account("agent", key, models=["m"])
    calls = []
    def upstream(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"code": "AccountQuotaExceeded", "message": "usage quota"}}, headers={"x-tt-logid": "quota-id"})
        return httpx.Response(200, json={"id": "resp_success", "output": [], "model": "m"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(create_app(store, remote)), base_url="http://test") as client:
            response = await client.post("/v1/responses", headers=AUTH, json={"model": "m", "input": "hello"})
            assert response.status_code == 200
    history = store.audit_history(error_code="AccountQuotaExceeded", source="upstream")
    assert history["total"] == history["recovered"] == 1 and history["summary"] == []
    entry = history["items"][0]
    assert entry["outcome"] == "success" and entry["error_code"] == ""
    assert entry["attempt_count"] == 2
    assert entry["attempts"][0]["error_code"] == "AccountQuotaExceeded"
    assert entry["attempts"][0]["upstream_request_id"] == "quota-id"
    assert entry["attempts"][1]["outcome"] == "success"


class Stream(httpx.AsyncByteStream):
    def __init__(self, error=False):
        self.error = error

    async def __aiter__(self):
        yield b'data: {"type":"response.created","response":{"id":"resp_stream"}}\n\n'
        if self.error:
            yield b'data: {"type":"error","code":"MissingParameter","message":"private error text","param":"input"}\n\n'


@pytest.mark.asyncio
@pytest.mark.parametrize("upstream_error", [True, False])
async def test_http_200_stream_errors_have_distinct_sources(setup, upstream_error):
    from gateway.main import create_app
    store = setup
    store.add_account("coding", "stream-account-key", models=["m"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,
            stream=Stream(upstream_error), headers={"content-type": "text/event-stream", "x-request-id": "stream-id"}))) as remote:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(create_app(store, remote)), base_url="http://test") as client:
            response = await client.post("/v1/responses", headers=AUTH, json={"model": "m", "input": "hello", "stream": True})
            assert response.status_code == 200
    entry = store.audit_history()["items"][0]
    assert entry["http_status"] == entry["upstream_status"] == 200
    assert entry["outcome"] == "error"
    assert entry["source"] == ("upstream" if upstream_error else "gateway")
    assert entry["error_code"] == ("MissingParameter" if upstream_error else "Gateway.upstream_stream_incomplete")
    assert "private error text" not in json.dumps(entry)


@pytest.mark.asyncio
async def test_auth_validation_and_transport_failure_without_body_logging(setup):
    from gateway.main import create_app
    store = setup
    store.add_account("agent", "transport-account-key", models=["m"])
    def upstream(request):
        raise httpx.ReadError("DO NOT LOG PRIVATE EXCEPTION", request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(create_app(store, remote)), base_url="http://test") as client:
            assert (await client.post("/v1/responses", json={"secret": "body"})).status_code == 401
            assert (await client.post("/v1/responses", headers=AUTH, json={})).status_code == 400
            assert (await client.post("/v1/responses", headers=AUTH, json={"model":"m", "input":"hello"})).status_code == 502
    history = store.audit_history()
    assert history["total"] == 3
    assert {r["error_code"] for r in history["items"]} == {"Gateway.invalid_token", "Gateway.model_required", "Gateway.upstream_transport_ambiguous"}
    assert history["items"][0]["attempts"][0]["error_type"] == "ReadError"
    assert "DO NOT LOG" not in json.dumps(history)


@pytest.mark.asyncio
async def test_concurrent_requests_keep_separate_contexts_and_item_routes(setup):
    from gateway.main import create_app
    store = setup
    account = store.add_account("agent", "concurrent-account-key", models=["m"])
    store.bind("resp_private-id", account)
    async def upstream(request):
        await asyncio.sleep(.01)
        if request.method == "GET":
            return httpx.Response(200, json={"id":"resp_private-id"}, headers={"x-request-id":"get-id"})
        return httpx.Response(400, json={"error":{"code":"MissingParameter"}}, headers={"x-request-id":"post-id"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(create_app(store, remote)), base_url="http://test") as client:
            await asyncio.gather(client.get("/v1/responses/resp_private-id?token=private", headers=AUTH),
                client.post("/v1/responses/compact", headers=AUTH, json={"model":"m", "input":"hello"}))
    entries = store.audit_history(errors_only=False)["items"]
    assert len(entries) == 2 and len({e["request_id"] for e in entries}) == 2
    get = next(e for e in entries if e["method"] == "GET")
    assert get["path"] == "/v1/responses/{response_id}" and get["outcome"] == "success"
    assert get["upstream_request_id"] == "get-id" and get["attempts"][0]["outcome"] == "success"
    assert "resp_private-id" not in json.dumps(entries)


@pytest.mark.asyncio
async def test_audit_persists_and_prunes_by_age_and_row_limit(tmp_path, monkeypatch):
    import gateway.store as storage
    monkeypatch.setattr(storage, "MAX_AUDIT_ROWS", 3)
    path = str(tmp_path / "audit.db")
    key = Fernet.generate_key().decode()
    store = Store(path, key)
    for _ in range(5):
        async with audited_request(store, "GET", "/v1/models"):
            pass
    assert store.audit_history(errors_only=False)["total"] == 3
    first = store.audit_history(errors_only=False, limit=2)
    second = store.audit_history(errors_only=False, limit=2, before=first["next_cursor"])
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    assert {r["id"] for r in first["items"]}.isdisjoint(r["id"] for r in second["items"])
    store.db.execute("UPDATE request_audit SET created_at=? WHERE id=?", (time.time()-31*86400, second["items"][0]["id"]))
    store.db.commit()
    store.close()
    store = Store(path, key)
    assert store.audit_history(errors_only=False)["total"] == 2
    assert store.db.execute("SELECT COUNT(*) FROM request_audit").fetchone()[0] == 2
    store.close()


@pytest.mark.asyncio
async def test_attempt_history_and_untrusted_metadata_are_bounded_and_redacted(setup):
    from gateway.audit import MAX_ATTEMPTS
    audit = RequestAudit(setup, "POST", "/v1/responses")
    for index in range(22):
        audit.start_attempt({"id":str(index), "plan":"agent", "api_key":"my-secret-api-key"}, "m")
        audit.upstream(httpx.Response(400, headers={"x-request-id":"my-secret-api-key"}))
        audit.upstream_error(json.dumps({"error":{"code":"MissingParameter", "type":"BadRequest",
            "param":"input\nforged log entry", "message":"DO NOT LOG"}}).encode())
        audit.result("request")
    await audit.save()
    entry = setup.audit_history()["items"][0]
    assert entry["attempt_count"] == 22 and len(entry["attempts"]) == MAX_ATTEMPTS
    assert [a["attempt"] for a in entry["attempts"]] == [*range(1,16),22]
    assert entry["upstream_request_id"] == "[redacted]" and entry["error_param"] == "[omitted]"
    assert "my-secret-api-key" not in json.dumps(entry) and "DO NOT LOG" not in json.dumps(entry)


@pytest.mark.asyncio
async def test_storage_failure_does_not_change_response_and_reports_gap(setup, monkeypatch, caplog):
    from gateway.main import create_app
    def failed(data):
        raise OSError("private filesystem path")
    monkeypatch.setattr(setup, "record_audit", failed)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as remote:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(create_app(setup, remote)), base_url="http://test") as client:
            for _ in range(2):
                assert (await client.get("/v1/models", headers=AUTH)).status_code == 200
    assert setup.audit_write_failures == 2
    assert len(caplog.records) == 1 and "private filesystem path" not in caplog.text


def test_websocket_errors_and_warmups_are_recorded_per_frame(setup):
    from gateway.main import create_app
    store = setup
    store.add_account("agent", "websocket-account-key", models=["m"])
    remote = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(400,
        json={"error":{"code":"MissingParameter", "message":"DO NOT LOG"}}, headers={"x-request-id":"ws-id"})))
    app = create_app(store, remote)
    with TestClient(app) as client:
        with client.websocket_connect("/v1/responses", headers=AUTH) as ws:
            ws.send_json({"type":"response.create", "model":"m", "input":"private input", "stream_id":"one"})
            assert ws.receive_json()["error"]["code"] == "MissingParameter"
            ws.send_json({"type":"response.create", "model":"m", "input":"warmup", "generate":False, "stream_id":"two"})
            assert ws.receive_json()["type"] == "response.created"
            assert ws.receive_json()["type"] == "response.completed"
            ws.send_text("invalid json")
            assert ws.receive_json()["type"] == "error"
        # The client context owns store shutdown, so query before its exit.
        entries = store.audit_history(errors_only=False)["items"]
        assert len(entries) == 3
        assert len({e["request_id"] for e in entries}) == 3
        assert all(e["transport"] == "websocket" for e in entries)
        error = next(e for e in entries if e["source"] == "upstream")
        assert error["error_code"] == "MissingParameter" and error["upstream_request_id"] == "ws-id"
        assert any(e["outcome"] == "success" and e["attempt_count"] == 0 for e in entries)
        assert "private input" not in json.dumps(entries)
    asyncio.run(remote.aclose())


@pytest.mark.asyncio
async def test_cancellation_saves_client_error_without_confusing_previous_attempt(setup):
    started = anyio.Event()
    async def operation():
        async with audited_request(setup, "POST", "/v1/responses") as audit:
            account = {"id":"a", "plan":"agent", "api_key":"synthetic-key-one"}
            audit.start_attempt(account, "m")
            audit.upstream_error(b'{"error":{"code":"AccountQuotaExceeded"}}')
            audit.result("quota")
            audit.start_attempt({**account,"id":"b"}, "m")
            started.set()
            await anyio.sleep_forever()
    async with anyio.create_task_group() as group:
        group.start_soon(operation)
        await started.wait()
        group.cancel_scope.cancel()
    entry = setup.audit_history()["items"][0]
    assert entry["outcome"] == "disconnected" and entry["source"] == "client"
    assert entry["attempts"][0]["error_code"] == "AccountQuotaExceeded"
    assert entry["attempts"][1]["source"] == "client"


@pytest.mark.asyncio
async def test_storage_admission_overflow_reports_gap_without_waiting(setup):
    reserved = []
    while setup.audit_capacity.acquire(blocking=False):
        reserved.append(True)
    try:
        async with audited_request(setup, "GET", "/v1/models"):
            pass
        assert setup.audit_write_failures == 1
        assert setup.audit_history(errors_only=False)["total"] == 0
    finally:
        for _ in reserved:
            setup.audit_capacity.release()


def test_websocket_disconnect_includes_accepted_queued_frames(setup):
    from gateway.main import create_app
    class Blocked(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"response.created","response":{"id":"resp_waiting"}}\n\n'
            await anyio.sleep_forever()
    setup.add_account("agent", "queued-account-key", models=["m"])
    remote = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,
        stream=Blocked(), headers={"content-type":"text/event-stream"})))
    app = create_app(setup, remote)
    with TestClient(app) as client:
        with client.websocket_connect("/v1/responses", headers=AUTH) as ws:
            event = {"type":"response.create", "model":"m", "input":"private queued input"}
            ws.send_json(event)
            assert ws.receive_json()["type"] == "response.created"
            ws.send_json(event)
            ws.send_json(event)
            ws.send_text("invalid json")
            assert ws.receive_json()["type"] == "error"  # reader has enqueued both frames
        entries = setup.audit_history()["items"]
        assert len(entries) == 4
        assert sum(e["outcome"] == "disconnected" for e in entries) == 3
        assert sum(e["error_code"] == "client_disconnected_before_dispatch" for e in entries) == 2
        assert app.state.socket_queue_bytes == app.state.socket_history_bytes == 0
    asyncio.run(remote.aclose())
