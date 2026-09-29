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
    store.record_request(first, "success", 1200, "m", 12, 3)
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


def test_pricing_model_override_default_and_missing_rate(store):
    account = store.add_account("coding", "key-coding", models=["m", "n"])
    store.record_request(account, "success", 10, "m", 1_000_000, 500_000)
    store.record_request(account, "success", 10, "n", 200_000, 300_000)
    store.set_pricing({"default": {"input": 2}, "models": {"m": {"input": 4, "output": 8}}})
    row = store.statistics(account, 1)["daily"][0]
    assert row["equivalent_cny"] == pytest.approx(8.4)
    assert row["unpriced_output_tokens"] == 300_000
    assert row["unpriced_input_tokens"] == 0


def test_model_daily_groups_accounts_and_outcomes_without_double_counting(store, monkeypatch):
    first = store.add_account("agent", "first", models=["m", "n"])
    second = store.add_account("coding", "second", models=["m"])
    store.set_pricing({"default": {"input": 2}, "models": {"m": {"output": 4}}})
    now = time.time()
    monkeypatch.setattr("gateway.store.time.time", lambda: now - 86400)
    store.record_request(first, "success", 10, "m", 100, 20)
    monkeypatch.setattr("gateway.store.time.time", lambda: now)
    store.record_request(first, "success", 10, "m", 200, 30)
    store.record_request(first, "stream_error", 10, "m", 50, 5)
    store.record_request(second, "success", 10, "m", 300, 40)
    store.record_request(first, "success", 10, "n", 400, 60)
    stats = store.statistics(None, 7)
    rows = stats["model_daily"]
    assert len(rows) == 3
    today_m = next(row for row in rows if row["model"] == "m" and row["input_tokens"] == 550)
    assert today_m["output_tokens"] == 75
    assert today_m["equivalent_cny"] == pytest.approx(0.0014)
    assert sum(row["input_tokens"] for row in rows) == sum(row["input_tokens"] for row in stats["daily"]) == 1050
    assert sum(row["unpriced_output_tokens"] for row in rows) == 60
    assert sum(row["input_tokens"] for row in store.statistics(first, 1)["model_daily"]) == 650


def test_legacy_statistics_migrate_without_losing_tokens(tmp_path):
    path = str(tmp_path / "legacy.db")
    key = Fernet.generate_key().decode()
    store = Store(path, key)
    store.db.execute("DROP TABLE request_daily")
    store.db.execute("""CREATE TABLE request_daily (day TEXT, account_id TEXT, outcome TEXT,
        requests INTEGER, input_tokens INTEGER, output_tokens INTEGER, latency_ms INTEGER,
        PRIMARY KEY(day,account_id,outcome))""")
    store.db.execute("INSERT INTO request_daily VALUES (date('now'),'a','success',1,7,3,12)")
    store.db.commit()
    store.close()
    store = Store(path, key)
    row = store.statistics(None, 1)["daily"][0]
    assert row["input_tokens"] == 7 and row["output_tokens"] == 3
    assert row["unpriced_input_tokens"] == 7
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
async def test_coding_usage_percent_and_shared_cooldown(store, monkeypatch):
    from gateway import quota
    first = store.add_account("coding", "coding-key-one", models=["m"])
    second = store.add_account("coding", "coding-key-two", models=["m"])
    store.set_quota_group(second, first)
    store.update(first, access_key="ak", secret_key="sk")
    now = time.time()
    async def fake_call(action, ak, sk, body):
        assert action == "GetCodingPlanUsage"
        return {"Status":"Running", "QuotaUsage":[
            {"Level":"session","Percent":100,"ResetTimestamp":int(now+30)},
            {"Level":"weekly","Percent":100,"ResetTimestamp":int(now+90)},
            {"Level":"monthly","Percent":20,"ResetTimestamp":-1}]}
    monkeypatch.setattr(quota, "management_call", fake_call)
    await quota.refresh_account(store, store.account(first, True))
    for account_id in (first, second):
        row = store.account(account_id)
        assert row["cooldown_kind"] == "quota"
        assert 80 < row["cooldown_until"]-now < 100
        assert row["usage"]["monthly"]["unit"] == "percent"
    assert len(store.statistics(None, 1)["quota_current"]) == 3


def test_coding_usage_rejects_invalid_percent():
    from gateway.quota import parse_coding_usage
    result = parse_coding_usage({"QuotaUsage":[
        {"Level":"session","Percent":-1,"ResetTimestamp":1},
        {"Level":"weekly","Percent":125,"ResetTimestamp":1},
        {"Level":"monthly","Percent":42.5,"ResetTimestamp":-1}]})
    assert result == {"monthly":{"quota":100.0,"used":42.5,"reset_time":None,"unit":"percent"}}


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
        assert (await client.put("/api/pricing", json={"default":{"input":2,"output":4},"models":{"m":{"input":3}}})).status_code == 200
        assert (await client.get("/api/pricing")).json()["models"]["m"]["input"] == 3
        priced = next(row for row in store.statistics(None, 1)["daily"] if row["outcome"] == "success")
        assert priced["equivalent_cny"] == pytest.approx(29 / 1_000_000)
        assert (await client.put("/api/pricing", json={"default":{"input":-1},"models":{}})).status_code == 422
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


@pytest.mark.asyncio
async def test_account_editor_credentials_and_duplicate_keys(store, monkeypatch):
    monkeypatch.setenv("ARK_GATEWAY_ADMIN_PASSWORD", "admin-password-123")
    monkeypatch.setenv("ARK_GATEWAY_SERVICE_TOKEN", "service-token-123456789012345")
    monkeypatch.setenv("ARK_GATEWAY_ALLOW_UNCONFIGURED", "1")
    from gateway.main import create_app
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as upstream:
        app = create_app(store, upstream)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/api/login", json={"password":"admin-password-123"})
            body = {"plan":"coding", "label":"first", "api_key":"test-first-key", "models":["m"], "access_key":"test-ak", "secret_key":"test-sk"}
            response = await client.post("/api/accounts", json=body)
            assert response.status_code == 200
            account = response.json()
            assert account["has_ak_sk"] is True
            assert "test-ak" not in response.text and "test-sk" not in response.text and body["api_key"] not in response.text
            assert store.account(account["id"], True)["access_key"] == "test-ak"
            assert (await client.post("/api/accounts", json={**body, "label":"overwrite"})).status_code == 409
            assert store.account(account["id"])["label"] == "first"
            assert (await client.post("/api/accounts", json={**body, "api_key":"incomplete-key", "secret_key":None})).status_code == 400
            assert len(store.accounts()) == 1
            second = store.add_account("coding", "test-second-key", "second", ["m"])
            assert (await client.patch(f"/api/accounts/{second}", json={"label":"changed", "api_key":body["api_key"]})).status_code == 409
            assert store.account(second)["label"] == "second"
            assert (await client.patch(f"/api/accounts/{second}", json={"label":"updated"})).status_code == 200
