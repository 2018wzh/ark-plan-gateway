"""Bounded request metadata; never retain bodies, messages or authorization headers."""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar

import anyio
from starlette.requests import ClientDisconnect
from starlette.websockets import WebSocketDisconnect

from .errors import error_data

current_audit = ContextVar("request_audit", default=None)
MAX_ATTEMPTS = 16
logger = logging.getLogger(__name__)


def route(path):
    path = path.rstrip("/")
    if path in {"/v1/models", "/v1/responses", "/v1/responses/compact", "/v1/chat/completions"}:
        return path
    if path.startswith("/v1/responses/"):
        return "/v1/responses/{response_id}"
    return "/v1/{unknown}"


class RequestAudit:
    def __init__(self, store, method, path, transport="http"):
        self.store = store
        self.started = time.monotonic()
        self.secrets = [store._dec(store.setting("service_token")) or ""]
        self.data = dict(request_id=uuid.uuid4().hex, created_at=time.time(), method=method if method in
            {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"} else "OTHER",
            path=route(path), transport=transport, model="", http_status=None,
            outcome="success", source="", error_code="", error_type="", error_param="",
            account_id="", plan="", upstream_status=None, upstream_request_id="",
            duration_ms=0, attempt_count=0, attempts=[])
        self.active = None

    def clean(self, value):
        if not isinstance(value, str) or not value:
            return ""
        if any(len(secret) >= 8 and secret in value for secret in self.secrets):
            return "[redacted]"
        if not re.fullmatch(r"[A-Za-z0-9_.:/\[\]-]{1,128}", value) or re.search(r"(?i)bearer|sk[-_]|AKLT", value):
            return "[omitted]"
        return value

    def model(self, model):
        self.data["model"] = self.clean(model)

    def start_attempt(self, account, model):
        self.secrets.extend(account.get(key) or "" for key in ("api_key", "access_key", "secret_key"))
        if len(self.secrets) > 1 + 3 * MAX_ATTEMPTS:
            self.secrets = self.secrets[:1 + 3 * (MAX_ATTEMPTS - 1)] + self.secrets[-3:]
        self.model(model)
        self.data["attempt_count"] += 1
        self.active = dict(attempt=self.data["attempt_count"], account_id=account["id"],
            plan=account["plan"], upstream_status=None, upstream_request_id="", outcome="success",
            source="", error_code="", error_type="", error_param="", duration_ms=0)
        self.attempt_started = time.monotonic()
        for key in ("account_id", "plan", "upstream_status", "upstream_request_id"):
            self.data[key] = self.active[key]

    def upstream(self, response):
        if self.active is None:
            return
        request_id = next((response.headers[name] for name in
            ("x-request-id", "x-tt-logid", "x-log-id", "request-id") if response.headers.get(name)), "")
        self.active.update(upstream_status=response.status_code, upstream_request_id=self.clean(request_id))
        self.data.update(upstream_status=response.status_code, upstream_request_id=self.clean(request_id))

    def error(self, source, code, error_type="", param=""):
        fields = dict(outcome="error", source=source, error_code=self.clean(code),
            error_type=self.clean(error_type), error_param=self.clean(param))
        self.data.update(fields)
        if self.active is not None:
            self.active.update(fields)

    def upstream_error(self, body):
        data, error = error_data(body)
        if self.active is not None and not self.active["upstream_request_id"]:
            request_id = self.clean(data.get("request_id") or error.get("request_id"))
            self.active["upstream_request_id"] = request_id
            self.data["upstream_request_id"] = request_id
        self.error("upstream", error.get("code") or "UnknownUpstreamError",
            error.get("type", ""), error.get("param", ""))

    def result(self, outcome):
        if self.active is None:
            return
        if outcome == "success":
            self.data.update(outcome="success", source="", error_code="", error_type="", error_param="")
        elif outcome == "client_disconnected":
            if not self.active["source"]:
                self.error("client", "client_disconnected")
                self.data["outcome"] = "disconnected"
        elif not self.active["source"]:
            codes = {"connection_error": "upstream_connection_failed", "transport_error": "upstream_transport_ambiguous",
                "connection_pool_busy": "gateway_connection_pool_busy", "response_error": "upstream_response_failed",
                "stream_error": "upstream_stream_incomplete"}
            self.error("gateway", "Gateway." + codes.get(outcome, outcome))
        self.active["outcome"] = outcome
        self.active["duration_ms"] = max(0, int((time.monotonic() - self.attempt_started) * 1000))
        attempts = self.data["attempts"]
        if len(attempts) == MAX_ATTEMPTS:
            attempts.pop()  # retain the first 15 attempts and the most recent one
        attempts.append(self.active)
        self.active = None

    async def save(self):
        if self.active is not None:
            self.result("client_disconnected")
        self.data["duration_ms"] = max(0, int((time.monotonic() - self.started) * 1000))
        # Metadata may have preceded selection of an account containing that secret.
        for item in [self.data, *self.data["attempts"]]:
            for field in ("model", "error_code", "error_type", "error_param", "upstream_request_id"):
                if field in item:
                    item[field] = self.clean(item[field])
        if not self.store.audit_capacity.acquire(blocking=False):
            self.storage_failed()
            return
        try:
            with anyio.CancelScope(shield=True):
                try:
                    await anyio.to_thread.run_sync(self.store.record_audit, self.data)
                except Exception:
                    self.storage_failed()
        finally:
            self.store.audit_capacity.release()

    def storage_failed(self):
        with self.store.audit_metrics_lock:
            self.store.audit_write_failures += 1
            if time.monotonic() - self.store.audit_warning_at >= 60:
                self.store.audit_warning_at = time.monotonic()
                logger.warning("Request audit storage failed; history may be incomplete")


def local_error(code, param=None):
    audit = current_audit.get()
    if audit is not None:
        full_code = "Gateway." + code
        previous_type = audit.data["error_type"] if audit.data["error_code"] == full_code else ""
        audit.error("gateway", full_code, previous_type, param=param)


@asynccontextmanager
async def audited_request(store, method, path, transport="http"):
    audit = RequestAudit(store, method, path, transport)
    token = current_audit.set(audit)
    try:
        yield audit
    except BaseException as exc:
        if isinstance(exc, (anyio.get_cancelled_exc_class(), ClientDisconnect, WebSocketDisconnect,
                ConnectionResetError, BrokenPipeError)) or (
                transport == "websocket" and isinstance(exc, OSError)):
            if audit.data["outcome"] != "error":
                audit.error("client", "client_disconnected")
                audit.data["outcome"] = "disconnected"
        else:
            audit.error("gateway", "Gateway.internal_error", type(exc).__name__)
            if audit.data["transport"] == "http" and audit.data["http_status"] is None:
                audit.data["http_status"] = 500
        raise
    finally:
        try:
            await audit.save()
        finally:
            current_audit.reset(token)


class AuditMiddleware:
    def __init__(self, app, store):
        self.app, self.store = app, store

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/v1" or path.startswith("/v1/")):
            return await self.app(scope, receive, send)
        async with audited_request(self.store, scope["method"], path) as audit:
            error_body = bytearray()
            disconnected = False
            completed = False

            async def observed_receive():
                nonlocal disconnected
                message = await receive()
                disconnected |= message["type"] == "http.disconnect"
                return message

            async def observed_send(message):
                nonlocal completed
                if message["type"] == "http.response.start":
                    audit.data["http_status"] = message["status"]
                elif message["type"] == "http.response.body":
                    if (audit.data["http_status"] or 0) >= 400 and not audit.data["source"]:
                        error_body.extend(message.get("body", b"")[:max(0, 4096 - len(error_body))])
                    completed = not message.get("more_body", False)
                await send(message)

            try:
                await self.app(scope, observed_receive, observed_send)
            finally:
                status = audit.data["http_status"] or 0
                if status >= 400 and not audit.data["source"]:
                    _, error = error_data(bytes(error_body))
                    audit.error("gateway", error.get("code") or f"Gateway.http_{status}",
                        error.get("type", ""), error.get("param", ""))
                if disconnected and not completed and audit.data["outcome"] != "error":
                    audit.error("client", "client_disconnected")
                    audit.data["outcome"] = "disconnected"
