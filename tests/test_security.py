import asyncio
import gzip
import hashlib
import hmac
import io
import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from gateway import limits, protocols, store as store_module
from gateway.private_files import create_private
from gateway.protocols import ProtocolError, SSEDecoder, StreamResult
from gateway.store import Store

AUTH = {'authorization': 'Bearer security-test-service-token-123456'}
PASSWORD = 'security-test-admin-password'


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('ARK_GATEWAY_ALLOW_UNCONFIGURED', '1')
    monkeypatch.setenv('ARK_GATEWAY_ADMIN_PASSWORD', PASSWORD)
    monkeypatch.setenv('ARK_GATEWAY_SERVICE_TOKEN', AUTH['authorization'][7:])
    monkeypatch.delenv('ARK_AGENT_PLAN_KEYS', raising=False)
    monkeypatch.delenv('ARK_CODING_PLAN_KEYS', raising=False)
    from gateway.main import create_app
    database = Store(str(tmp_path / 'data' / 'test.db'), Fernet.generate_key().decode())
    database.add_account('agent', 'test-key', models=['m'])
    yield database, create_app
    database.close()


def assert_private(path, directory=False):
    if os.name == 'nt':
        import win32security
        from gateway.private_files import _attributes
        descriptor = win32security.GetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.OWNER_SECURITY_INFORMATION)
        owner = _attributes().SECURITY_DESCRIPTOR.GetSecurityDescriptorOwner()
        acl = descriptor.GetSecurityDescriptorDacl()
        assert descriptor.GetSecurityDescriptorOwner() == owner
        assert descriptor.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED or not directory
        assert acl.GetAceCount() == 1 and acl.GetAce(0)[2] == owner
    else:
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if directory else 0o600)


def test_secret_permissions_exist_before_writing_and_are_exclusive(tmp_path):
    target = tmp_path / '.env'
    with create_private(target) as output:
        assert target.stat().st_size == 0
        assert_private(target)
        output.write(b'secret')
    assert_private(target)
    with pytest.raises(Exception):
        create_private(target)
    assert target.read_bytes() == b'secret'


def test_setup_creates_private_configuration_without_overwriting(tmp_path):
    target = tmp_path / '.env'
    result = subprocess.run([sys.executable, '-m', 'gateway.setup', '--output', str(target)], capture_output=True)
    assert result.returncode == 0
    assert_private(target)
    before = target.read_bytes()
    result = subprocess.run([sys.executable, '-m', 'gateway.setup', '--output', str(target)], capture_output=True)
    assert result.returncode != 0 and target.read_bytes() == before


@pytest.mark.skipif(os.name != 'nt', reason='NTFS alternate streams are Windows-specific')
def test_ntfs_alternate_stream_rejected_before_writing(tmp_path):
    import win32security
    base = tmp_path / 'public.txt'
    base.write_bytes(b'retain')
    everyone = win32security.CreateWellKnownSid(win32security.WinWorldSid)
    acl = win32security.ACL()
    acl.AddAccessAllowedAce(win32security.ACL_REVISION, 0x1F01FF, everyone)
    win32security.SetNamedSecurityInfo(str(base), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None, None, acl, None)
    stream = Path(str(base) + ':secret')
    with pytest.raises(ValueError, match='alternate data streams'):
        create_private(stream)
    assert not stream.exists() and base.read_bytes() == b'retain'
    actual = win32security.GetNamedSecurityInfo(str(base), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION).GetSecurityDescriptorDacl()
    assert actual.GetAce(0)[2] == everyone


def test_database_sidecars_private_and_shared_directory_rejected(setup, tmp_path):
    database, _ = setup
    folder = tmp_path / 'data'
    assert_private(folder, True)
    for name in ('test.db', 'test.db-wal', 'test.db-shm'):
        assert_private(folder / name)
    shared = tmp_path / 'shared'
    shared.mkdir()
    (shared / 'unrelated.txt').write_text('retain')
    before = shared.stat().st_mode
    with pytest.raises(ValueError, match='dedicated'):
        Store(str(shared / 'gateway.db'), Fernet.generate_key().decode())
    assert shared.stat().st_mode == before and not (shared / 'gateway.db').exists()


