"""Administrator-triggered, pinned-account inference checks."""
from __future__ import annotations

import json
import time

import anyio
import httpx

from .audit import audited_request
from .limits import BodyTooLarge, MAX_MANAGEMENT_BYTES, bounded_response, decoded_chunks
from .protocols import ProtocolError, SSEDecoder, request_json

CHECK_TIMEOUT = 60
CHECK_CONCURRENCY = 4
MAX_BATCH = 100


def completed(response):
    if not isinstance(response, dict) or not isinstance(response.get("id"), str) or not response["id"]:
        return False
    if not isinstance(response.get("output"), list):
        return False
    return response.get("status") == "completed" or (
        response.get("status") == "incomplete" and isinstance(response.get("incomplete_details"), dict)
        and response["incomplete_details"].get("reason") == "max_output_tokens")


class AccountChecks:
    def __init__(self, store, client, requests, urls):
        self.store, self.client, self.requests, self.urls = store, client, requests, urls
        self.capacity = anyio.CapacityLimiter(CHECK_CONCURRENCY)
        self.running = set()

    async def check(self, account_id):
        if account_id in self.running:
            return dict(account_id=account_id, status="skipped", code="Gateway.check_in_progress", disabled=False)
        self.running.add(account_id)
        try:
            async with self.capacity:
                account = self.store.account(account_id, private=True)
                if not account or not account["models"]:
                    return dict(account_id=account_id, status="skipped", code="Gateway.account_changed", disabled=False)
                if not self.requests.acquire():
                    return dict(account_id=account_id, status="skipped", code="Gateway.gateway_busy", disabled=False)
                try:
                    return await self._check(account)
                finally:
                    self.requests.release()
        finally:
            self.running.discard(account_id)

    async def batch(self, account_ids):
        # Fixed workers bound both active connections and queued tasks.
        results = [None] * len(account_ids)
        pending = iter(enumerate(account_ids))

        async def worker():
            for index, account_id in pending:
                results[index] = await self.check(account_id)

        async with anyio.create_task_group() as group:
            for _ in range(min(CHECK_CONCURRENCY, len(account_ids))):
                group.start_soon(worker)
        return results

    async def _check(self, account):
        requested_at, started = time.time(), time.monotonic()
        model = account["models"][0]
        upstream_model = account["model_mapping"].get(model, model)
        response_data = {}
        async with audited_request(self.store, "POST", "/api/accounts/{account_id}/check") as audit:
            audit.data["http_status"] = 200  # management result, distinct from upstream_status
            audit.start_attempt(account, model)
            try:
                with anyio.fail_after(CHECK_TIMEOUT):
                    async with self.client.stream("POST", self.urls[account["plan"]] + "/responses",
                            headers={"Authorization": "Bearer " + account["api_key"],
                                     "Accept-Encoding": "identity"},
                            json={"model": upstream_model, "input": "Reply with OK.",
                                  "stream": True, "max_output_tokens": 128},
                            timeout=httpx.Timeout(CHECK_TIMEOUT, connect=10), follow_redirects=False) as response:
                        audit.upstream(response)
                        if not 200 <= response.status_code < 300:
                            audit.upstream_error(await bounded_response(response, MAX_MANAGEMENT_BYTES))
                        elif "text/event-stream" in response.headers.get("content-type", "").lower():
                            response_data = await self._stream(response, audit)
                        else:
                            response_data = request_json(await bounded_response(response, MAX_MANAGEMENT_BYTES))
                            if isinstance(response_data, dict) and response_data.get("error"):
                                audit.upstream_error(json.dumps(response_data).encode())
                            elif not completed(response_data):
                                raise ProtocolError("upstream_response_invalid")
            except httpx.PoolTimeout:
                audit.error("gateway", "Gateway.gateway_connection_pool_busy", "PoolTimeout")
                audit.result("connection_pool_busy")
                return dict(account_id=account["id"], status="skipped",
                            code=audit.data["error_code"], disabled=False, request_id=audit.data["request_id"])
            except (TimeoutError, httpx.TimeoutException) as exc:
                audit.error("gateway", "Gateway.check_timeout", type(exc).__name__)
            except httpx.TransportError as exc:
                code = "upstream_connection_failed" if isinstance(exc, httpx.ConnectError) else "upstream_transport_ambiguous"
                audit.error("gateway", "Gateway." + code, type(exc).__name__)
            except (BodyTooLarge, ProtocolError) as exc:
                code = "upstream_response_invalid" if str(exc) == "invalid_json" else str(exc)
                audit.error("gateway", "Gateway." + code)
            except ValueError as exc:
                audit.error("gateway", "Gateway.upstream_response_invalid", type(exc).__name__)

            success = audit.data["outcome"] == "success"
            result = dict(account_id=account["id"], model=audit.data["model"], upstream_model=audit.clean(upstream_model),
                          status="success" if success else "failed", checked_at=time.time(),
                          duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                          upstream_status=audit.data["upstream_status"], source=audit.data["source"],
                          code=audit.data["error_code"], request_id=audit.data["request_id"], disabled=False)
            # Commit only if the tested credentials and routing configuration still match.
            if not self.store.finish_check(account, result):
                result.update(status="skipped", code="Gateway.account_changed", disabled=False)
            audit.result("success" if success else "check_failed")
            usage = response_data.get("usage") if isinstance(response_data, dict) else None
            usage = usage if isinstance(usage, dict) else {}
            def count(name):
                value = usage.get(name)
                return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
            self.store.record_request(account["id"], "success" if success else "check_failed",
                result["duration_ms"], model=upstream_model, input_tokens=count("input_tokens"),
                output_tokens=count("output_tokens"), requested_at=requested_at)
            return result

    async def _stream(self, response, audit):
        decoder, size = SSEDecoder(), 0

        def observe(event):
            if event is None:
                return None
            data = event.get("response")
            if event.get("error") or event.get("type") in {"error", "response.failed"} or (
                    isinstance(data, dict) and (data.get("error") or data.get("status") == "failed")):
                error = event.get("error") or (data.get("error") if isinstance(data, dict) else None) or event
                audit.upstream_error(json.dumps({"error": error}).encode())
                return data if isinstance(data, dict) else {}
            if event.get("type") in {"response.completed", "response.incomplete"}:
                if not completed(data):
                    raise ProtocolError("upstream_response_invalid")
                return data
            return None

        async for chunk in decoded_chunks(response):
            size += len(chunk)
            if size > MAX_MANAGEMENT_BYTES:
                raise BodyTooLarge("upstream_response_too_large")
            for event in decoder.feed(chunk):
                result = observe(event)
                if result is not None:
                    return result
        for event in decoder.finish():
            result = observe(event)
            if result is not None:
                return result
        raise ProtocolError("upstream_stream_incomplete")
