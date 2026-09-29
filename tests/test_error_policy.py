import json
import time
from email.utils import formatdate

import httpx
import pytest
from cryptography.fernet import Fernet

from gateway.errors import classify_error, retry_after, safe_error_code, short_backoff
from gateway.pool import AccountPool
from gateway.store import Store


def error(code, message="", **extra):
    return json.dumps({"error": {"code": code, "message": message, **extra}}).encode()


@pytest.mark.parametrize("status,code,message,kind", [
    (401, "AuthenticationError", "", "auth"), (401, "MCPInvalidCredential", "", "request"),
    (401, "InvalidAccountStatus", "", "account"),
    (403, "OperationDenied.ServiceNotOpen", "", "model"),
    (403, "OperationDenied.ServiceOverdue", "", "account"),
    (403, "AccountOverdueError", "", "account"), (403, "AccessDenied", "", "permission"),
    (403, "QuotaExceeded.DoubaoSearchFreeQuotaExceeded", "", "permission"),
    (403, "OperationDenied.TosAccessDenied", "", "permission"),
    (404, "UnsupportedModel", "", "model"), (404, "ModelNotOpen", "", "model"),
    (404, "InvalidEndpointOrModel.NotFound", "", "model"),
    (404, "NotFound.File", "", "request"), (400, "InvalidParameter", "", "request"),
    (429, "AccountRateLimitExceeded", "", "rate"),
    (429, "RateLimitExceeded.EndpointTPMExceeded", "", "model_rate"),
    (429, "ModelAccountIpmRateLimitExceeded", "", "model_rate"),
    (429, "ServerOverloaded", "", "overload"), (429, "RequestBurstTooFast", "", "overload"),
    (429, "InflightBatchsizeExceeded", "", "rate"), (429, "SetLimitExceeded", "", "model_limit"),
    (429, "QuotaExceeded", "Your account has exhausted its free trial quota.", "model_limit"),
    (429, "QuotaExceeded", "The request has exceeded the quota.", "rate"),
    (429, "QuotaExceeded", "You have exceeded the weekly usage quota.", "quota"),
    (429, "QuotaExceeded.AgentPlanQuotaExceeded", "", "quota"),
    (429, "FutureUnknownCode", "", "rate"), (429, "SessionQuotaExceeded", "", "request"),
    (500, "InternalServiceError", "", "server"), (503, "unknown", "", "server"),
    (424, "UpstreamUnavailable", "", "request"),
])
def test_documented_error_policy(status, code, message, kind):
    assert classify_error(status, error(code, message), {}, time.time())[0] == kind