@pytest.mark.asyncio
async def test_database_only_cookie_forgery_fails_and_sessions_survive_restart(setup):
    database, create = setup
    database.set_setting('session_secret', 'public-to-db-readers')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as remote:
        app = create(database, remote)
        stamp = str(int(time.time()))
        forged = stamp + '.' + hmac.new(b'public-to-db-readers', (stamp + database.setting('admin_hash')).encode(), hashlib.sha256).hexdigest()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/api/accounts', headers={'cookie': 'ark_gateway_session=' + forged})).status_code == 401
            assert (await client.post('/api/login', json={'password': PASSWORD})).status_code == 200
            assert (await client.get('/api/accounts')).status_code == 200
            cookie = client.cookies.get('ark_gateway_session')
            restarted = create(database, remote)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url='http://test') as other:
                assert (await other.get('/api/accounts', headers={'cookie': 'ark_gateway_session=' + cookie})).status_code == 200


async def direct_request(app, path, messages, headers=None, method='POST'):
    consumed = 0
    sent = []
    async def receive():
        nonlocal consumed
        item = messages[consumed]
        consumed += 1
        return item
    async def send(message):
        sent.append(message)
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': method,
             'scheme': 'http', 'path': path, 'raw_path': path.encode(), 'query_string': b'',
             'root_path': '', 'server': ('test', 80), 'client': ('127.0.0.1', 123),
             'headers': [(k.encode(), v.encode()) for k, v in (headers or {}).items()]}
    await app(scope, receive, send)
    return next(m['status'] for m in sent if m['type'] == 'http.response.start'), consumed


@pytest.mark.asyncio
@pytest.mark.parametrize('path,method', [('/api/accounts', 'POST'), ('/api/accounts/x', 'PATCH'),
    ('/api/settings', 'PATCH'), ('/api/pricing', 'PUT')])
async def test_management_auth_precedes_any_body_read(setup, path, method):
    database, create = setup
    async with httpx.AsyncClient() as remote:
        status, consumed = await direct_request(create(database, remote), path, [], method=method)
    assert status == 401 and consumed == 0


@pytest.mark.asyncio
async def test_login_slash_alias_preserves_redirect_and_authentication(setup):
    database, create = setup
    async with httpx.AsyncClient() as remote:
        app = create(database, remote)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            result = await client.post('/api/login/', json={'password': PASSWORD})
            assert result.status_code == 307
            result = await client.post('/api/login/', json={'password': PASSWORD}, follow_redirects=True)
            assert result.status_code == 200 and (await client.get('/api/accounts')).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize('path,headers', [('/v1/responses', AUTH), ('/v1/chat/completions', AUTH),
    ('/v1/responses/compact', AUTH), ('/api/login', {})])
@pytest.mark.parametrize('known_length', [False, True])
async def test_ingress_limit_stops_before_full_buffering(setup, monkeypatch, path, headers, known_length):
    database, create = setup
    monkeypatch.setattr(limits, 'MAX_BODY_BYTES', 64)
    monkeypatch.setattr(limits, 'MAX_LOGIN_BYTES', 64)
    chunks = [{'type': 'http.request', 'body': b'x' * 40, 'more_body': True}] * 3
    headers = {**headers, **({'content-length': '120'} if known_length else {})}
    async with httpx.AsyncClient() as remote:
        app = create(database, remote)
        status, consumed = await direct_request(app, path, chunks, headers)
    assert status == 413 and consumed == (0 if known_length else 2)
    assert app.state.requests.active == 0 and not app.state.pool.inflight


@pytest.mark.asyncio
async def test_absurd_content_length_and_global_http_limit_do_not_read_body(setup):
    database, create = setup
    async with httpx.AsyncClient() as remote:
        app = create(database, remote)
        assert await direct_request(app, '/v1/responses', [], {**AUTH, 'content-length': '9' * 5000}) == (413, 0)
        app.state.requests.maximum = 0
        assert await direct_request(app, '/v1/responses', [], AUTH) == (503, 0)


