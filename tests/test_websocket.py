import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import uvicorn
from websockets.sync.client import connect

from gateway.store import Store

AUTH = {'authorization': 'Bearer websocket-test-service-token-123456'}


class EventStream(httpx.AsyncByteStream):
    def __init__(self, events, block=False):
        self.payload = b''.join(b'data: ' + json.dumps(e, ensure_ascii=False).encode() + b'\n\n' for e in events)
        self.closed = threading.Event()
        self.block = block

    async def __aiter__(self):
        for offset in range(0, len(self.payload), 7):
            yield self.payload[offset:offset + 7]
        if self.block:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed.set()


def completed(response_id='resp_one', output=None):
    return {'type': 'response.completed', 'response': {'id': response_id, 'model': 'm',
            'status': 'completed', 'output': output or [],
            'usage': {'input_tokens': 3, 'output_tokens': 2, 'total_tokens': 5}}}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('ARK_GATEWAY_ALLOW_UNCONFIGURED', '1')
    monkeypatch.setenv('ARK_GATEWAY_ADMIN_PASSWORD', 'websocket-test-admin-password')
    monkeypatch.setenv('ARK_GATEWAY_SERVICE_TOKEN', AUTH['authorization'][7:])
    monkeypatch.delenv('ARK_AGENT_PLAN_KEYS', raising=False)
    monkeypatch.delenv('ARK_CODING_PLAN_KEYS', raising=False)
    from gateway.main import create_app
    store = Store(str(tmp_path / 'socket.db'), Fernet.generate_key().decode())
    clients = []

    def create(upstream):
        remote = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        clients.append(remote)
        app = create_app(store, remote)
        return app, TestClient(app)

    yield store, create
    for remote in clients:
        asyncio.run(remote.aclose())
    store.close()


@pytest.mark.parametrize('stream_error', [False, True])
@pytest.mark.parametrize('plan', ['coding', 'agent'])
def test_websocket_upstream_errors_preserve_payload(setup, stream_error, plan):
    store, create = setup
    store.add_account(plan, 'a', models=['m'])
    store.add_account(plan, 'b', models=['m'])
    calls = []
    payload = {'error': {'code': 'MissingParameter', 'message': 'Missing input', 'param': 'input', 'type': 'BadRequest'}}
    event = {'type': 'error', 'code': 'MissingParameter', 'message': 'Missing input', 'param': 'input'}
    def upstream(request):
        calls.append(request)
        if stream_error:
            return httpx.Response(200, stream=EventStream([event]), headers={'content-type': 'text/event-stream'})
        return httpx.Response(400, json=payload)
    _, client = create(upstream)
    with client.websocket_connect('/v1/responses', headers=dict(AUTH)) as ws:
        for _ in range(2):
            ws.send_json({'type':'response.create', 'model':'m', 'input':'test'})
            assert ws.receive_json() == (event if stream_error else {'type':'error', 'status':400, **payload})
    assert len(calls) == 2
    assert all(a['cooldown_kind'] is None and not a['model_blocks'] for a in store.accounts())


def test_websocket_authentication_before_upgrade(setup):
    store, create = setup
    calls = []
    _, client = create(lambda r: calls.append(r))
    for headers in ({}, {'authorization': 'Bearer wrong'}):
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect('/v1/responses', headers=headers):
                pass
        assert denied.value.code == 1008
    assert not calls


def test_websocket_frames_preserve_custom_tools_and_utf8(setup):
    store, create = setup
    store.add_account('agent', 'test-key', models=['m'])
    events = [
        {'type': 'response.created', 'response': {'id': 'resp_one'}},
        {'type': 'response.output_text.delta', 'delta': '你好'},
        {'type': 'response.custom_tool_call_input.delta', 'delta': 'print("你好")'},
        completed(),
    ]
    source = EventStream(events)
    calls = []

    def upstream(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, stream=source, headers={'content-type': 'text/event-stream'})

    app, client = create(upstream)
    tools = [{'type': 'custom', 'name': 'python', 'format': {'type': 'text'}}]
    with client.websocket_connect('/v1/responses', headers=dict(AUTH)) as ws:
        ws.send_json({'type': 'response.create', 'model': 'm', 'input': 'test', 'tools': tools})
        assert [ws.receive_json() for _ in events] == events
    assert calls == [{'model': 'm', 'input': 'test', 'tools': tools, 'stream': True}]
    assert source.closed.wait(1)
    assert store.lookup_binding('resp_one')
    assert all(n == 0 for n in app.state.pool.inflight.values())


