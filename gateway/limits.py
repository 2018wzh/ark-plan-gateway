"""Shared ingress admission and bounded decoded upstream reads."""
from __future__ import annotations

import time
import zlib
import threading

import anyio
from fastapi import HTTPException, Request
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse

MAX_BODY_BYTES = 8_000_000
MAX_ADMIN_BYTES = 512_000
MAX_LOGIN_BYTES = 4096
MAX_MANAGEMENT_BYTES = 1_000_000
BODY_TIMEOUT = 30


class BodyTooLarge(ValueError):
    pass


def declared_oversize(headers, limit):
    length = headers.get("content-length", "")
    if not length.isascii() or not length.isdecimal():
        return False
    value, ceiling = length.lstrip("0") or "0", str(limit)
    return len(value) > len(ceiling) or len(value) == len(ceiling) and value > ceiling


async def decoded_chunks(response):
    """Bound gzip/deflate output before allocation, rather than after httpx decoding."""
    if response.is_stream_consumed:
        for offset in range(0, len(response.content), 65536):
            yield response.content[offset:offset + 65536]
        return
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in ("identity", "gzip", "deflate"):
        raise ValueError("unsupported upstream content encoding")
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
    prefix = bytearray()
    async for raw in response.aiter_raw():
        if encoding == "identity":
            for offset in range(0, len(raw), 65536):
                yield raw[offset:offset + 65536]
            continue
        if encoding == "deflate" and decoder is None:
            prefix.extend(raw)
            if len(prefix) < 2:
                continue
            raw = bytes(prefix)
            header = int.from_bytes(raw[:2], "big")
            window = zlib.MAX_WBITS if raw[0] & 15 == 8 and header % 31 == 0 else -zlib.MAX_WBITS
            decoder = zlib.decompressobj(window)
            prefix.clear()
        while raw:
            if decoder.eof:
                if encoding != "gzip":
                    raise ValueError("trailing compressed data")
                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            try:
                chunk = decoder.decompress(raw, 65536)
            except zlib.error as exc:
                raise ValueError("invalid compressed response") from exc
            raw = decoder.unused_data if decoder.eof else decoder.unconsumed_tail
            if chunk:
                yield chunk
    if encoding != "identity" and (decoder is None or not decoder.eof):
        raise ValueError("incomplete compressed response")


async def bounded_response(response, limit=MAX_BODY_BYTES):
    if declared_oversize(response.headers, limit):
        raise BodyTooLarge("upstream_response_too_large")
    body = bytearray()
    async for chunk in decoded_chunks(response):
        if len(body) + len(chunk) > limit:
            raise BodyTooLarge("upstream_response_too_large")
        body.extend(chunk)
    return bytes(body)


class Admission:
    """Process-wide, nonwaiting limits for the supported single-worker deployment."""
    def __init__(self, maximum):
        self.maximum = maximum
        self.active = 0
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            if self.active >= self.maximum:
                return False
            self.active += 1
            return True

    def release(self):
        with self.lock:
            self.active -= 1


class LoginThrottle:
    def __init__(self):
        self.tokens = 10.0
        self.updated = time.monotonic()

    def acquire(self):
        now = time.monotonic()
        self.tokens = min(10.0, self.tokens + (now - self.updated) / 10)
        self.updated = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


class SecurityBoundary:
    def __init__(self, app, admin, service, requests, login_throttle, error_response):
        self.app, self.admin, self.service = app, admin, service
        self.requests, self.login_throttle = requests, login_throttle
        self.error_response = error_response

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "").rstrip("/")
        if scope["type"] != "http" or not (path == "/api" or path.startswith("/api/") or path == "/v1" or path.startswith("/v1/")):
            return await self.app(scope, receive, send)
        request = Request(scope)
        inference = path == "/v1" or path.startswith("/v1/")

        def rejected(status, code, headers=None):
            result = self.error_response(status, code) if inference else JSONResponse({"detail": code}, status_code=status)
            result.headers.update(headers or {})
            return result

        # Authenticate before reading anything, including FastAPI's typed JSON bodies.
        try:
            if inference:
                self.service(request)
            elif path != "/api/login":
                self.admin(request, scope["method"] not in ("GET", "HEAD", "OPTIONS"))
        except HTTPException as exc:
            return await rejected(exc.status_code, "invalid_token" if inference else exc.detail, exc.headers)(scope, receive, send)
        if not self.requests.acquire():
            return await rejected(503, "gateway_busy", {"Retry-After": "1"})(scope, receive, send)
        try:
            if path == "/api/login" and scope["method"] == "POST" and not self.login_throttle.acquire():
                return await rejected(429, "login rate limited", {"Retry-After": "10"})(scope, receive, send)
            limit = MAX_BODY_BYTES if inference else MAX_LOGIN_BYTES if path == "/api/login" else MAX_ADMIN_BYTES
            if declared_oversize(request.headers, limit):
                return await rejected(413, "request_too_large")(scope, receive, send)
            body = bytearray()
            try:
                with anyio.fail_after(BODY_TIMEOUT):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            raise ClientDisconnect()
                        chunk = message.get("body", b"")
                        if len(body) + len(chunk) > limit:
                            raise BodyTooLarge()
                        body.extend(chunk)
                        await anyio.lowlevel.checkpoint()
                        if not message.get("more_body", False):
                            break
            except BodyTooLarge:
                return await rejected(413, "request_too_large")(scope, receive, send)
            except TimeoutError:
                return await rejected(408, "request_body_timeout")(scope, receive, send)
            except ClientDisconnect:
                return
            raw = bytes(body)
            del body
            delivered = False

            async def replay():
                nonlocal delivered, raw
                if not delivered:
                    delivered = True
                    result = {"type": "http.request", "body": raw, "more_body": False}
                    raw = b""
                    return result
                return await receive()

            await self.app(scope, replay, send)
        finally:
            self.requests.release()