@pytest.mark.asyncio
async def test_admin_limits_and_origin_precede_json_parsing(setup, monkeypatch):
    database, create = setup
    monkeypatch.setattr(limits, 'MAX_ADMIN_BYTES', 64)
    async with httpx.AsyncClient() as remote:
        app = create(database, remote)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            await client.post('/api/login', json={'password': PASSWORD})
            assert (await client.patch('/api/settings', content=b'x' * 65)).status_code == 413
            assert (await client.patch('/api/settings', content=b'broken', headers={'origin': 'https://foreign'})).status_code == 403
            assert (await client.patch('/api/settings', json={'refresh_seconds': 120})).status_code == 200


@pytest.mark.asyncio
async def test_slow_body_deadline_releases_admission(setup, monkeypatch):
    database, create = setup
    monkeypatch.setattr(limits, 'BODY_TIMEOUT', .01)
    async with httpx.AsyncClient() as remote:
        app = create(database, remote)
        async def body():
            yield b'{'
            await asyncio.sleep(1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            result = await client.post('/v1/responses', headers=AUTH, content=body())
    assert result.status_code == 408 and app.state.requests.active == 0


@pytest.mark.asyncio
async def test_login_hashing_offloop_bounded_and_throttled(setup, monkeypatch):
    database, create = setup
    from gateway import main
    entered = threading.Event()
    release = threading.Event()
    calls = []
    def verify(*args):
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return False
    monkeypatch.setattr(main, 'password_ok', verify)
    async with httpx.AsyncClient() as remote:
        app = create(database, remote)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            first = asyncio.create_task(client.post('/api/login', json={'password': 'wrong'}))
            try:
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(.001)
                assert entered.is_set() and calls[0] != threading.get_ident()
                assert (await asyncio.wait_for(client.get('/healthz'), .2)).status_code == 200
                second = asyncio.create_task(client.post('/api/login', json={'password': 'wrong'}))
                for _ in range(100):
                    if len(calls) == 2:
                        break
                    await asyncio.sleep(.001)
                result = await client.post('/api/login', json={'password': 'wrong'})
                assert result.status_code == 429 and len(calls) == 2
            finally:
                release.set()
                await first
                if 'second' in locals():
                    await second
            for _ in range(8):
                await client.post('/api/login', json={'password': 'wrong'})
            assert (await client.post('/api/login', json={'password': 'wrong'})).status_code == 429


@pytest.mark.asyncio
async def test_canceling_login_keeps_actual_hashing_capacity_occupied(setup, monkeypatch):
    from gateway import main
    database, create = setup
    calls = []
    release = threading.Event()
    def verify(*args):
        calls.append(1)
        assert release.wait(3)
        return False
    monkeypatch.setattr(main, 'password_ok', verify)
    async with httpx.AsyncClient() as remote:
        app = create(database, remote)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            tasks = [asyncio.create_task(client.post('/api/login', json={'password': 'wrong'})) for _ in range(2)]
            try:
                for _ in range(100):
                    if len(calls) == 2:
                        break
                    await asyncio.sleep(.001)
                assert len(calls) == 2
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                assert (await client.post('/api/login', json={'password': 'wrong'})).status_code == 429
                assert len(calls) == 2
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
                await asyncio.sleep(.02)


class Chunks(httpx.AsyncByteStream):
    def __init__(self, parts):
        self.parts, self.reads, self.closed = parts, 0, False
    async def __aiter__(self):
        for chunk in self.parts:
            self.reads += 1
            yield chunk
    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize('encoding', ['identity', 'gzip', 'deflate'])
async def test_bounded_decoding_stops_compressed_expansion_and_closes(encoding):
    import zlib
    body = b'x' * 1_000_000
    raw = gzip.compress(body) if encoding == 'gzip' else zlib.compress(body) if encoding == 'deflate' else body
    source = Chunks([raw, b'must not be read'])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=source,
            headers={'content-encoding': encoding}))) as client:
        async with client.stream('GET', 'https://test') as response:
            with pytest.raises(limits.BodyTooLarge):
                await limits.bounded_response(response, 100)
    assert source.reads == 1 and source.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [200, 400])
