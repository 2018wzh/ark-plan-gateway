"""Responses WebSocket transport over the gateway's HTTP/SSE routing."""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from contextlib import aclosing
from collections import OrderedDict

import anyio
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

from .protocols import ProtocolError, SSEDecoder, request_json
from .errors import gateway_error
from .audit import audited_request, current_audit

MAX_MESSAGE_BYTES = 8_000_000
MAX_QUEUE_ITEMS = 256
MAX_SHARED_BYTES = 64_000_000


def input_items(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [{"role": "user", "content": value}]
    if isinstance(value, list):
        return value
    raise ProtocolError("invalid_input")


class SocketHistory:
    """Keep only the latest response in memory, including store=false turns."""

    def __init__(self):
        self.response_id = None
        self.snapshot = b""
        self.binding_id = None
        self.stream_id = None
        self.warmup = False
        self.size = 0

    def prepare(self, body, stream_id):
        binding_id = None
        if body.get("previous_response_id") == self.response_id and self.response_id:
            cached_body, output = json.loads(self.snapshot)
            merged = {**cached_body, **body} if self.warmup else dict(body)
            merged["input"] = input_items(cached_body.get("input")) + output + input_items(body.get("input"))
            # The HTTP upstream has no access to this socket's local cache.
            prior = cached_body.get("previous_response_id")
            if prior:
                merged["previous_response_id"] = prior
            else:
                merged.pop("previous_response_id", None)
            body = merged
            binding_id = self.binding_id
        elif str(body.get("previous_response_id", "")).startswith("resp_ws_warmup_"):
            raise ProtocolError("previous_response_not_found")
        if len(json.dumps(body, ensure_ascii=False).encode()) > MAX_MESSAGE_BYTES:
            raise ProtocolError("request_too_large")
        return body, binding_id

    def remember(self, body, response, stream_id, warmup=False, binding_id=None):
        output = response.get("output", [])
        if not response.get("id") or not isinstance(output, list):
            return
        snapshot = json.dumps([body, output], ensure_ascii=False).encode()
        size = len(snapshot)
        if size > MAX_MESSAGE_BYTES:
            self.__init__()
            return
        self.response_id = response["id"]
        self.snapshot = snapshot
        self.binding_id = binding_id if warmup else response["id"]
        self.stream_id = stream_id
        self.warmup = warmup
        self.size = size


async def serve_responses(websocket: WebSocket, validate, generate, resources):
    await websocket.accept()
    send_lock = anyio.Lock()
    histories = OrderedDict()
    lanes = {}
    capacity = anyio.CapacityLimiter(16)
    queued_bytes = 0
    queued_items = 0

    def remember(body, response, stream_id, **kwargs):
        history = SocketHistory()
        history.remember(body, response, stream_id, **kwargs)
        previous = histories.pop(stream_id, None)
        if previous:
            resources.socket_history_bytes -= previous.size
        if history.response_id and resources.socket_history_bytes + history.size <= MAX_SHARED_BYTES:
            histories[stream_id] = history
            resources.socket_history_bytes += history.size
            while sum(h.size for h in histories.values()) > MAX_MESSAGE_BYTES:
                resources.socket_history_bytes -= histories.popitem(last=False)[1].size

    async def send(event, stream_id=None):
        if stream_id is not None:
            event = {**event, "stream_id": stream_id}
        async with send_lock:
            await websocket.send_json(event)

    async def error(code, stream_id=None, status=400):
        if current_audit.get() is None:
            async with audited_request(resources.store, "POST", "/v1/responses", "websocket"):
                return await error(code, stream_id, status)
        payload = gateway_error(status, code)
        payload["error"]["param"] = None
        if code in ("invalid_stream_id", "previous_response_not_found", "websocket_stream_limit_reached"):
            payload["error"].update(code=code, type="invalid_request_error",
                                    param="previous_response_id" if code == "previous_response_not_found" else "stream_id")
        current_audit.get().error("gateway", payload["error"]["code"], payload["error"]["type"], payload["error"].get("param"))
        await send({"type": "error", "status": status, **payload}, stream_id)

    async def send_rejection(result, stream_id):
        try:
            payload = json.loads(result.body)
        except (ValueError, UnicodeError):
            payload = None
        if not isinstance(payload, dict):
            payload = gateway_error(result.status_code, "upstream_rejected",
                                    message=result.body.decode("utf-8", errors="replace"))
            payload["error"]["param"] = None
        elif isinstance(result, JSONResponse) and isinstance(payload.get("error"), dict):
            payload["error"].setdefault("param", None)
            if payload["error"].get("code") == "Gateway.previous_response_unknown":
                await error("previous_response_not_found", stream_id)
                return
        audit = current_audit.get()
        if audit is not None and not audit.data["source"]:
            error_data = payload.get("error", {})
            if not isinstance(error_data, dict):
                error_data = {}
            audit.error("gateway" if isinstance(result, JSONResponse) else "upstream",
                error_data.get("code") or "UnknownUpstreamError", error_data.get("type"), error_data.get("param"))
        await send({**payload, "type": "error", "status": result.status_code}, stream_id)

    async def process(event, stream_id):
        try:
            if "generate" in event and not isinstance(event["generate"], bool):
                raise ProtocolError("generate_must_be_boolean")
            warmup = event.get("generate") is False
            body = {k: v for k, v in event.items() if k not in ("type", "generate", "stream_id")}
            current_audit.get().model(body.get("model"))
            body["stream"] = True
            history = next((h for h in histories.values() if h.response_id == body.get("previous_response_id")), SocketHistory())
            body, binding_id = history.prepare(body, stream_id)
        except (ValueError, UnicodeError) as exc:
            await error(str(exc) if isinstance(exc, ProtocolError) else "invalid_json", stream_id)
            return

        rejected = validate(body)
        if rejected is not None:
            await send_rejection(rejected, stream_id)
            return
        if warmup:
            response = {"id": "resp_ws_warmup_" + secrets.token_hex(12), "object": "response",
                        "created_at": int(time.time()), "model": body["model"], "status": "completed",
                        "output": [], "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}
            remember(body, response, stream_id, warmup=True,
                             binding_id=binding_id or body.get("previous_response_id"))
            # This prepares gateway state without issuing a billable generation.
            await send({"type": "response.created", "sequence_number": 0, "response": {**response, "status": "in_progress"}}, stream_id)
            await send({"type": "response.completed", "sequence_number": 1, "response": response}, stream_id)
            return

        result = await generate(body, binding_id)
        if not isinstance(result, StreamingResponse):
            await send_rejection(result, stream_id)
            return
        decoder = SSEDecoder()
        try:
            async with aclosing(result.body_iterator) as events:
                async for chunk in events:
                    for event in decoder.feed(chunk.encode() if isinstance(chunk, str) else chunk):
                        if event is None:
                            continue
                        if event.get("error") and not event.get("type"):
                            event = {"type": "error", **event}
                        if event.get("type") in ("response.completed", "response.incomplete") and isinstance(event.get("response"), dict):
                            remember(body, event["response"], stream_id)
                        await send(event, stream_id)
                        if event.get("type") in ("response.completed", "response.incomplete", "response.failed", "error"):
                            return
                for event in decoder.finish():
                    if event is not None:
                        if event.get("type") in ("response.completed", "response.incomplete") and isinstance(event.get("response"), dict):
                            remember(body, event["response"], stream_id)
                        await send(event, stream_id)
        except ProtocolError:
            await error("upstream_stream_invalid", stream_id, 502)

    try:
        async with anyio.create_task_group() as group:
            async def worker(queue, stream_id):
                nonlocal queued_bytes, queued_items
                try:
                    while True:
                        raw, size = await queue.get()
                        try:
                            async with audited_request(resources.store, "POST", "/v1/responses", "websocket"):
                                async with capacity:
                                    if not resources.requests.acquire():
                                        await error("gateway_busy", stream_id, 503)
                                        continue
                                    try:
                                        await process(request_json(raw), stream_id)
                                    finally:
                                        resources.requests.release()
                        finally:
                            queued_bytes -= size
                            resources.socket_queue_bytes -= size
                            queued_items -= 1
                            raw = None
                except (WebSocketDisconnect, OSError):
                    group.cancel_scope.cancel()

            try:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        break
                    if message.get("text") is None:
                        await error("text_frame_required")
                        continue
                    stream_id = None
                    try:
                        raw = message["text"].encode()
                        message = None
                        size = len(raw)
                        if size > MAX_MESSAGE_BYTES:
                            raise ProtocolError("request_too_large")
                        if queued_items >= MAX_QUEUE_ITEMS or queued_bytes + size > MAX_MESSAGE_BYTES or resources.socket_queue_bytes + size > MAX_SHARED_BYTES:
                            await error("websocket_request_queue_full", status=429)
                            continue
                        event = request_json(raw)
                        if not isinstance(event, dict) or event.get("type") != "response.create":
                            raise ProtocolError("unsupported_websocket_event")
                        stream_id = event.get("stream_id")
                        if "stream_id" in event and (not isinstance(stream_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", stream_id)):
                            stream_id = None
                            raise ProtocolError("invalid_stream_id")
                        if stream_id not in lanes:
                            if stream_id is not None and len([key for key in lanes if key is not None]) >= 32:
                                raise ProtocolError("websocket_stream_limit_reached")
                            lanes[stream_id] = asyncio.Queue()
                            group.start_soon(worker, lanes[stream_id], stream_id)
                        del event
                        lanes[stream_id].put_nowait((raw, size))
                        queued_bytes += size
                        resources.socket_queue_bytes += size
                        queued_items += 1
                    except (ValueError, UnicodeError, RecursionError) as exc:
                        code = str(exc) if isinstance(exc, ProtocolError) else "invalid_json"
                        await error(code, stream_id, 413 if code == "request_too_large" else 400)
                    finally:
                        raw = None
                        event = None
            except (WebSocketDisconnect, OSError):
                pass
            finally:
                group.cancel_scope.cancel()
    finally:
        resources.socket_queue_bytes -= queued_bytes
        resources.socket_history_bytes -= sum(h.size for h in histories.values())
        # Accepted frames that never reached a worker still have a disconnect history.
        with anyio.CancelScope(shield=True):
            for queue in lanes.values():
                while not queue.empty():
                    queue.get_nowait()
                    async with audited_request(resources.store, "POST", "/v1/responses", "websocket") as audit:
                        audit.error("client", "client_disconnected_before_dispatch")
                        audit.data["outcome"] = "disconnected"
