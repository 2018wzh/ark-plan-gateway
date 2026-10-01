from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
import anyio
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

from .pool import AccountPool, classify_error
from .protocols import ProtocolError, SSEDecoder, StreamResult, invalid_tool_arguments
from .errors import gateway_error, safe_error_code
from .store import Store
from .websocket import serve_responses

AGENT_URL = "https://ark.cn-beijing.volces.com/api/plan/v3"
CODING_URL = "https://ark.cn-beijing.volces.com/api/coding/v3"
TRANSIENT_HTTP_STATUSES = {500, 502, 503, 504}
MAX_TRANSIENT_FAILURES = 3


def password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 300_000).hex()
    return salt + ":" + digest


def password_ok(password: str, hashed: str) -> bool:
    salt = hashed.split(":", 1)[0]
    return hmac.compare_digest(password_hash(password, salt), hashed)


def error_response(status: int, code: str, retry_at: float | None = None,
                   param: str | None = None) -> JSONResponse:
    headers = {}
    message = None
    if retry_at is not None:
        seconds = max(1, int(retry_at - time.time() + 0.999))
        headers["Retry-After"] = str(seconds)
        message = f"{code}; retry after {seconds} seconds"
    return JSONResponse(gateway_error(status, code, param, message), status_code=status, headers=headers)


def forward_response_headers(response: Response, upstream: httpx.Response, transformed: bool = False):
    # httpx decodes the body; HTTP framing and connection state belong to this hop.
    excluded = {"connection", "proxy-connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
                "transfer-encoding", "upgrade", "content-length", "content-encoding"}
    excluded.update(value.strip().lower() for value in upstream.headers.get("connection", "").split(","))
    if transformed or upstream.headers.get("content-encoding"):
        excluded.update({"etag", "content-md5", "digest", "content-digest", "repr-digest", "content-range"})
    if transformed:
        excluded.add("content-type")
    forwarded = [(key.lower(), value) for key, value in upstream.headers.raw if key.decode("ascii").lower() not in excluded]
    names = {key for key, _ in forwarded}
    response.raw_headers = [(key, value) for key, value in response.raw_headers if key not in names] + forwarded
    return response


def token_usage(response: object, chat: bool = False) -> tuple[int, int]:
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return 0, 0
    def count(name: str) -> int:
        value = usage.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
    return count("prompt_tokens" if chat else "input_tokens"), count("completion_tokens" if chat else "output_tokens")


class AccountIn(BaseModel):
    plan: str = Field(pattern="^(agent|coding)$")
    api_key: str = Field(min_length=5)
    label: str = Field(min_length=1, max_length=100)
    models: list[str] = Field(default_factory=lambda: ["ark-code-latest"])
    model_mapping: dict[str, str] = Field(default_factory=dict)
    access_key: str | None = None
    secret_key: str | None = None


class AccountPatch(BaseModel):
    label: str | None = None
    models: list[str] | None = None
    model_mapping: dict[str, str] | None = None
    enabled: bool | None = None
    quota_group: str | None = None
    api_key: str | None = None
    access_key: str | None = None
    secret_key: str | None = None


class LoginIn(BaseModel):
    password: str


class SettingsIn(BaseModel):
    refresh_seconds: int | None = Field(default=None, ge=30, le=3600)
    new_password: str | None = Field(default=None, min_length=12)
    new_service_token: str | None = Field(default=None, min_length=24)


class Price(BaseModel):
    input: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    output: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class PriceTier(BaseModel):
    max_input_tokens: int = Field(gt=0)
    input: float = Field(ge=0, allow_inf_nan=False)
    output: float = Field(ge=0, allow_inf_nan=False)


class ModelPrice(Price):
    tiers: list[PriceTier] = Field(default_factory=list, max_length=20)
    peak: Price | None = None
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def valid_tiers(self):
        if self.tiers:
            bounds = [tier.max_input_tokens for tier in self.tiers]
            if bounds != sorted(set(bounds)) or self.input is not None or self.output is not None or self.peak is not None:
                raise ValueError("tiers require increasing unique bounds and no flat rates")
        if self.peak is not None and any(rate is None for rate in (self.input, self.output, self.peak.input, self.peak.output)):
            raise ValueError("peak pricing requires complete peak and off-peak prices")
        return self