async def test_upstream_size_rejection_closes_and_does_not_fail_over(setup, monkeypatch, status):
    database, create = setup
    database.add_account('coding', 'other-key', models=['m'])
    from gateway import main
    async def bounded(response):
        return await limits.bounded_response(response, 100)
    monkeypatch.setattr(main, 'bounded_response', bounded)
    source = Chunks([b'x' * 101, b'not-read'])
    calls = []
    def upstream(request):
        calls.append(request)
        return httpx.Response(status, stream=source)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        app = create(database, remote)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            result = await client.post('/v1/responses/compact', json={'model': 'm', 'input': 'ok'}, headers=AUTH)
    assert result.status_code == 502 and result.json()['error']['code'] == 'Gateway.upstream_response_too_large'
    assert len(calls) == 1 and source.reads == 1 and source.closed
    assert app.state.requests.active == 0 and not any(app.state.pool.inflight.values())


@pytest.mark.asyncio
async def test_management_query_bounded_before_json(setup, monkeypatch):
    from gateway import quota
    database, _ = setup
    monkeypatch.setattr(quota, 'MAX_MANAGEMENT_BYTES', 100)
    source = Chunks([b'x' * 101, b'not-read'])
    original = httpx.AsyncClient
    monkeypatch.setattr(quota.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, stream=source))))
    with pytest.raises(limits.BodyTooLarge):
        await quota.management_call('GetAFPUsage', 'test-ak', 'test-sk', {})
    assert source.reads == 1 and source.closed


def test_empty_sse_lines_are_charged_without_per_line_objects(monkeypatch):
    monkeypatch.setattr(protocols, 'MAX_EVENT_BYTES', 100)
    decoder = SSEDecoder()
    list(decoder.feed(b'data:\n' * 50))
    assert decoder.size == len(decoder.data) == 50 and isinstance(decoder.data, bytearray)
    with pytest.raises(ProtocolError, match='too_large'):
        list(decoder.feed(b'data:\n' * 51))


def test_sse_partial_line_scan_only_visits_new_bytes(monkeypatch):
    original = protocols.LINE_ENDING
    scanned = 0
    class Scan:
        def search(self, buffer, start):
            nonlocal scanned
            scanned += len(buffer) - start
            return original.search(buffer, start)
    monkeypatch.setattr(protocols, 'LINE_ENDING', Scan())
    decoder = SSEDecoder()
    for _ in range(10_000):
        list(decoder.feed(b'x'))
    assert scanned == 10_000 and isinstance(decoder.buffer, bytearray)


def test_json_depth_limit_before_materialization_preserves_quoted_braces(monkeypatch):
    monkeypatch.setattr(protocols, 'MAX_JSON_DEPTH', 10)
    value = {'text': '[{' * 1000 + '\\"', 'nested': [[{'a': 'ok'}]]}
    assert protocols.request_json(json.dumps(value)) == value
    with pytest.raises(ProtocolError):
        protocols.request_json(b'[' * 11 + b'0' + b']' * 11)


@pytest.mark.parametrize('encoding', ['utf-16', 'utf-32'])
def test_alternate_json_encoding_cannot_bypass_depth_guard(monkeypatch, encoding):
    monkeypatch.setattr(protocols, 'MAX_JSON_DEPTH', 10)
    payload = '["\\\"",' + '[' * 11 + '0' + ']' * 11 + ']'
    with pytest.raises(ProtocolError):
        protocols.request_json(payload.encode(encoding))


def test_chat_fragment_aggregation_uses_buffers_and_keeps_utf8_limits(monkeypatch):
    result = StreamResult(chat=True, collect=True)
    for _ in range(1000):
        result.observe({'choices': [{'index': 0, 'delta': {'content': '你', 'tool_calls': [
            {'index': 0, 'function': {'name': 'f', 'arguments': 'x'}}]}}]})
    assert isinstance(result.choices[0]['message']['content'], io.StringIO)
    assert isinstance(result.choices[0]['message']['tool_calls'][0]['function']['arguments'], io.StringIO)
    result.observe({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})
    result.observe(None)
    message = result.result()['choices'][0]['message']
    assert message['content'] == '你' * 1000 and message['tool_calls'][0]['function']['arguments'] == 'x' * 1000
    monkeypatch.setattr(protocols, 'MAX_EVENT_BYTES', 100)
    with pytest.raises(ProtocolError, match='too_large'):
        StreamResult(True, True).observe({'choices': [{'index': 0, 'delta': {'content': '你' * 30}}]})


