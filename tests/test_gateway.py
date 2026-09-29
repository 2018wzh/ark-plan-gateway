import json
import time

import httpx
import pytest
from cryptography.fernet import Fernet

from gateway.pool import AccountPool, classify_error
from gateway.store import Store


@pytest.fixture
def store(tmp_path):
    db = Store(str(tmp_path / "test.db"), Fernet.generate_key().decode())
    yield db
    db.close()


def test_import_deduplicates_and_encrypts(store):
    a = store.add_account("agent", "secret-value", models=["m"])
    assert store.add_account("agent", "secret-value") == a
    assert store.account(a)["api_key_mask"] == "••••alue"
    assert "secret-value" not in (store.db.execute("SELECT api_key FROM accounts").fetchone()[0])


def test_statistics_are_persistent_and_shared_quota_is_not_double_counted(tmp_path):
    key = Fernet.generate_key().decode()
    path = str(tmp_path / "statistics.db")
    store = Store(path, key)
    first = store.add_account("agent", "key-first", models=["m"])
    second = store.add_account("agent", "key-second", models=["m"])
    store.set_quota_group(second, first)
    usage = {"five_hour": {"quota": 100, "used": 35, "reset_time": time.time() + 3600}}
    for account_id in (first, second):
        store.update(account_id, usage_json=json.dumps(usage), quota_checked_at=time.time())
    store.record_quota(first, "agent", usage, time.time())
    store.record_request(first, "success", 1200, 12, 3)
    store.record_request(second, "rate", 80)
    store.close()
    store = Store(path, key)
    all_stats = store.statistics(None, 7)
    assert len(all_stats["quota_current"]) == 1
    assert len(all_stats["quota_history"]) == 1
    assert sum(row["requests"] for row in all_stats["daily"]) == 2
    assert sum(row["input_tokens"] for row in all_stats["daily"]) == 12
    assert sum(row["requests"] for row in store.statistics(first, 7)["daily"]) == 1
    assert sum(row["requests"] for row in store.statistics(second, 7)["daily"]) == 1
    store.close()


@pytest.mark.asyncio
async def test_cross_plan_selection_and_group_cooldown(store):
    a = store.add_account("agent", "key-agent", models=["m"])
    c = store.add_account("coding", "key-coding", models=["m"])
    store.update(a, usage_json=json.dumps({"five": {"quota": 100, "used": 90, "reset_time": time.time()+60}}))
    pool = AccountPool(store)
    selected = await pool.candidates("m")
    assert [x["id"] for x in selected] == [a, c]
    pool.update_result(selected[0], "quota", time.time()+60)
    assert [x["id"] for x in await pool.candidates("m")] == [c]
    pool.update_result(store.account(c, True), "quota", time.time()+30)
    status, code, retry_at, exact = pool.unavailable("m")
    assert (status, code, exact) == (429, "plan_pool_cooling_down", True)
    assert 20 < retry_at-time.time() < 40


def test_error_classification():
    now = time.time()
    assert classify_error(429, b'{"error":{"code":"RateLimitExceeded.EndpointRPMExceeded"}}', {"retry-after":"5"}, now) == ("rate", now+5)
    assert classify_error(429, b'{"error":{"code":"QuotaExceeded","message":"You have exceeded the weekly usage quota"}}', {}, now) == ("quota", None)
    assert classify_error(401, b'{}', {}, now) == ("auth", None)


def test_unknown_reset_and_multiple_windows(store):
    a = store.add_account("agent", "key-agent", models=["m"])
    c = store.add_account("coding", "key-coding", models=["m"])
    pool = AccountPool(store)
    pool.update_result(store.account(a, True), "quota", time.time() + 25)
    pool.update_result(store.account(c, True), "quota", None)
    status, code, retry_at, exact = pool.unavailable("m")
    assert status == 429 and code == "plan_pool_cooling_down" and not exact
    assert 15 < retry_at - time.time() < 30
    pool.update_result(store.account(a, True), "quota", None)
    assert pool.unavailable("m")[1:] == ("plan_quota_exhausted", None, False)


@pytest.mark.asyncio
async def test_persisted_cooldown_survives_pool_restart(store):
    a = store.add_account("agent", "key-agent", models=["m"])
    pool = AccountPool(store)
    pool.update_result(store.account(a, True), "quota", time.time() + 30)
    assert await AccountPool(store).candidates("m") == []


@pytest.mark.asyncio
async def test_equal_accounts_rotate_and_recovery_probe_is_transient(store):
    first = store.add_account("agent", "key-first", models=["m"])
    second = store.add_account("coding", "key-second", models=["m"])
    pool = AccountPool(store)
    choices = [(await pool.candidates("m"))[0]["id"] for _ in range(2)]
    assert set(choices) == {first, second}
    store.update(first, cooldown_kind="quota", cooldown_until=time.time() - 1)
    account = store.account(first, True)
    assert await pool.reserve(account)
    status, code, retry_at, _ = pool.unavailable("m", first)
    assert status == 429 and code == "rate_limited" and retry_at > time.time()
    await pool.release(account)


