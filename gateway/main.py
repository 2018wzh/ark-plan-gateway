from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.staticfiles import StaticFiles

from .pool import AccountPool, classify_error
from .store import Store

AGENT_URL = "https://ark.cn-beijing.volces.com/api/plan/v3"
CODING_URL = "https://ark.cn-beijing.volces.com/api/coding/v3"


def password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 300_000).hex()
    return salt + ":" + digest


def password_ok(password: str, hashed: str) -> bool:
    salt = hashed.split(":", 1)[0]
    return hmac.compare_digest(password_hash(password, salt), hashed)


def error_response(status: int, code: str, retry_at: float | None = None, exact: bool = False) -> JSONResponse:
    metadata = {"reset_time_known": retry_at is not None, "exact_pool_minimum": exact}
    headers = {}
    message = code
    if retry_at is not None:
        seconds = max(1, int(retry_at - time.time() + 0.999))
        metadata.update(retry_after_seconds=seconds, retry_at=datetime.fromtimestamp(retry_at, timezone.utc).isoformat())
        headers["Retry-After"] = str(seconds)
        message = f"{code}; retry after {seconds} seconds"
    return JSONResponse({"error": {"message": message, "type": "gateway_error", "code": code,
                                   "metadata": metadata}}, status_code=status, headers=headers)


class AccountIn(BaseModel):
    plan: str = Field(pattern="^(agent|coding)$")
    api_key: str = Field(min_length=5)
    label: str = Field(min_length=1, max_length=100)
    models: list[str] = Field(default_factory=lambda: ["ark-code-latest"])
    model_mapping: dict[str, str] = Field(default_factory=dict)


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

    def require_service(request: Request):
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
        account_id = store.add_account(body.plan, body.api_key, body.label, body.models)
        if body.model_mapping:
            store.update(account_id, model_mapping=body.model_mapping)
        return store.account(account_id)

    @app.patch("/api/accounts/{account_id}")
    async def edit_account(account_id: str, body: AccountPatch, request: Request):
        require_admin(request, True)
        if not store.account(account_id):
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
        store.update(account_id, **data)
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
                store.update(a["id"], cooldown_kind=None, cooldown_until=None, auth_failed=0)
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

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/v1/models")
    async def models(request: Request):
        require_service(request)
        names = sorted({m for a in store.accounts() if a["enabled"] and not a["auth_failed"] for m in a["models"]})
        return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "ark-plan-gateway"} for m in names]}

    async def proxy(request: Request, method: str, path: str, body: bytes | None, model: str,
                    pinned: str | None, stream: bool = False):
        attempted: set[str] = set()
        while True:
            candidates = await pool.candidates(model, pinned, attempted)
            if not candidates:
                status, code, retry_at, exact = pool.unavailable(model, pinned)
                return error_response(status, code, retry_at, exact)
            account = candidates[0]
            attempted.add(account["id"])
            if not await pool.reserve(account):
                continue
            base = AGENT_URL if account["plan"] == "agent" else CODING_URL
            url = base + path
            headers = {"Authorization": "Bearer " + account["api_key"], "Content-Type": "application/json"}
            if stream:
                headers["Accept"] = "text/event-stream"
            try:
                account_body = body
                mapped = account["model_mapping"].get(model)
                if method == "POST" and mapped and mapped != model:
                    account_json = json.loads(body)
                    account_json["model"] = mapped
                    account_body = json.dumps(account_json, ensure_ascii=False).encode()
                req = client.build_request(method, url, content=account_body, headers=headers)
                upstream = await client.send(req, stream=True)
            except (httpx.TimeoutException, httpx.TransportError):
                await pool.release(account)
                return error_response(502, "upstream_transport_ambiguous")
            if upstream.status_code >= 400:
                raw = await upstream.aread()
                kind, until = classify_error(upstream.status_code, raw, upstream.headers, time.time())
                pool.update_result(account, kind, until)
                await upstream.aclose()
                await pool.release(account)
                if kind in ("quota", "rate", "auth"):
                    continue
                return error_response(upstream.status_code if upstream.status_code < 500 else 502, "upstream_rejected")
            pool.update_result(account, "ok", None)
            if not stream:
                try:
                    raw = await upstream.aread()
                    data = json.loads(raw)
                    if isinstance(data, dict) and data.get("id"):
                        store.bind(data["id"], account["id"])
                    return Response(content=raw, status_code=upstream.status_code,
                                    media_type=upstream.headers.get("content-type", "application/json"))
                except (httpx.TransportError, ValueError):
                    return error_response(502, "upstream_response_failed")
                finally:
                    await upstream.aclose()
                    await pool.release(account)

            async def events():
                buffer = b""
                try:
                    async for chunk in upstream.aiter_raw():
                        buffer += chunk
                        if len(buffer) > 2_000_000:
                            buffer = buffer[-1_000_000:]
                        for line in buffer.split(b"\n")[:-1]:
                            if line.startswith(b"data:"):
                                try:
                                    obj = json.loads(line[5:])
                                    response_obj = obj.get("response", {})
                                    response_id = response_obj.get("id") or obj.get("id")
                                    if response_id and str(response_id).startswith("resp_"):
                                        store.bind(response_id, account["id"])
                                    if isinstance(obj.get("error"), dict):
                                        kind, until = classify_error(429, json.dumps(obj).encode(), {}, time.time())
                                        if kind == "quota":
                                            pool.update_result(account, kind, until)
                                except (ValueError, TypeError, AttributeError):
                                    pass
                        buffer = buffer.rsplit(b"\n", 1)[-1]
                        yield chunk
                except httpx.TransportError:
                    yield b"\nevent: error\ndata: {\"error\":{\"code\":\"upstream_stream_interrupted\"}}\n\n"
                finally:
                    await upstream.aclose()
                    await pool.release(account)

            return StreamingResponse(events(), status_code=upstream.status_code, media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/v1/responses")
    async def responses(request: Request):
        require_service(request)
        raw = await request.body()
        if len(raw) > 8_000_000:
            return error_response(413, "request_too_large")
        try:
            data = json.loads(raw)
        except ValueError:
            return error_response(400, "invalid_json")
        model = data.get("model") if isinstance(data, dict) else None
        if not isinstance(model, str) or not model:
            return error_response(400, "model_required")
        if data.get("background"):
            return error_response(400, "background_not_supported")
        prior = data.get("previous_response_id")
        pinned = store.lookup_binding(prior) if prior else None
        if prior and not pinned:
            return error_response(404, "previous_response_unknown")
        return await proxy(request, "POST", "/responses", raw, model, pinned, bool(data.get("stream")))

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