def test_retry_after_and_jitter(monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr('gateway.errors.random.uniform', lambda low, high: high)
    assert short_backoff({}, now, 0) == now + 10
    assert short_backoff({}, now, 1) == now + 20
    assert short_backoff({}, now, 100) == now + 300
    assert short_backoff({'retry-after': '600'}, now, 0) == now + 600
    assert short_backoff({'retry-after': formatdate(now + 900, usegmt=True)}, now, 0) == now + 900
    for value in ('NaN', 'Infinity', '1e300', 'bad'):
        assert retry_after({'retry-after': value}, now) is None
    assert retry_after({'retry-after': '-1'}, now) == now + 1


def test_safe_error_metadata_and_untrusted_reset():
    now = time.time()
    assert safe_error_code(error('secret-api-key', 'private prompt')) == 'UnknownUpstreamError'
    assert safe_error_code(error('RateLimitExceeded.EndpointRPMExceeded', 'private prompt')) == 'RateLimitExceeded.EndpointRPMExceeded'
    for reset in (now - 1, float('nan'), 1e300, '2099-01-01T00:00:00'):
        assert classify_error(429, error('QuotaExceeded.AgentPlanQuotaExceeded', reset_time=reset), {}, now) == ('quota', None)
    assert classify_error(429, b'not-json', {}, now) == ('rate', None)


@pytest.fixture
def store(tmp_path):
    db = Store(str(tmp_path/'policy.db'), Fernet.generate_key().decode())
    yield db
    db.close()


@pytest.mark.asyncio
async def test_model_scope_persistence_and_aliases(store):
    aid = store.add_account('agent', 'a', models=['m', 'alias', 'other'])
    store.update(aid, model_mapping={'alias': 'm'})
    pool = AccountPool(store)
    pool.update_result(store.account(aid), 'model', None, 'alias', 'UnsupportedModel')
    assert not await pool.candidates('m') and not await pool.candidates('alias')
    assert (await pool.candidates('other'))[0]['id'] == aid
    assert store.account(aid)['auth_failed'] == 0
    # A new pool reads durable state, not an in-memory route cache.
    assert not await AccountPool(store).candidates('m')
    assert pool.unavailable('m')[1] == 'model_unavailable'
    store.clear_model_blocks(aid)
    assert await pool.candidates('m')


@pytest.mark.asyncio
async def test_blocks_and_backoff_survive_database_reopen(tmp_path):
    path = str(tmp_path / 'restart.db')
    key = Fernet.generate_key().decode()
    store = Store(path, key)
    aid = store.add_account('agent', 'a', models=['m', 'other'])
    pool = AccountPool(store)
    pool.update_result(store.account(aid), 'model', None, 'm', 'UnsupportedModel')
    pool.update_result(store.account(aid), 'rate', time.time() + 600, 'other')
    store.close()
    store = Store(path, key)
    assert store.account(aid)['cooldown_failures'] == 1
    assert not await AccountPool(store).candidates('m')
    assert not await AccountPool(store).candidates('other')
    store.close()


@pytest.mark.asyncio
async def test_shared_rate_limit_and_single_recovery_probe(store):
    a = store.add_account('agent', 'a', models=['m', 'other'])
    b = store.add_account('agent', 'b', models=['m', 'other'])
    store.update(b, quota_group=a)
    pool = AccountPool(store)
    pool.update_result(store.account(a), 'model_rate', time.time() + 600, 'm', 'ModelAccountRpmRateLimitExceeded')
    assert not await pool.candidates('m') and len(await pool.candidates('other')) == 2
    assert pool.unavailable('m')[2] > time.time() + 590
    for aid in (a, b):
        store.block_model(aid, 'm', 'model_rate', time.time() - 1, 'ModelAccountRpmRateLimitExceeded', 2)
    first = store.account(a)
    assert await pool.reserve(first, 'm')
    assert not await pool.reserve(store.account(b), 'm')
    pool.update_result(first, 'ok', None, 'm')
    await pool.release(first)
    assert store.account(a)['model_blocks'] == []
    assert await pool.reserve(store.account(b), 'm')


@pytest.mark.asyncio
async def test_provider_overload_does_not_cycle_same_plan_or_other_models(store):
    a = store.add_account('agent', 'a', models=['m', 'other'])
    b = store.add_account('agent', 'b', models=['m', 'other'])
    c = store.add_account('coding', 'c', models=['m'])
    pool = AccountPool(store)
    pool.update_result(store.account(a), 'overload', None, 'm', 'ServerOverloaded')
    assert [a['id'] for a in await pool.candidates('m')] == [c]
    assert len(await pool.candidates('other')) == 2
    for aid in (a, b):
        store.block_model(aid, 'm', 'overload', time.time() - 1, 'ServerOverloaded', 1)
    first = store.account(a)
    assert await pool.reserve(first, 'm')
    assert not await pool.reserve(store.account(b), 'm')
    await pool.release(first)


@pytest.mark.asyncio
async def test_late_success_cannot_erase_new_cooldown(store):
    aid = store.add_account('agent', 'a', models=['m'])
    pool = AccountPool(store)
    old = store.account(aid)
    assert await pool.reserve(old, 'm')
    pool.update_result(store.account(aid), 'rate', None, 'm')
    pool.update_result(old, 'ok', None, 'm')
    await pool.release(old)
    assert not await pool.candidates('m')
    assert store.account(aid)['cooldown_kind'] == 'rate'


def test_concurrent_failure_snapshots_do_not_reset_backoff(store):
    aid = store.add_account('agent', 'a', models=['m'])
    stale = store.account(aid)
    pool = AccountPool(store)
    for _ in range(3):
        pool.update_result(stale, 'server', None, 'm')
    assert store.account(aid)['model_blocks'][0]['failures'] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('status,code,expected_calls,expected_status', [
    (403, 'AccessDenied', 1, 403), (403, 'OperationDenied.ServiceNotOpen', 2, 200),
    (429, 'InflightBatchsizeExceeded', 2, 200), (429, 'UnknownCode', 2, 200),
    (404, 'UnsupportedModel', 2, 200), (500, 'InternalServiceError', 1, 502),
    (503, 'ServerOverloaded', 1, 502), (401, 'MCPInvalidCredential', 1, 401),
])
async def test_http_policy_and_metadata(store, monkeypatch, status, code, expected_calls, expected_status):
    monkeypatch.setenv('ARK_GATEWAY_ALLOW_UNCONFIGURED', '1')
    monkeypatch.setenv('ARK_GATEWAY_ADMIN_PASSWORD', 'test-password-123')
    monkeypatch.setenv('ARK_GATEWAY_SERVICE_TOKEN', 'service-token-123456789012345')
    from gateway.main import create_app
    store.add_account('agent', 'a', models=['m', 'other'])
    store.add_account('coding', 'b', models=['m', 'other'])
    calls = []
    def upstream(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(status, content=error(code, 'DO_NOT_EXPOSE_PRIVATE_ERROR'))
        return httpx.Response(200, json={'id': 'resp_ok', 'model': 'm', 'usage': {'input_tokens': 1, 'output_tokens': 1}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        app = create_app(store, remote)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            r = await client.post('/v1/responses', headers={'authorization': 'Bearer service-token-123456789012345'}, json={'model': 'm', 'input': 'test'})
            assert r.status_code == expected_status
            assert 'DO_NOT_EXPOSE' not in r.text
            if expected_status != 200:
                assert r.json()['error']['metadata']['upstream_code'] == code
                assert r.json()['error']['metadata']['retryable'] is False
    assert len(calls) == expected_calls
    assert all(a['auth_failed'] == 0 for a in store.accounts())
    assert len(await app.state.pool.candidates('other')) >= 1


@pytest.mark.asyncio
async def test_admin_resume_and_model_discovery(store, monkeypatch):
    monkeypatch.setenv('ARK_GATEWAY_ALLOW_UNCONFIGURED', '1')
    monkeypatch.setenv('ARK_GATEWAY_ADMIN_PASSWORD', 'test-password-123')
    monkeypatch.setenv('ARK_GATEWAY_SERVICE_TOKEN', 'service-token-123456789012345')
    from gateway.main import create_app
    aid = store.add_account('agent', 'a', models=['m', 'other'])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200))) as remote:
        app = create_app(store, remote)
        app.state.pool.update_result(store.account(aid), 'model', None, 'm', 'UnsupportedModel')
        headers = {'authorization': 'Bearer service-token-123456789012345'}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert [m['id'] for m in (await client.get('/v1/models', headers=headers)).json()['data']] == ['other']
            failed = await client.post('/v1/responses', headers=headers, json={'model':'m','input':'test'})
            assert failed.json()['error']['metadata']['blocking_codes'] == ['UnsupportedModel']
            assert failed.json()['error']['metadata']['requires_admin'] is True
            await client.post('/api/login', json={'password': 'test-password-123'})
            edited = await client.patch(f'/api/accounts/{aid}', json={'label': 'renamed', 'models': ['m','other'], 'model_mapping': {}, 'api_key': None})
            assert edited.status_code == 200 and len(edited.json()['model_blocks']) == 1
            assert (await client.post(f'/api/accounts/{aid}/resume')).status_code == 200
            assert [m['id'] for m in (await client.get('/v1/models', headers=headers)).json()['data']] == ['m', 'other']
            assert store.account(aid)['model_blocks'] == []


@pytest.mark.asyncio
async def test_quota_refresh_does_not_remove_account_hold(store, monkeypatch):
    from gateway import quota
    aid = store.add_account('agent', 'a', models=['m'])
    store.update(aid, access_key='ak', secret_key='sk', cooldown_kind='account', cooldown_code='AccountOverdueError')
    async def usage(*args):
        return {'AFPFiveHour': {'Quota': 10, 'Used': 10, 'ResetTime': int((time.time()+60)*1000)}}
    monkeypatch.setattr(quota, 'management_call', usage)
    await quota.refresh_account(store, store.account(aid, True))
    assert store.account(aid)['cooldown_kind'] == 'account'


@pytest.mark.parametrize('message', ['daily quota exceeded', 'per-day quota exhausted', '每日额度耗尽'])
def test_daily_quota_classification(message):
    assert classify_error(429, error('QuotaExceeded', message), {'retry-after': '3600'}, 1000) == ('quota', 4600)
    assert classify_error(429, error('QuotaExceeded', message), {}, 1000) == ('quota', None)
    assert classify_error(429, error('QuotaExceeded', 'daily free trial quota exceeded'), {}, 1000)[0] == 'model_limit'


@pytest.mark.asyncio
@pytest.mark.parametrize('plan', ['agent', 'coding'])
async def test_refresh_preserves_future_quota_hold_and_partial_windows(store, monkeypatch, plan):
    from gateway import quota
    now = time.time()
    aid = store.add_account(plan, 'a', models=['m'])
    window = 'AFPDaily' if plan == 'agent' else 'weekly'
    store.update(aid, access_key='ak', secret_key='sk', cooldown_kind='quota', cooldown_until=now+3600)
    result = ({'AFPDaily': {'Quota': 100, 'Used': 99, 'ResetTime': int((now+3600)*1000)}} if plan == 'agent'
              else {'QuotaUsage': [{'Level': 'weekly', 'Percent': 99, 'ResetTimestamp': now+3600}]})
    async def usage(*args):
        return result
    monkeypatch.setattr(quota, 'management_call', usage)
    await quota.refresh_account(store, store.account(aid, True))
    assert store.account(aid)['cooldown_until'] == now+3600
    store.update(aid, cooldown_until=now-1)
    await quota.refresh_account(store, store.account(aid, True))
    assert store.account(aid)['cooldown_kind'] is None
    store.update(aid, usage_json=json.dumps({window: {'quota': 100, 'used': 100, 'reset_time': now+7200}}))
    result = ({'AFPFiveHour': {'Quota': 100, 'Used': 1, 'ResetTime': int((now+3600)*1000)}} if plan == 'agent'
              else {'QuotaUsage': [{'Level': 'session', 'Percent': 1, 'ResetTimestamp': now+3600}]})
    for _ in range(2):
        await quota.refresh_account(store, store.account(aid, True))
        assert store.account(aid)['cooldown_kind'] == 'quota'
        assert store.account(aid)['cooldown_until'] == now+7200
        assert store.account(aid)['usage'][window]['used'] == 100


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/v1/responses', '/v1/chat/completions'])
@pytest.mark.parametrize('known', [True, False])
async def test_daily_exhaustion_enters_cooldown_and_stops_repeat_calls(store, monkeypatch, path, known):
    monkeypatch.setenv('ARK_GATEWAY_ALLOW_UNCONFIGURED', '1')
    monkeypatch.setenv('ARK_GATEWAY_ADMIN_PASSWORD', 'test-password-123')
    monkeypatch.setenv('ARK_GATEWAY_SERVICE_TOKEN', 'service-token-123456789012345')
    from gateway.main import create_app
    for plan in ('agent', 'coding'):
        store.add_account(plan, plan, models=['m'])
    calls = []
    def upstream(request):
        calls.append(request)
        return httpx.Response(429, content=error('QuotaExceeded', 'daily quota exhausted'),
                              headers={'retry-after': str(3600 if len(calls) == 1 else 7200)} if known else {})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        app = create_app(store, remote)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            for _ in range(2):
                response = await client.post(path, headers={'authorization': 'Bearer service-token-123456789012345'},
                                             json={'model': 'm', 'input': 'test', 'messages': [{'role': 'user', 'content': 'test'}]})
                assert response.status_code == 429
                assert response.json()['error']['code'] == ('plan_pool_cooling_down' if known else 'plan_quota_exhausted')
                if known:
                    assert 3598 <= int(response.headers['retry-after']) <= 3600
                    assert response.json()['error']['metadata']['reset_time_known'] is True
                else:
                    assert 'retry-after' not in response.headers
    assert len(calls) == 2
    assert all(a['cooldown_kind'] == 'quota' for a in store.accounts())


@pytest.mark.asyncio
@pytest.mark.parametrize('reset', [None, 0, -1])
async def test_exhausted_daily_without_valid_reset_never_probes(store, monkeypatch, reset):
    from gateway import quota
    aid = store.add_account('agent', 'a', models=['m'])
    store.update(aid, access_key='ak', secret_key='sk')
    async def usage(*args):
        return {'AFPDaily': {'Quota': 100, 'Used': 100, 'ResetTime': reset}}
    monkeypatch.setattr(quota, 'management_call', usage)
    await quota.refresh_account(store, store.account(aid, True))
    account = store.account(aid)
    assert account['quota_error'] is None
    assert account['cooldown_kind'] == 'quota' and account['cooldown_until'] is None
    assert not await AccountPool(store).candidates('m')