@pytest.mark.asyncio
async def test_afp_uses_latest_exhausted_window_reset(store, monkeypatch):
    from gateway import quota
    a = store.add_account("agent", "key-agent", models=["m"])
    store.update(a, access_key="ak", secret_key="sk")
    now = time.time()
    async def fake_call(*args):
        return {"AFPFiveHour":{"Quota":"10","Used":"10","ResetTime":int((now+20)*1000)},
                "AFPWeekly":{"Quota":"100","Used":"100","ResetTime":int((now+90)*1000)},
                "AFPMonthly":{"Quota":"1000","Used":"1","ResetTime":int((now+900)*1000)}}
    monkeypatch.setattr(quota, "management_call", fake_call)
    await quota.refresh_account(store, store.account(a, True))
    result = store.account(a)
    assert result["cooldown_kind"] == "quota"
    assert 80 < result["cooldown_until"]-now < 100
    assert len(store.statistics(a, 1)["quota_history"]) == 3


@pytest.mark.asyncio
async def test_sync_failover_and_pinned_continuation(store, monkeypatch):
    monkeypatch.setenv("ARK_GATEWAY_ADMIN_PASSWORD", "admin-password-123")
    monkeypatch.setenv("ARK_GATEWAY_SERVICE_TOKEN", "service-token-123456789012345")
    monkeypatch.setenv("ARK_GATEWAY_ALLOW_UNCONFIGURED", "1")
    from gateway.main import create_app
    a = store.add_account("agent", "key-agent", models=["m"])
    c = store.add_account("coding", "key-coding", models=["m"])
    store.update(c, model_mapping={"m": "actual-model"})
    store.update(a, usage_json=json.dumps({"five": {"quota": 100, "used": 10, "reset_time": time.time()+60}}))
    calls = []
    def upstream(req):
        calls.append((req.url.path, req.headers["authorization"], json.loads(req.content) if req.content else {}))
        if "key-agent" in req.headers["authorization"]:
            return httpx.Response(429, json={"error":{"code":"QuotaExceeded.AgentPlanQuotaExceeded"}})
        return httpx.Response(200, json={"id":"resp_123", "object":"response", "model":"m", "output":[],
                                         "usage":{"input_tokens":7,"output_tokens":2}})
    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(store, upstream_client)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        headers = {"authorization":"Bearer service-token-123456789012345"}
        r = await client.post("/v1/responses", headers=headers, json={"model":"m", "input":"hello"})
        assert r.status_code == 200 and r.json()["id"] == "resp_123"
        assert len(calls) == 2 and calls[0][1] == "Bearer key-agent" and calls[1][1] == "Bearer key-coding"
        assert calls[1][2]["model"] == "actual-model"
        outcomes = {row["outcome"]: row["requests"] for row in store.statistics(None, 1)["daily"]}
        assert outcomes == {"quota": 1, "success": 1}
        assert sum(row["input_tokens"] for row in store.statistics(None, 1)["daily"]) == 7
        assert (await client.get("/api/statistics")).status_code == 401
        assert (await client.post("/api/login", json={"password":"admin-password-123"})).status_code == 200
        assert (await client.get("/api/statistics?days=1")).json()["daily"]
        assert (await client.get("/api/statistics?days=91")).status_code == 400
        r = await client.post("/v1/responses", headers=headers, json={"model":"m", "previous_response_id":"resp_123", "input":"more"})
        assert r.status_code == 200 and calls[-1][1] == "Bearer key-coding"
    await upstream_client.aclose()


@pytest.mark.asyncio
async def test_stream_preserves_events(store, monkeypatch):
    monkeypatch.setenv("ARK_GATEWAY_ADMIN_PASSWORD", "admin-password-123")
    monkeypatch.setenv("ARK_GATEWAY_SERVICE_TOKEN", "service-token-123456789012345")
    monkeypatch.setenv("ARK_GATEWAY_ALLOW_UNCONFIGURED", "1")
    from gateway.main import create_app
    store.add_account("coding", "key-coding", models=["m"])
    payload = b'event: response.created\ndata: {"response":{"id":"resp_stream"}}\n\nevent: response.completed\ndata: {"response":{"id":"resp_stream"}}\n\n'
    class EventStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield payload
    transport = httpx.MockTransport(lambda req: httpx.Response(200, stream=EventStream(), headers={"content-type":"text/event-stream"}))
    upstream_client = httpx.AsyncClient(transport=transport)
    app = create_app(store, upstream_client)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream("POST", "/v1/responses", headers={"authorization":"Bearer service-token-123456789012345"}, json={"model":"m","stream":True,"input":"hello"}) as r:
            chunks = [chunk async for chunk in r.aiter_bytes()]
            assert r.status_code == 200 and b"".join(chunks) == payload
        assert store.lookup_binding("resp_stream")
    await upstream_client.aclose()
