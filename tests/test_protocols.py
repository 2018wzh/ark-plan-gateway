import asyncio
import json
import time

import httpx
import pytest
from cryptography.fernet import Fernet

from gateway.protocols import SSEDecoder
from gateway.store import Store


AUTH = {"authorization": "Bearer test-service-token-1234567890"}
MODEL = "doubao-seed-2-1-turbo-260628"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("ARK_GATEWAY_ALLOW_UNCONFIGURED", "1")
    monkeypatch.setenv("ARK_GATEWAY_ADMIN_PASSWORD", "test-admin-password")
    monkeypatch.setenv("ARK_GATEWAY_SERVICE_TOKEN", AUTH["authorization"][7:])
    monkeypatch.delenv("ARK_AGENT_PLAN_KEYS", raising=False)
    monkeypatch.delenv("ARK_CODING_PLAN_KEYS", raising=False)
    from gateway.main import create_app
    store = Store(str(tmp_path / "test.db"), Fernet.generate_key().decode())
    store.set_pricing({"default": {}, "models": {"doubao-seed-2.1-turbo": {"input": 3, "output": 15}}})
    yield store, create_app
    store.close()


def sse(*events):
    return b"".join(b"data: " + (b"[DONE]" if e is None else json.dumps(e, ensure_ascii=False).encode()) + b"\r\n\r\n" for e in events)


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, payload, fail=False):
        self.payload = payload
        self.fail = fail
        self.closed = False

    async def __aiter__(self):
        for start in range(0, len(self.payload), 7):
            yield self.payload[start:start + 7]
        if self.fail:
            raise httpx.ReadError("test interruption")

    async def aclose(self):
        self.closed = True