def test_prewarm_and_store_false_incremental_tool_continuation(setup):
    store, create = setup
    for plan, key in [('agent', 'first-key'), ('coding', 'second-key')]:
        store.add_account(plan, key, models=['m'])
    output = [{'type': 'function_call', 'call_id': 'call_one', 'name': 'tool', 'arguments': '{}'}]
    calls = []

    def upstream(request):
        calls.append((request.headers['authorization'], json.loads(request.content)))
        return httpx.Response(200, stream=EventStream([completed(f'resp_{len(calls)}', output if len(calls) == 1 else [])]),
                              headers={'content-type': 'text/event-stream'})

    app, client = create(upstream)
    initial = [{'role': 'system', 'content': 'instructions'}]
    user = {'role': 'user', 'content': 'test'}
    tool = {'type': 'function_call_output', 'call_id': 'call_one', 'output': 'done'}
    with client.websocket_connect('/v1/responses', headers=dict(AUTH)) as ws:
        ws.send_json({'type': 'response.create', 'model': 'm', 'input': initial, 'generate': False, 'store': False})
        assert ws.receive_json()['type'] == 'response.created'
        warmup = ws.receive_json()
        assert warmup['type'] == 'response.completed' and warmup['response']['usage']['total_tokens'] == 0
        assert not calls
        ws.send_json({'type': 'response.create', 'model': 'm', 'input': [user],
                      'previous_response_id': warmup['response']['id'], 'store': False})
        assert ws.receive_json()['response']['id'] == 'resp_1'
        ws.send_json({'type': 'response.create', 'model': 'm', 'input': [tool],
                      'previous_response_id': 'resp_1', 'store': False})
        assert ws.receive_json()['response']['id'] == 'resp_2'
    assert len(calls) == 2 and calls[0][0] == calls[1][0]
    assert calls[0][1]['input'] == initial + [user]
    assert calls[1][1]['input'] == initial + [user] + output + [tool]
    assert all('previous_response_id' not in body and 'generate' not in body for _, body in calls)
    assert all(n == 0 for n in app.state.pool.inflight.values())


def test_websocket_failover_and_request_scoped_errors(setup):
    store, create = setup
    for plan in ('agent', 'coding'):
        store.add_account(plan, plan, models=['m'])
    calls = []

    def upstream(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(503, json={'error': {'code': 'ServerOverloaded'}})
        return httpx.Response(200, stream=EventStream([completed()]), headers={'content-type': 'text/event-stream'})

    _, client = create(upstream)
    with client.websocket_connect('/v1/responses', headers=dict(AUTH)) as ws:
        ws.send_json({'type': 'response.create', 'model': 'm', 'stream_id': 'main',
                      'input': [{'type': 'function_call', 'arguments': '{bad'}]})
        error = ws.receive_json()
        assert error['type'] == 'error' and error['status'] == 400 and error['stream_id'] == 'main'
        assert error['error']['code'] == 'Gateway.invalid_tool_arguments' and not calls
        ws.send_json({'type': 'response.create', 'model': 'm', 'input': 'test', 'stream_id': 'main'})
        assert ws.receive_json()['stream_id'] == 'main'
    assert len(calls) == 2 and all('stream_id' not in body for body in calls)


@pytest.mark.parametrize('event,code', [
    ({'type': 'other'}, 'unsupported_websocket_event'),
    ({'type': 'response.create', 'model': 'm', 'stream_id': ''}, 'invalid_stream_id'),
    ({'type': 'response.create', 'model': 'm', 'generate': 'false'}, 'generate_must_be_boolean'),
    ({'type': 'response.create', 'model': 'm', 'previous_response_id': 'resp_ws_warmup_missing'}, 'previous_response_not_found'),
    ({'type': 'response.create', 'model': 'm', 'background': True}, 'background_not_supported'),
])
def test_invalid_frames_do_not_call_upstream(setup, event, code):
    _, create = setup
    calls = []
    _, client = create(lambda r: calls.append(r))
    with client.websocket_connect('/v1/responses', headers=dict(AUTH)) as ws:
        ws.send_json(event)
        error = ws.receive_json()
        assert error['error']['code'] == (code if code in ('invalid_stream_id','previous_response_not_found') else 'Gateway.' + code)
        assert 'param' in error['error']
        if code in ('invalid_stream_id','previous_response_not_found'):
            assert error['error']['type'] == 'invalid_request_error'
        ws.send_text('{invalid json')
        assert ws.receive_json()['error']['code'] == 'Gateway.invalid_json'
    assert not calls


def test_disconnect_cancels_generation_and_releases_account(setup):
    store, create = setup
    account = store.add_account('agent', 'key', models=['m'])
    source = EventStream([{'type': 'response.created', 'response': {'id': 'resp_one'}}], block=True)
    app, client = create(lambda r: httpx.Response(200, stream=source, headers={'content-type': 'text/event-stream'}))
    with client.websocket_connect('/v1/responses', headers=dict(AUTH)) as ws:
        ws.send_json({'type': 'response.create', 'model': 'm', 'input': 'test'})
        assert ws.receive_json()['type'] == 'response.created'
    assert source.closed.wait(1)
    assert app.state.pool.inflight[account] == 0
    assert not app.state.pool.probing


def test_incomplete_upstream_is_not_replayed(setup):
    store, create = setup
    for plan in ('agent', 'coding'):
        store.add_account(plan, plan, models=['m'])
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, stream=EventStream([{'type': 'response.output_text.delta', 'delta': 'partial'}]),
                              headers={'content-type': 'text/event-stream'})

    app, client = create(upstream)
    with client.websocket_connect('/v1/responses', headers=dict(AUTH)) as ws:
        ws.send_json({'type': 'response.create', 'model': 'm', 'input': 'test'})
        assert ws.receive_json()['delta'] == 'partial'
        error = ws.receive_json()
        assert error == {'type':'error', 'code':'Gateway.upstream_stream_incomplete',
                         'message':'Gateway: upstream_stream_incomplete', 'param':None, 'sequence_number':0}
    assert len(calls) == 1 and all(n == 0 for n in app.state.pool.inflight.values())