def test_binding_expiration_capacity_and_id_validation(setup, monkeypatch):
    database, _ = setup
    account = database.accounts()[0]['id']
    monkeypatch.setattr(store_module, 'MAX_BINDINGS', 3)
    clock = time.time()
    for index in range(5):
        monkeypatch.setattr(store_module.time, 'time', lambda: clock + index)
        database.bind('resp_' + str(index), account)
    assert database.db.execute('SELECT COUNT(*) FROM response_bindings').fetchone()[0] == 3
    assert database.lookup_binding('resp_0') is None and database.lookup_binding('resp_4') == account
    with pytest.raises(ValueError):
        database.bind('x' * 257, account)
    monkeypatch.setattr(store_module.time, 'time', lambda: clock + store_module.BINDING_TTL + 10)
    assert database.lookup_binding('resp_4') is None
    database.bind('new', account)
    assert database.db.execute('SELECT COUNT(*) FROM response_bindings').fetchone()[0] == 1


@pytest.mark.asyncio
async def test_bindings_not_written_for_each_intermediate_id_and_delete_unbinds(setup):
    database, create = setup
    events = [{'type': 'response.in_progress', 'response': {'id': 'resp_' + str(i)}} for i in range(100)]
    events += [{'type': 'response.completed', 'response': {'id': 'resp_done', 'status': 'completed', 'output': []}}]
    payload = b''.join(b'data: ' + json.dumps(e).encode() + b'\n\n' for e in events)
    def upstream(request):
        return httpx.Response(204) if request.method == 'DELETE' else httpx.Response(200, content=payload,
            headers={'content-type': 'text/event-stream'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        app = create(database, remote)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.post('/v1/responses', json={'model': 'm', 'input': 'ok'}, headers=AUTH)).status_code == 200
            assert database.db.execute('SELECT COUNT(*) FROM response_bindings').fetchone()[0] == 2
            assert (await client.delete('/v1/responses/resp_done', headers=AUTH)).status_code == 204
            assert database.lookup_binding('resp_done') is None


def test_socket_connection_and_shared_call_limits(setup):
    database, create = setup
    remote = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail('must not contact upstream')))
    app = create(database, remote)
    app.state.sockets.maximum = 1
    try:
        with TestClient(app) as client:
            with client.websocket_connect('/v1/responses', headers=AUTH) as first:
                with pytest.raises(WebSocketDisconnect) as refused:
                    with client.websocket_connect('/v1/responses', headers=AUTH):
                        pass
                assert refused.value.code == 1013
                app.state.requests.maximum = 0
                first.send_json({'type': 'response.create', 'model': 'm', 'input': 'ok'})
                assert first.receive_json()['error']['code'] == 'Gateway.gateway_busy'
            assert app.state.sockets.active == 0 and app.state.requests.active == 0
            assert app.state.socket_queue_bytes == app.state.socket_history_bytes == 0
    finally:
        asyncio.run(remote.aclose())


def test_socket_queue_item_limit_precedes_decode_and_disconnect_cleans(setup, monkeypatch):
    from gateway import websocket
    database, create = setup
    monkeypatch.setattr(websocket, 'MAX_QUEUE_ITEMS', 1)
    source = Chunks([b'data: {"type":"response.created","response":{"id":"resp_one"}}\n\n'])
    release = threading.Event()
    class Blocking(Chunks):
        async def __aiter__(self):
            yield source.parts[0]
            while not release.is_set():
                await asyncio.sleep(.001)
    stream = Blocking([])
    remote = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=stream,
        headers={'content-type': 'text/event-stream'})))
    app = create(database, remote)
    try:
        # No lifespan here: the fixture owns the store, just as existing WS tests do.
        client = TestClient(app)
        with client.websocket_connect('/v1/responses', headers=AUTH) as socket:
            socket.send_json({'type': 'response.create', 'model': 'm', 'input': 'ok'})
            assert socket.receive_json()['type'] == 'response.created'
            socket.send_text('not even JSON')
            assert socket.receive_json()['error']['code'] == 'Gateway.websocket_request_queue_full'
            assert app.state.socket_queue_bytes > 0
        assert stream.closed and app.state.socket_queue_bytes == app.state.socket_history_bytes == 0
        assert app.state.requests.active == app.state.sockets.active == 0
    finally:
        release.set()
        asyncio.run(remote.aclose())