class PricingIn(BaseModel):
    default: Price
    models: dict[str, ModelPrice]

    @field_validator("models")
    @classmethod
    def valid_models(cls, value: dict[str, ModelPrice]) -> dict[str, ModelPrice]:
        if len(value) > 100 or any(not name.strip() or len(name) > 128 for name in value):
            raise ValueError("invalid model pricing")
        return value


def create_app(store: Store | None = None, client: httpx.AsyncClient | None = None) -> FastAPI:
    if store is None:
        master = os.environ.get("ARK_GATEWAY_MASTER_KEY")
        if not master:
            raise RuntimeError("ARK_GATEWAY_MASTER_KEY is required")
        store = Store(os.environ.get("ARK_GATEWAY_DB", "data/gateway.db"), master)
    if not store.setting("admin_hash"):
        password = os.environ.get("ARK_GATEWAY_ADMIN_PASSWORD", "")
        if len(password) < 12:
            raise RuntimeError("ARK_GATEWAY_ADMIN_PASSWORD (12+ chars) is required on first start")
        store.set_setting("admin_hash", password_hash(password))
    if not store.setting("service_token"):
        token = os.environ.get("ARK_GATEWAY_SERVICE_TOKEN", "")
        if len(token) < 24:
            raise RuntimeError("ARK_GATEWAY_SERVICE_TOKEN (24+ chars) is required on first start")
        store.set_setting("service_token", store._enc(token))
    if not store.setting("session_secret"):
        store.set_setting("session_secret", secrets.token_hex(32))
    if not store.setting("refresh_seconds"):
        store.set_setting("refresh_seconds", "60")

    for plan, env in (("agent", "ARK_AGENT_PLAN_KEYS"), ("coding", "ARK_CODING_PLAN_KEYS")):
        for key in os.environ.get(env, "").split(";"):
            key = key.strip().strip('"').strip("'")
            if key:
                store.add_account(plan, key)

    pool = AccountPool(store)
    owned_client = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(180, connect=10), follow_redirects=False)

    async def quota_loop():
        while True:
            for account in store.accounts(private=True):
                if not account["enabled"] or not account["has_ak_sk"]:
                    continue
                try:
                    from .quota import refresh_account
                    await refresh_account(store, account)
                except Exception:
                    store.update(account["id"], quota_error="额度查询失败")
            await asyncio.sleep(int(store.setting("refresh_seconds", "60")))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(quota_loop())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            if owned_client:
                await client.aclose()
            store.close()

    app = FastAPI(title="Ark Plan Gateway", lifespan=lifespan)
    app.state.store = store
    app.state.pool = pool
    app.state.client = client

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if request.url.path == "/v1" or request.url.path.startswith("/v1/"):
            code = {401: "invalid_token", 404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "request_rejected")
            response = error_response(exc.status_code, code)
            response.headers.update(exc.headers or {})
            return response
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    def require_service(request: Request | WebSocket):
        auth = request.headers.get("authorization", "")
        expected = store._dec(store.setting("service_token"))
        if not hmac.compare_digest(auth, "Bearer " + expected):
            raise HTTPException(401, "invalid token")

    def session_value() -> str:
        secret = store.setting("session_secret")
        stamp = str(int(time.time()))
        sig = hmac.new(secret.encode(), (stamp + store.setting("admin_hash")).encode(), hashlib.sha256).hexdigest()
        return stamp + "." + sig

    def require_admin(request: Request, write: bool = False):
        cookie = request.cookies.get("ark_gateway_session", "")
        try:
            stamp, sig = cookie.split(".", 1)
            if time.time() - int(stamp) > 86400 or time.time() < int(stamp):
                raise ValueError()
            secret = store.setting("session_secret")
            expected = hmac.new(secret.encode(), (stamp + store.setting("admin_hash")).encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected):
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(401, "login required")
        if write:
            origin = request.headers.get("origin", "")
            host = request.headers.get("host", "")
            if origin and origin not in (f"http://{host}", f"https://{host}"):
                raise HTTPException(403, "cross-origin write rejected")

    @app.post("/api/login")
    async def login(body: LoginIn, response: Response, request: Request):
        if not password_ok(body.password, store.setting("admin_hash")):
            raise HTTPException(401, "invalid password")
        response.set_cookie("ark_gateway_session", session_value(), httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", max_age=86400)
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(request: Request, response: Response):
        require_admin(request, True)
        response.delete_cookie("ark_gateway_session")
        return {"ok": True}

    @app.get("/api/accounts")
    async def accounts(request: Request):
        require_admin(request)
        return store.accounts()

    @app.post("/api/accounts")
    async def add_account(body: AccountIn, request: Request):
        require_admin(request, True)
        if not body.models or any(k not in body.models or not v for k, v in body.model_mapping.items()):
            raise HTTPException(400, "model mapping must use configured model names")
        if bool(body.access_key) != bool(body.secret_key):
            raise HTTPException(400, "AK/SK must be provided together")
        existing = {a["id"] for a in store.accounts()}
        account_id = store.add_account(body.plan, body.api_key, body.label, body.models)
        if account_id in existing:
            raise HTTPException(409, "API key already exists")
        store.update(account_id, model_mapping=body.model_mapping,
                     access_key=body.access_key, secret_key=body.secret_key)
        return store.account(account_id)

    @app.patch("/api/accounts/{account_id}")
    async def edit_account(account_id: str, body: AccountPatch, request: Request):
        require_admin(request, True)
        previous = store.account(account_id)
        if not previous:
            raise HTTPException(404, "account not found")
        data = body.model_dump(exclude_unset=True)
        if "quota_group" in data:
            parent = store.account(data["quota_group"])
            if not parent or parent["plan"] != store.account(account_id)["plan"]:
                raise HTTPException(400, "quota group must refer to an account in the same plan")
        if "models" in data and (not data["models"] or not all(isinstance(x, str) and x for x in data["models"])):
            raise HTTPException(400, "models required")
        if "model_mapping" in data and any(k not in (data.get("models") or store.account(account_id)["models"]) or not v for k,v in data["model_mapping"].items()):
            raise HTTPException(400, "model mapping must use configured model names")
        try:
            store.update(account_id, **data)
        except sqlite3.IntegrityError:
            raise HTTPException(409, "API key already exists")
        if data.get("api_key") or any(k in data and data[k] != previous[k] for k in ("models", "model_mapping")):
            store.clear_model_blocks(account_id)
        if data.get("api_key"):
            store.update(account_id, auth_failed=0)
        return store.account(account_id)

    @app.post("/api/accounts/{account_id}/refresh")
    async def refresh(account_id: str, request: Request):
        require_admin(request, True)
        account = store.account(account_id, True)
        if not account:
            raise HTTPException(404, "account not found")
        from .quota import refresh_account
        await refresh_account(store, account)
        return store.account(account_id)

    @app.post("/api/accounts/{account_id}/resume")
    async def resume(account_id: str, request: Request):
        require_admin(request, True)
        account = store.account(account_id)
        if not account:
            raise HTTPException(404, "account not found")
        for a in store.accounts():
            if a["quota_group"] == account["quota_group"]:
                store.update(a["id"], cooldown_kind=None, cooldown_until=None, cooldown_failures=0, cooldown_code=None, auth_failed=0)
                store.clear_model_blocks(a["id"])
        return store.account(account_id)

    @app.get("/api/settings")
    async def settings(request: Request):
        require_admin(request)
        return {"refresh_seconds": int(store.setting("refresh_seconds")), "service_token_configured": True}

    @app.patch("/api/settings")
    async def edit_settings(body: SettingsIn, request: Request):
        require_admin(request, True)
        if body.refresh_seconds is not None:
            store.set_setting("refresh_seconds", str(body.refresh_seconds))
        if body.new_password:
            store.set_setting("admin_hash", password_hash(body.new_password))
        if body.new_service_token:
            store.set_setting("service_token", store._enc(body.new_service_token))
        return {"ok": True}

    @app.get("/api/routes")
    async def routes(request: Request):
        require_admin(request)
        rows = store.accounts()
        now = time.time()
        for a in rows:
            a["inflight"] = pool.inflight.get(a["id"], 0)
            a["cooldown_seconds"] = max(0, int(a["cooldown_until"] - now)) if a["cooldown_until"] else None
        return rows

    @app.get("/api/statistics")
    async def statistics(request: Request, account_id: str | None = None, days: int = 30):
        require_admin(request)
        if days < 1 or days > 90:
            raise HTTPException(400, "days must be between 1 and 90")
        try:
            return store.statistics(account_id, days)
        except KeyError:
            raise HTTPException(404, "account not found")

    @app.get("/api/pricing")
    async def pricing(request: Request):
        require_admin(request)
        return store.pricing()

    @app.put("/api/pricing")
    async def edit_pricing(body: PricingIn, request: Request):
        require_admin(request, True)
        store.set_pricing(body.model_dump(exclude_none=True))
        return store.pricing()

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/v1/models")
    async def models(request: Request):
        require_service(request)
        names = sorted({m for a in store.accounts() if a["enabled"] and not a["auth_failed"] and not a["expired"] and a["cooldown_kind"] != "account"
                        for m in a["models"] if not (pool.model_block(a, m) and pool.model_block(a, m)["retry_at"] is None)})
        return {"object": "list", "data": [{"id": m, "object": "model", "created": 0, "owned_by": "ark-plan-gateway"} for m in names]}

    async def proxy(request: Request | None, method: str, path: str, body: bytes | None, model: str,
                    pinned: str | None, stream: bool = False):
        chat = path == "/chat/completions"
        generation = method == "POST" and path in ("/responses", "/chat/completions")
        attempted: set[str] = set()
        last_response = None
        transient_failures = 0
        while True:
            candidates = await pool.candidates(model, pinned, attempted)
            if not candidates:
                if last_response is not None:
                    return last_response
                status, code, retry_at, _ = pool.unavailable(model, pinned)
                return error_response(status, code, retry_at)
            if transient_failures >= MAX_TRANSIENT_FAILURES:
                if last_response is not None:
                    return last_response
                return error_response(503, "upstream_failover_exhausted")
            account = candidates[0]
            attempted.add(account["id"])
            if not await pool.reserve(account, model):
                continue
            started = time.monotonic()
            started_at = time.time()
            def record(outcome: str, response_data: object = None):
                if method == "POST":
                    inputs, outputs = token_usage(response_data, chat)
                    usage = response_data.get("usage") if isinstance(response_data, dict) else None
                    context = usage.get("prompt_tokens" if chat else "input_tokens") if isinstance(usage, dict) else None
                    context = context if isinstance(context, int) and not isinstance(context, bool) and context >= 0 else -1
                    reported_model = response_data.get("model") if isinstance(response_data, dict) else None
                    usage_model = reported_model if isinstance(reported_model, str) and 0 < len(reported_model) <= 128 else account["model_mapping"].get(model, model)
                    store.record_request(account["id"], outcome, int((time.monotonic() - started) * 1000), usage_model, inputs, outputs, started_at, context)
                if outcome == "success":
                    pool.update_result(account, "ok", None, model)
            base = AGENT_URL if account["plan"] == "agent" else CODING_URL
            url = base + path
            headers = {"Authorization": "Bearer " + account["api_key"], "Content-Type": "application/json"}
            if generation:
                headers["Accept"] = "text/event-stream"
            try:
                account_body = body
                mapped = account["model_mapping"].get(model)
                if method == "POST":
                    account_json = json.loads(body)
                    if mapped:
                        account_json["model"] = mapped
                    # Plan/model variants may require streaming. Request it on the
                    # first attempt; never replay an accepted generation to adapt.
                    if generation:
                        account_json["stream"] = True
                        if chat:
                            account_json["stream_options"] = {**account_json.get("stream_options", {}), "include_usage": True}
                    account_body = json.dumps(account_json, ensure_ascii=False).encode()
                req = client.build_request(method, url, content=account_body, headers=headers)
                upstream = await client.send(req, stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                record("connection_error")
                last_response = None
                if method == "POST":
                    pool.update_result(account, "server", None, model)
                with anyio.CancelScope(shield=True):
                    await pool.release(account)
                if method == "POST":
                    transient_failures += 1
                    continue
                return error_response(502, "upstream_connection_failed")
            except httpx.PoolTimeout:
                record("connection_pool_busy")
                with anyio.CancelScope(shield=True):
                    await pool.release(account)
                return error_response(503, "gateway_connection_pool_busy")
            except (httpx.TimeoutException, httpx.TransportError):
                record("transport_error")
                if method == "POST":
                    pool.update_result(account, "server", None, model)
                await pool.release(account)
                return error_response(502, "upstream_transport_ambiguous")
            except asyncio.CancelledError:
                record("client_disconnected")
                with anyio.CancelScope(shield=True):
                    await pool.release(account)
                raise

            async def close_upstream():
                with anyio.CancelScope(shield=True):
                    try:
                        await upstream.aclose()
                    finally:
                        await pool.release(account)
            if upstream.status_code >= 400:
                try:
                    raw = await upstream.aread()
                except (httpx.TimeoutException, httpx.TransportError):
                    record("response_error")
                    if method == "POST":
                        pool.update_result(account, "server", None, model)
                    await close_upstream()
                    return error_response(502, "upstream_response_failed")
                except asyncio.CancelledError:
                    record("client_disconnected")
                    await close_upstream()
                    raise
                kind, until = classify_error(upstream.status_code, raw, upstream.headers, time.time())
                code = safe_error_code(raw)
                last_response = forward_response_headers(Response(content=raw, status_code=upstream.status_code), upstream)
                record(kind)
                # A lookup/deletion error must not quarantine a working model.
                if method == "POST" or kind in ("quota", "rate", "auth", "account"):
                    pool.update_result(account, kind, until, model, code)
                await close_upstream()
                # A 5xx can still mean the upstream did work; this favors availability.
                if method == "POST" and upstream.status_code in TRANSIENT_HTTP_STATUSES and kind in ("server", "overload"):
                    transient_failures += 1
                    continue
                if kind in ("quota", "rate", "model_rate", "auth", "account", "model", "model_limit", "overload") and method == "POST" and upstream.status_code < 500:
                    continue
                return last_response
            upstream_sse = upstream.headers.get("content-type", "").split(";", 1)[0].strip() == "text/event-stream"
            if not upstream_sse:
                try:
                    if stream:
                        record("response_error")
                        return error_response(502, "upstream_stream_expected")
                    raw = await upstream.aread()
                    data = json.loads(raw)
                    if path == "/responses" and isinstance(data, dict) and data.get("id"):
                        store.bind(data["id"], account["id"])
                    record("success", data)
                    return forward_response_headers(Response(content=raw, status_code=upstream.status_code,
                                    media_type=upstream.headers.get("content-type", "application/json")), upstream)
                except (httpx.TimeoutException, httpx.TransportError, ValueError):
                    record("response_error")
                    if method == "POST":
                        pool.update_result(account, "server", None, model)
                    return error_response(502, "upstream_response_failed")
                finally:
                    await close_upstream()

            decoder = SSEDecoder()
            state = StreamResult(chat, collect=not stream)
            stream_error: dict = {}
            last_sequence_number = -1

            def observe(chunk: bytes):
                nonlocal last_sequence_number
                for obj in decoder.feed(chunk):
                    state.observe(obj)
                    if obj is None:
                        continue
                    sequence = obj.get("sequence_number")
                    if isinstance(sequence, int) and not isinstance(sequence, bool):
                        last_sequence_number = max(last_sequence_number, sequence)
                    response_obj = obj.get("response", {})
                    if not chat and isinstance(response_obj, dict) and response_obj.get("id"):
                        store.bind(response_obj["id"], account["id"])
                    if state.failed and not stream_error:
                        error = obj.get("error")
                        if not error and isinstance(response_obj, dict):
                            error = response_obj.get("error")
                        if not error and obj.get("type") == "error":
                            error = {"code": obj.get("code"), "message": obj.get("message")}
                        raw_error = json.dumps({"error": error}).encode()
                        kind, until = classify_error(500, raw_error, {}, time.time())
                        code = safe_error_code(raw_error)
                        stream_error.update(obj)
                        pool.update_result(account, kind, until, model, code)

            if not stream:
                outcome = "client_disconnected"
                try:
                    async for chunk in upstream.aiter_bytes():
                        if request is not None and await request.is_disconnected():
                            return error_response(499, "client_disconnected")
                        observe(chunk)
                        if state.failed:
                            raise ProtocolError("upstream_stream_failed")
                    data = state.result()
                    outcome = "success"
                    return forward_response_headers(JSONResponse(data, status_code=upstream.status_code), upstream, transformed=True)
                except (httpx.TimeoutException, httpx.TransportError, ValueError, KeyError, TypeError):
                    outcome = "stream_error"
                    if not state.failed:
                        pool.update_result(account, "server", None, model)
                    if stream_error:
                        return forward_response_headers(JSONResponse(stream_error, status_code=502), upstream, transformed=True)
                    return error_response(502, "upstream_stream_incomplete")
                finally:
                    record(outcome, state.usage_response)
                    await close_upstream()

            def failure_event(code):
                if chat:
                    event = gateway_error(502, code)
                else:
                    event = {"type": "error", "code": "Gateway." + code, "message": "Gateway: " + code,
                             "param": None, "sequence_number": last_sequence_number + 1}
                return b'\nevent: error\ndata: ' + json.dumps(event).encode() + b'\n\n'

            async def events():
                outcome = "client_disconnected"
                try:
                    async for chunk in upstream.aiter_bytes():
                        observe(chunk)
                        yield chunk
                    outcome = "stream_error" if state.failed or not state.done else "success"
                    if not state.failed and not state.done:
                        pool.update_result(account, "server", None, model)
                        yield failure_event("upstream_stream_incomplete")
                except (httpx.TimeoutException, httpx.TransportError, ValueError, KeyError, TypeError):
                    outcome = "stream_error"
                    if not state.failed:
                        pool.update_result(account, "server", None, model)
                        yield failure_event("upstream_stream_interrupted")
                finally:
                    record(outcome, state.usage_response)
                    await close_upstream()

            return forward_response_headers(StreamingResponse(events(), status_code=upstream.status_code, media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}), upstream)

    def validate_generation(data: object, chat: bool, compact: bool = False):
        model = data.get("model") if isinstance(data, dict) else None
        if not isinstance(model, str) or not model:
            return error_response(400, "model_required")
        if compact:
            return model, None
        if "stream" in data and not isinstance(data["stream"], bool):
            return error_response(400, "stream_must_be_boolean")
        invalid_arguments = invalid_tool_arguments(data, chat)
        if invalid_arguments:
            return error_response(400, "invalid_tool_arguments", param=invalid_arguments)
        if chat:
            if not isinstance(data.get("messages"), list) or not data["messages"]:
                return error_response(400, "messages_required")
            if "stream_options" in data and not isinstance(data["stream_options"], dict):
                return error_response(400, "invalid_stream_options")
            return model, None
        if data.get("background"):
            return error_response(400, "background_not_supported")
        prior = data.get("previous_response_id")
        if prior is not None and not isinstance(prior, str):
            return error_response(400, "invalid_previous_response_id")
        pinned = store.lookup_binding(prior) if prior else None
        if prior and not pinned:
            return error_response(404, "previous_response_unknown")
        return model, pinned

    async def create_generation(request: Request, chat: bool, compact: bool = False):
        require_service(request)
        raw = await request.body()
        if len(raw) > 8_000_000:
            return error_response(413, "request_too_large")
        try:
            data = json.loads(raw)
        except ValueError:
            return error_response(400, "invalid_json")
        validated = validate_generation(data, chat, compact)
        if isinstance(validated, Response):
            return validated
        model, pinned = validated
        path = "/responses/compact" if compact else "/chat/completions" if chat else "/responses"
        return await proxy(request, "POST", path, raw, model, pinned, False if compact else data.get("stream", False))

    @app.post("/v1/responses")
    async def responses(request: Request):
        return await create_generation(request, chat=False)

    @app.post("/v1/responses/compact")
    async def compact_responses(request: Request):
        return await create_generation(request, chat=False, compact=True)

    @app.websocket("/v1/responses")
    async def websocket_responses(websocket: WebSocket):
        try:
            require_service(websocket)
        except HTTPException:
            await websocket.close(code=1008)
            return

        def validate(data):
            result = validate_generation(data, False)
            return result if isinstance(result, Response) else None

        async def generate(data, binding_id):
            validated = validate_generation(data, False)
            if isinstance(validated, Response):
                return validated
            model, pinned = validated
            if binding_id:
                pinned = store.lookup_binding(binding_id)
                if not pinned:
                    return error_response(404, "previous_response_unknown")
            raw = json.dumps(data, ensure_ascii=False).encode()
            return await proxy(None, "POST", "/responses", raw, model, pinned, True)

        await serve_responses(websocket, validate, generate)

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        return await create_generation(request, chat=True)

    @app.api_route("/v1/responses/{response_id}", methods=["GET", "DELETE"])
    async def response_item(response_id: str, request: Request):
        require_service(request)
        account_id = store.lookup_binding(response_id)
        if not account_id:
            return error_response(404, "response_unknown")
        account = store.account(account_id)
        if not account:
            return error_response(404, "response_account_missing")
        model = account["models"][0]
        return await proxy(request, request.method, "/responses/" + quote(response_id, safe=""), None, model, account_id)

    dist = Path(__file__).parent / "static"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/")
        async def index():
            return FileResponse(dist / "index.html")
    return app


app = None
if os.environ.get("ARK_GATEWAY_ALLOW_UNCONFIGURED") != "1":
    from dotenv import load_dotenv
    load_dotenv()
    app = create_app()