def test_real_websocket_upgrade_and_json_frames(setup):
    store, create = setup
    store.add_account('agent', 'key', models=['m'])
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, stream=EventStream([completed()]), headers={'content-type': 'text/event-stream'})

    app, _ = create(upstream)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='critical', lifespan='off', ws='auto'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started
        with connect(f'ws://127.0.0.1:{port}/v1/responses', additional_headers=dict(AUTH), open_timeout=3) as ws:
            assert ws.response.status_code == 101
            ws.send(json.dumps({'type': 'response.create', 'model': 'm', 'input': 'test'}))
            assert json.loads(ws.recv(timeout=3)) == completed()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
    assert not thread.is_alive() and len(calls) == 1


def test_named_lanes_run_independently_and_burst_stays_fifo(setup):
    store, create = setup
    store.add_account('agent', 'key', models=['m'])
    release = threading.Event()
    calls = []
    class SlowStream(EventStream):
        async def __aiter__(self):
            yield b'data: {"type":"response.created","response":{"id":"slow"}}\n\n'
            while not release.is_set():
                await asyncio.sleep(.005)
            async for chunk in super().__aiter__():
                yield chunk
    def upstream(request):
        value = json.loads(request.content)['input']
        calls.append(value)
        source = SlowStream([completed('resp_slow')]) if value == 'slow' else EventStream([completed('resp_' + value)])
        return httpx.Response(200, stream=source, headers={'content-type': 'text/event-stream'})
    app, client = create(upstream)
    timer = threading.Timer(3, release.set)
    timer.start()
    try:
        with client.websocket_connect('/v1/responses', headers=dict(AUTH)) as ws:
            ws.send_json({'type':'response.create', 'model':'m', 'input':'slow', 'stream_id':'slow'})
            assert ws.receive_json()['type'] == 'response.created'
            for i in range(10):
                ws.send_json({'type':'response.create', 'model':'m', 'input':str(i), 'stream_id':'fast'})
            for i in range(10):
                event = ws.receive_json()
                assert event['stream_id'] == 'fast' and event['response']['id'] == 'resp_' + str(i)
            release.set()
            assert ws.receive_json()['response']['id'] == 'resp_slow'
    finally:
        release.set()
        timer.cancel()
    assert calls == ['slow'] + [str(i) for i in range(10)]
    assert not app.state.pool.probing and all(n == 0 for n in app.state.pool.inflight.values())