def chat_events():
    def event(choices, **extra):
        return {"id": "chatcmpl_test", "object": "chat.completion.chunk", "created": 123, "model": MODEL, "choices": choices, **extra}
    return [
        event([{"index": 0, "delta": {"role": "assistant", "content": "你", "reasoning_content": "think "}, "finish_reason": None}]),
        event([{"index": 0, "delta": {"content": "好", "reasoning_content": "more", "tool_calls": [
            {"index": 1, "id": "call_b", "type": "function", "function": {"name": "second", "arguments": "{"}},
            {"index": 0, "id": "call_a", "type": "function", "function": {"name": "first", "arguments": "{"}}]}, "finish_reason": None}]),
        event([{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "\"a\":1}"}},
            {"index": 1, "function": {"arguments": "}"}}]}, "finish_reason": "tool_calls"},
            {"index": 1, "delta": {"content": "other"}, "finish_reason": "stop"}]),
        event([], usage={"prompt_tokens": 100, "completion_tokens": 63, "total_tokens": 163,
                         "completion_tokens_details": {"reasoning_tokens": 20}}), None]


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", ["agent", "coding"])
@pytest.mark.parametrize("stream", [False, True, None])
async def test_chat_stream_only_upstream_tools_and_usage(setup, plan, stream):
    store, create_app = setup
    account = store.add_account(plan, "test-key", models=["alias"])
    store.update(account, model_mapping={"alias": "doubao-seed-2.1-turbo"})
    payload = sse(*chat_events())
    source = BytesStream(payload)
    calls = []
    def upstream(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.url.path == f'/api/{"plan" if plan == "agent" else "coding"}/v3/chat/completions'
        assert body["stream"] is True  # Reproduces upstream's stream=true requirement.
        assert body["model"] == "doubao-seed-2.1-turbo"
        assert body["tools"] == [{"type": "function", "function": {"name": "first"}}]
        assert body["messages"][0]["role"] == "user"
        assert body["stream_options"] == {"include_usage": True}
        return httpx.Response(200, stream=source, headers={"content-type": "text/event-stream; charset=utf-8"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        app = create_app(store, remote)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            body = {"model": "alias", "messages": [{"role": "user", "content": "test"}],
                    "tools": [{"type": "function", "function": {"name": "first"}}]}
            if stream is not None:
                body["stream"] = stream
            response = await client.post('/v1/chat/completions', headers=AUTH, json=body)
            assert response.status_code == 200
            if stream:
                assert response.content == payload
            else:
                result = response.json()
                assert result["object"] == "chat.completion"
                assert result["usage"]["total_tokens"] == 163
                assert result["model"] == MODEL
                choice = result["choices"][0]
                assert choice["finish_reason"] == "tool_calls"
                assert choice["message"]["content"] == "你好"
                assert choice["message"]["reasoning_content"] == "think more"
                tools = choice["message"]["tool_calls"]
                assert [t["id"] for t in tools] == ["call_a", "call_b"]
                assert tools[0]["function"]["arguments"] == '{"a":1}'
                assert tools[1]["function"]["arguments"] == '{}'
                assert result["choices"][1]["message"]["content"] == "other"
    assert len(calls) == 1 and source.closed
    assert not store.lookup_binding("chatcmpl_test")
    assert app.state.pool.inflight[account] == 0
    row = store.statistics(None, 1)["model_daily"][0]
    assert row["requests"] == 1 and row["input_tokens"] == 100 and row["output_tokens"] == 63
    assert row["equivalent_cny"] == pytest.approx(.001245)


@pytest.mark.asyncio
async def test_models_list_and_validation(setup):
    store, create_app = setup
    store.add_account("agent", "a", models=["m", "alias"])
    store.add_account("coding", "b", models=["m"])
    for flag in ("expired", "auth_failed", "enabled"):
        account = store.add_account("agent", flag, models=[flag])
        store.update(account, **{flag: 0 if flag == "enabled" else 1})
    cooling = store.add_account("coding", "cooling", models=["cooling"])
    store.update(cooling, cooldown_kind="quota", cooldown_until=time.time() + 60)
    def no_call(request):
        pytest.fail("must not contact upstream")
    async with httpx.AsyncClient(transport=httpx.MockTransport(no_call)) as remote:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(store, remote)), base_url="http://test") as client:
            assert (await client.get('/v1/models')).status_code == 401
            result = (await client.get('/v1/models', headers=AUTH)).json()
            assert result["object"] == "list"
            assert [m['id'] for m in result['data']] == ['alias', 'cooling', 'm']
            assert all(isinstance(m['created'], int) and m['object'] == 'model' for m in result['data'])
            assert (await client.post('/v1/chat/completions', json={})).status_code == 401
            for body in ({}, [], {"model": "m"}, {"model": "m", "messages": []},
                         {"model": "m", "messages": [{}], "stream": "true"},
                         {"model": "m", "messages": [{}], "stream_options": []}):
                assert (await client.post('/v1/chat/completions', headers=AUTH, json=body)).status_code == 400
            response = await client.post('/v1/chat/completions', headers=AUTH,
                                         json={"model": "cooling", "messages": [{"role": "user", "content": "test"}]})
            assert response.status_code == 429
            assert response.json()['error']['code'] == 'plan_pool_cooling_down'
            assert int(response.headers['retry-after']) > 0
            assert 'metadata' not in response.json()['error']


@pytest.mark.asyncio
async def test_responses_sync_collects_terminal_object_and_binding(setup):
    store, create_app = setup
    account = store.add_account("coding", "key", models=["m"])
    terminal = {"id": "resp_test", "model": MODEL, "status": "completed", "output": [{"type": "function_call", "arguments": "{}"}],
                "usage": {"input_tokens": 100, "output_tokens": 63}}
    def upstream(request):
        assert json.loads(request.content)['stream'] is True
        return httpx.Response(200, stream=BytesStream(sse({"type": "response.created", "response": {"id": "resp_test"}},
                                                         {"type": "response.completed", "response": terminal})),
                              headers={"content-type": "text/event-stream"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(store, remote)), base_url='http://test') as client:
            response = await client.post('/v1/responses', headers=AUTH, json={"model": "m", "input": "test", "stream": False})
            assert response.status_code == 200 and response.json() == terminal
    assert store.lookup_binding('resp_test') == account
    assert store.statistics(None, 1)['model_daily'][0]['equivalent_cny'] == pytest.approx(.001245)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure", ['transport', 'missing_terminal', 'error_event'])
async def test_chat_stream_failure_never_replayed(setup, stream, failure):
    store, create_app = setup
    store.add_account('agent', 'a', models=['m'])
    store.add_account('coding', 'b', models=['m'])
    calls = []
    events = chat_events()[:1]
    if failure == 'error_event':
        events.append({'error': {'code': 'test_failure'}})
    source = BytesStream(sse(*events), fail=failure == 'transport')
    def upstream(request):
        calls.append(request)
        return httpx.Response(200, stream=source, headers={'content-type': 'text/event-stream'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        app = create_app(store, remote)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/v1/chat/completions', headers=AUTH,
                                         json={'model': 'm', 'messages': [{'role': 'user', 'content': 'test'}], 'stream': stream})
            assert response.status_code == (200 if stream else 502)
            assert b'error' in response.content
    assert len(calls) == 1 and source.closed
    assert all(count == 0 for count in app.state.pool.inflight.values())
    assert store.statistics(None, 1)['daily'][0]['outcome'] == 'stream_error'


def test_sse_multiline_event_and_utf8_boundaries():
    decoder = SSEDecoder()
    payload = 'event: response.completed\r\ndata: {"response":\r\ndata: {"id":"你好"}}\r\n\r\n'.encode()
    events = []
    for value in payload:
        events.extend(decoder.feed(bytes([value])))
    assert events == [{'type': 'response.completed', 'response': {'id': '你好'}}]


@pytest.mark.asyncio
@pytest.mark.parametrize('chat', [False, True])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('failure', [500, 502, 503, 504, httpx.ConnectError, httpx.ConnectTimeout])
async def test_transient_failure_switches_before_successful_generation(setup, chat, stream, failure):
    store, create_app = setup
    store.add_account('agent', 'a', models=['m'])
    store.add_account('coding', 'b', models=['m'])
    terminal = {'id': 'resp_recovered', 'status': 'completed', 'output': [], 'model': 'm'}
    payload = sse(*chat_events()) if chat else sse({'type': 'response.completed', 'response': terminal})
    source = BytesStream(payload)
    rejected = BytesStream(b'{"error":{"code":"InternalServiceError"}}')
    calls = []

    def upstream(request):
        calls.append(request)
        if len(calls) == 1:
            if isinstance(failure, int):
                return httpx.Response(failure, stream=rejected, headers={'retry-after': '600'})
            raise failure('connection failed', request=request)
        return httpx.Response(200, stream=source, headers={'content-type': 'text/event-stream'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        app = create_app(store, remote)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            path = '/v1/chat/completions' if chat else '/v1/responses'
            body = {'model': 'm', 'stream': stream, 'input': 'test', 'messages': [{'role': 'user', 'content': 'test'}]}
            response = await client.post(path, headers=AUTH, json=body)
            assert response.status_code == 200
            if stream:
                assert response.content == payload
            else:
                assert response.json()['id'] == ('chatcmpl_test' if chat else 'resp_recovered')
    assert len(calls) == 2
    assert calls[0].headers['authorization'] != calls[1].headers['authorization']
    assert source.closed
    if isinstance(failure, int):
        assert rejected.closed
        blocked = [a for a in store.accounts() if a['model_blocks']]
        assert len(blocked) == 1 and blocked[0]['model_blocks'][0]['retry_at'] >= time.time() + 590
    assert all(n == 0 for n in app.state.pool.inflight.values())
    assert not app.state.pool.probing


@pytest.mark.asyncio
@pytest.mark.parametrize('accounts,pinned,expected_calls', [(1, False, 1), (2, False, 2), (5, False, 3), (5, True, 1)])
@pytest.mark.parametrize('failure', [503, httpx.ConnectTimeout])
async def test_transient_failover_exhaustion_and_response_binding(setup, accounts, pinned, expected_calls, failure):
    store, create_app = setup
    ids = [store.add_account('agent', f'key-{i}', models=['m']) for i in range(accounts)]
    store.bind('resp_previous', ids[0])
    calls = []

    def upstream(request):
        calls.append(request)
        if isinstance(failure, int):
            return httpx.Response(failure, json={'error': {'code': 'InternalServiceError'}}, headers={'retry-after': '600'})
        raise failure('connect timeout', request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        app = create_app(store, remote)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            body = {'model': 'm', 'input': 'test'}
            if pinned:
                body['previous_response_id'] = 'resp_previous'
            response = await client.post('/v1/responses', headers=AUTH, json=body)
            assert response.status_code == (failure if isinstance(failure, int) else 503)
            code = 'upstream_failover_exhausted' if accounts == 5 and not pinned else 'upstream_unavailable'
            assert response.json()['error']['code'] == ('InternalServiceError' if isinstance(failure, int) else code)
            assert 'metadata' not in response.json()['error']
            if code == 'upstream_unavailable' or isinstance(failure, int):
                assert int(response.headers['retry-after']) > 0
    assert len(calls) == expected_calls
    assert len({r.headers['authorization'] for r in calls}) == expected_calls
    if pinned:
        assert calls[0].headers['authorization'] == 'Bearer key-0'
    assert sum(bool(a['model_blocks']) for a in store.accounts()) == expected_calls
    assert all(n == 0 for n in app.state.pool.inflight.values())
    assert not app.state.pool.probing


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError, httpx.PoolTimeout])
async def test_ambiguous_send_not_replayed_and_pool_timeout_not_quarantined(setup, failure):
    store, create_app = setup
    for key in ('a', 'b'):
        store.add_account('agent', key, models=['m'])
    calls = []

    def upstream(request):
        calls.append(request)
        raise failure('test failure', request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        app = create_app(store, remote)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/v1/responses', headers=AUTH, json={'model': 'm', 'input': 'test'})
            busy = failure is httpx.PoolTimeout
            assert response.status_code == (503 if busy else 502)
            assert 'metadata' not in response.json()['error']
    assert len(calls) == 1
    assert sum(bool(a['model_blocks']) for a in store.accounts()) == (0 if busy else 1)
    assert all(n == 0 for n in app.state.pool.inflight.values())


@pytest.mark.asyncio
async def test_chat_switches_only_after_explicit_rejection(setup):
    store, create_app = setup
    store.add_account('agent', 'a', models=['m'])
    store.add_account('coding', 'b', models=['m'])
    calls = []
    def upstream(request):
        calls.append(request.headers['authorization'])
        if len(calls) == 1:
            return httpx.Response(429, json={'error': {'code': 'QuotaExceeded.AgentPlan', 'reset_time': time.time() + 60}})
        return httpx.Response(200, stream=BytesStream(sse(*chat_events())), headers={'content-type': 'text/event-stream'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(store, remote)), base_url='http://test') as client:
            response = await client.post('/v1/chat/completions', headers=AUTH,
                                         json={'model': 'm', 'messages': [{'role': 'user', 'content': 'test'}]})
            assert response.status_code == 200
    assert len(calls) == len(set(calls)) == 2


@pytest.mark.asyncio
async def test_chat_disconnect_closes_upstream_and_releases_account(setup):
    store, create_app = setup
    account = store.add_account('agent', 'a', models=['m'])
    first_sent = asyncio.Event()
    closed = asyncio.Event()
    calls = []
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse(chat_events()[0])
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, stream=SlowStream(), headers={'content-type': 'text/event-stream'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        app = create_app(store, remote)
        body = json.dumps({'model': 'm', 'messages': [{'role': 'user', 'content': 'test'}], 'stream': True}).encode()
        received = False
        async def receive():
            nonlocal received
            if not received:
                received = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            await first_sent.wait()
            return {'type': 'http.disconnect'}

        async def send(message):
            if message['type'] == 'http.response.body' and message.get('body'):
                first_sent.set()

        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'}, 'http_version': '1.1',
                 'method': 'POST', 'scheme': 'http', 'path': '/v1/chat/completions', 'raw_path': b'/v1/chat/completions',
                 'query_string': b'', 'root_path': '', 'server': ('test', 80), 'client': ('test', 123),
                 'headers': [(b'authorization', AUTH['authorization'].encode()), (b'content-type', b'application/json')]}
        await asyncio.wait_for(app(scope, receive, send), timeout=2)
    assert first_sent.is_set() and closed.is_set() and len(calls) == 1
    assert app.state.pool.inflight[account] == 0
    assert store.statistics(None, 1)['daily'][0]['outcome'] == 'client_disconnected'


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_daily_quota_stream_error_cools_account_without_replay(setup, stream):
    store, create_app = setup
    store.add_account('agent', 'a', models=['m'])
    store.add_account('coding', 'b', models=['m'])
    calls = []
    reset = time.time() + 3600
    def upstream(request):
        calls.append(request)
        return httpx.Response(200, stream=BytesStream(sse(
            {'type': 'error', 'error': {'code': 'QuotaExceeded', 'message': 'daily quota exhausted', 'reset_time': reset}})),
            headers={'content-type': 'text/event-stream'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        app = create_app(store, remote)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/v1/responses', headers=AUTH, json={'model': 'm', 'input': 'test', 'stream': stream})
            assert response.status_code == (200 if stream else 502)
    assert len(calls) == 1
    cooling = [a for a in store.accounts() if a['cooldown_kind'] == 'quota']
    assert len(cooling) == 1 and cooling[0]['cooldown_until'] == reset
    assert len(await app.state.pool.candidates('m')) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('chat', [False, True])
@pytest.mark.parametrize('arguments', ['{"broken":', '', '{"number":NaN}', {'not': 'a string'}])
async def test_invalid_tool_history_never_reaches_provider(setup, chat, arguments):
    store, create_app = setup
    store.add_account('agent', 'a', models=['m'])
    store.add_account('coding', 'b', models=['m'])
    calls = []
    def upstream(request):
        calls.append(request)
        return httpx.Response(400)
    body = {'model': 'm'}
    if chat:
        body['messages'] = [{'role': 'assistant', 'tool_calls': [{'id': 'private-call', 'type': 'function', 'function': {'name': 'private-tool', 'arguments': arguments}}]}]
    else:
        body['input'] = [{'type': 'function_call', 'name': 'private-tool', 'call_id': 'private-call', 'arguments': arguments}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(store, remote)), base_url='http://test') as client:
            for _ in range(2):
                response = await client.post('/v1/chat/completions' if chat else '/v1/responses', headers=AUTH, json=body)
                assert response.status_code == 400
                error = response.json()['error']
                assert error['code'] == 'invalid_tool_arguments'
                assert 'metadata' not in error
                assert 'arguments' in error['param']
                assert 'private-' not in response.text
    assert calls == []
    assert all(a['cooldown_kind'] is None and not a['model_blocks'] for a in store.accounts())


@pytest.mark.asyncio
@pytest.mark.parametrize('chat', [False, True])
async def test_provider_invalid_parameter_is_not_retried_or_cooled(setup, chat):
    store, create_app = setup
    store.add_account('agent', 'a', models=['m'])
    store.add_account('coding', 'b', models=['m'])
    calls = []
    def upstream(request):
        calls.append(request)
        return httpx.Response(400, json={'error': {'code': 'InvalidParameter', 'message': 'PRIVATE_PROVIDER_DETAIL'}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(store, remote)), base_url='http://test') as client:
            for expected in (1, 2):
                response = await client.post('/v1/chat/completions' if chat else '/v1/responses', headers=AUTH,
                    json={'model':'m', 'input':'test', 'messages':[{'role':'user','content':'test'}]})
                assert response.status_code == 400 and len(calls) == expected
                assert response.json() == {'error': {'code': 'InvalidParameter', 'message': 'PRIVATE_PROVIDER_DETAIL'}}
    assert all(a['cooldown_kind'] is None and not a['model_blocks'] for a in store.accounts())


@pytest.mark.asyncio
@pytest.mark.parametrize('chat', [False, True])
@pytest.mark.parametrize('raw,content_type', [
    (b'{ "error": {"code":"MissingParameter", "message":"Missing input", "param":"input", "type":"invalid_request_error"}, "request_id":"original-id" }', 'application/json; charset=utf-8'),
    (b'upstream rejected parameters', 'text/plain'),
])
async def test_upstream_error_is_forwarded_without_rewriting(setup, chat, raw, content_type):
    store, create_app = setup
    store.add_account('agent', 'a', models=['m'])
    store.add_account('coding', 'b', models=['m'])
    calls = []
    def upstream(request):
        calls.append(request)
        return httpx.Response(400, content=raw, headers={'content-type': content_type, 'retry-after': '17', 'x-request-id': 'original-id'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(store, remote)), base_url='http://test') as client:
            response = await client.post('/v1/chat/completions' if chat else '/v1/responses', headers=AUTH,
                json={'model':'m', 'input':'test', 'messages':[{'role':'user','content':'test'}]})
    assert response.status_code == 400
    assert response.content == raw
    assert response.headers['content-type'] == content_type
    assert response.headers['retry-after'] == '17'
    assert response.headers['x-request-id'] == 'original-id'
    assert len(calls) == 1
    assert all(a['cooldown_kind'] is None and not a['model_blocks'] for a in store.accounts())


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_flat_stream_parameter_error_does_not_quarantine_model(setup, stream):
    store, create_app = setup
    aid = store.add_account('agent', 'a', models=['m'])
    calls = []
    def upstream(request):
        calls.append(request)
        return httpx.Response(200, stream=BytesStream(sse({'type':'error','code':'InvalidParameter.ToolArguments', 'message':'invalid arguments'})), headers={'content-type':'text/event-stream'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        app = create_app(store, remote)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/v1/responses', headers=AUTH, json={'model':'m','input':'test','stream':stream})
            assert response.status_code == (200 if stream else 502)
            if not stream:
                assert response.json() == {'type':'error','code':'InvalidParameter.ToolArguments', 'message':'invalid arguments'}
    assert len(calls) == 1
    assert store.account(aid)['model_blocks'] == []
    assert store.account(aid)['cooldown_kind'] is None


def test_valid_tool_history_is_not_rewritten():
    from gateway.protocols import invalid_tool_arguments
    arguments = '{"text":"你好", "nested":{"items":[1,2]}}'
    chat = {'messages':[{'role':'assistant','tool_calls':[{'type':'function','function':{'name':'f','arguments':arguments}}]}]}
    responses = {'input':[{'type':'function_call','arguments':arguments},{'type':'function_call_output','output':'plain text'}], 'previous_response_id':'resp_previous'}
    before = json.dumps([chat,responses])
    assert invalid_tool_arguments(chat, True) is None
    assert invalid_tool_arguments(responses, False) is None
    assert json.dumps([chat,responses]) == before


def test_codex_custom_tool_and_server_context_are_not_json_validated():
    from gateway.protocols import invalid_tool_arguments
    data = {'previous_response_id': 'resp_previous', 'input': [
        {'type': 'custom_tool_call', 'name': 'apply_patch', 'input': '*** Begin Patch'},
        {'type': 'custom_tool_call_output', 'call_id': 'call_a', 'output': 'failed to parse'},
        {'type': 'function_call_output', 'call_id': 'call_b', 'output': 'tool parse error'},
    ]}
    assert invalid_tool_arguments(data, False) is None


@pytest.mark.asyncio
async def test_responses_tool_argument_stream_is_forwarded_byte_for_byte(setup):
    store, create_app = setup
    store.add_account('agent', 'a', models=['m'])
    payload = sse(
        {'type':'response.function_call_arguments.delta','item_id':'fc_a','delta':'{"x":'},
        {'type':'response.function_call_arguments.delta','item_id':'fc_a','delta':'1}'},
        {'type':'response.function_call_arguments.done','item_id':'fc_a','arguments':'{"x":1}'},
        {'type':'response.completed','response':{'id':'resp_a','status':'completed','output':[]}},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,stream=BytesStream(payload),headers={'content-type':'text/event-stream'}))) as remote:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(store,remote)),base_url='http://test') as client:
            result = await client.post('/v1/responses',headers=AUTH,json={'model':'m','input':'test','stream':True})
            assert result.content == payload