def test_stream_limit_default_lane_and_idle_lanes_do_not_hold_capacity(setup):
    store, create = setup
    store.add_account('agent', 'key', models=['m'])
    calls = []
    def upstream(request):
        calls.append(request)
        return httpx.Response(200, stream=EventStream([completed()]), headers={'content-type':'text/event-stream'})
    _, client = create(upstream)
    with client.websocket_connect('/v1/responses', headers=dict(AUTH)) as ws:
        for i in range(32):
            ws.send_json({'type':'response.create', 'model':'m', 'input':'test', 'stream_id':str(i)})
            assert ws.receive_json()['stream_id'] == str(i)
        ws.send_json({'type':'response.create', 'model':'m', 'input':'test', 'stream_id':'33'})
        event = ws.receive_json()
        assert event['error']['code'] == 'websocket_stream_limit_reached' and event['stream_id'] == '33'
        ws.send_json({'type':'response.create', 'model':'m', 'input':'test'})
        assert 'stream_id' not in ws.receive_json()
        ws.send_json({'type':'response.create', 'model':'m', 'input':'test', 'stream_id':'0'})
        assert ws.receive_json()['stream_id'] == '0'
    assert len(calls) == 34


def test_concurrency_limit_and_disconnect_cancel_active_and_queued_lanes(setup):
    store, create = setup
    store.add_account('agent', 'key', models=['m'])
    sources = []
    def upstream(request):
        source = EventStream([{'type':'response.created', 'response':{'id':'resp_' + str(len(sources))}}], block=True)
        sources.append(source)
        return httpx.Response(200, stream=source, headers={'content-type':'text/event-stream'})
    app, client = create(upstream)
    with client.websocket_connect('/v1/responses', headers=dict(AUTH)) as ws:
        for i in range(17):
            ws.send_json({'type':'response.create', 'model':'m', 'input':'test', 'stream_id':str(i)})
        for _ in range(16):
            assert ws.receive_json()['type'] == 'response.created'
        assert len(sources) == 16 and sum(app.state.pool.inflight.values()) == 16
    assert len(sources) == 16 and all(source.closed.wait(1) for source in sources)
    assert not app.state.pool.probing and all(n == 0 for n in app.state.pool.inflight.values())


def test_cross_lane_fork_and_cached_continuation_can_fail_over_when_account_is_held(setup):
    store, create = setup
    for plan in ('agent', 'coding'):
        store.add_account(plan, plan, models=['m'])
    calls = []
    output = [{'role':'assistant', 'content':'answer'}]
    def upstream(request):
        calls.append((request.headers['authorization'], json.loads(request.content)))
        return httpx.Response(200, stream=EventStream([completed('resp_' + str(len(calls)), output)]),
                              headers={'content-type':'text/event-stream'})
    app, client = create(upstream)
    with client.websocket_connect('/v1/responses', headers=dict(AUTH)) as ws:
        ws.send_json({'type':'response.create', 'model':'m', 'input':'parent', 'store':False, 'stream_id':'parent'})
        assert ws.receive_json()['response']['id'] == 'resp_1'
        source_id = store.lookup_binding('resp_1')
        store.update(source_id, cooldown_kind='quota', cooldown_until=time.time()+3600)
        ws.send_json({'type':'response.create', 'model':'m', 'input':'branch', 'previous_response_id':'resp_1',
                      'store':False, 'stream_id':'fork'})
        event = ws.receive_json()
        assert event['response']['id'] == 'resp_2' and event['stream_id'] == 'fork'
    assert calls[0][0] != calls[1][0]
    assert calls[1][1]['input'] == [{'role':'user', 'content':'parent'}] + output + [{'role':'user', 'content':'branch'}]
    assert 'previous_response_id' not in calls[1][1]
    assert store.account(source_id)['cooldown_kind'] == 'quota'
    assert all(n == 0 for n in app.state.pool.inflight.values())


@pytest.mark.parametrize('raw', ['{"type":"response.create","model":"m","input":NaN}',
                                '{"type":"response.create","model":"m","input":"\\ud800"}',
                                '{"type":"response.create","model":"m","input":' + '['*10000 + '0' + ']'*10000 + '}'], ids=['nan', 'surrogate', 'deep'])
def test_invalid_json_does_not_break_socket_or_reserve_account(setup, raw):
    store, create = setup
    store.add_account('agent', 'key', models=['m'])
    calls = []
    def upstream(request):
        calls.append(request)
        return httpx.Response(200, stream=EventStream([completed()]), headers={'content-type':'text/event-stream'})
    app, client = create(upstream)
    with client.websocket_connect('/v1/responses', headers=dict(AUTH)) as ws:
        ws.send_text(raw)
        assert ws.receive_json()['error']['code'] == 'Gateway.invalid_json'
        ws.send_json({'type':'response.create', 'model':'m', 'input':'test'})
        assert ws.receive_json()['type'] == 'response.completed'
    assert len(calls) == 1 and all(n == 0 for n in app.state.pool.inflight.values())
