"""SSE framing and synchronous output for the two native upstream protocols."""
from __future__ import annotations

import json


class ProtocolError(ValueError):
    pass


class SSEDecoder:
    """Incremental SSE parser; forwarding still uses the original byte chunks."""

    def __init__(self):
        self.buffer = b""
        self.data: list[bytes] = []
        self.size = 0
        self.event_type: str | None = None

    def feed(self, chunk: bytes):
        self.buffer += chunk
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            line = line.removesuffix(b"\r")
            if not line:
                if self.data:
                    data = b"\n".join(self.data)
                    self.data = []
                    self.size = 0
                    if data == b"[DONE]":
                        yield None
                    else:
                        try:
                            event = json.loads(data)
                        except (ValueError, UnicodeError) as exc:
                            raise ProtocolError("invalid_sse_json") from exc
                        if not isinstance(event, dict):
                            raise ProtocolError("invalid_sse_event")
                        if self.event_type:
                            event.setdefault("type", self.event_type)
                        yield event
                self.event_type = None
            elif line.startswith(b"event:"):
                self.event_type = line[6:].strip().decode("utf-8")
            elif line.startswith(b"data:"):
                value = line[5:]
                if value.startswith(b" "):
                    value = value[1:]
                self.data.append(value)
                self.size += len(value)
            if self.size + len(self.buffer.split(b"\n", 1)[0]) > 8_000_000:
                raise ProtocolError("upstream_event_too_large")
        if self.size + len(self.buffer) > 8_000_000:
            raise ProtocolError("upstream_event_too_large")


class StreamResult:
    def __init__(self, chat: bool, collect: bool):
        self.chat = chat
        self.collect = collect
        self.done = False
        self.failed = False
        self.usage_response: dict = {}
        self.response: dict | None = None
        self.choices: dict[int, dict] = {}
        self.finished: set[int] = set()
        self.seen: set[int] = set()
        self.size = 0

    def observe(self, event: dict | None):
        if event is None:
            if self.chat:
                self.done = bool(self.seen) and self.seen <= self.finished
            return
        if event.get("error") or event.get("type") in ("error", "response.failed"):
            self.failed = True
        if not self.chat:
            response = event.get("response")
            if isinstance(response, dict):
                if response.get("usage"):
                    self.usage_response = response
                if event.get("type") in ("response.completed", "response.incomplete"):
                    self.done = True
                    self.response = response if self.collect else None
                if response.get("status") == "failed":
                    self.failed = True
            return

        if event.get("model"):
            self.usage_response["model"] = event["model"]
        if event.get("usage"):
            self.usage_response["usage"] = event["usage"]
        if self.collect:
            self.size += len(json.dumps(event, ensure_ascii=False))
            if self.size > 8_000_000:
                raise ProtocolError("upstream_response_too_large")
            if self.response is None:
                self.response = {}
            self.response.update({k: v for k, v in event.items() if k not in ("choices", "object", "usage")})
            self.response["object"] = "chat.completion"
            if event.get("usage"):
                self.response["usage"] = event["usage"]
        for choice in event.get("choices", []):
            index = choice["index"]
            self.seen.add(index)
            if choice.get("finish_reason") is not None:
                self.finished.add(index)
            if not self.collect:
                continue
            target = self.choices.setdefault(index, {"index": index, "message": {"role": "assistant", "content": None},
                                                     "finish_reason": None, "logprobs": None})
            if choice.get("finish_reason") is not None:
                target["finish_reason"] = choice["finish_reason"]
            logprobs = choice.get("logprobs")
            if isinstance(logprobs, dict):
                if target["logprobs"] is None:
                    target["logprobs"] = {}
                for key, value in logprobs.items():
                    if isinstance(value, list):
                        target["logprobs"].setdefault(key, []).extend(value)
                    else:
                        target["logprobs"][key] = value
            message = target["message"]
            for key, value in choice.get("delta", {}).items():
                if key == "tool_calls":
                    calls = message.setdefault("tool_calls", {})
                    for call in value:
                        tool = calls.setdefault(call["index"], {})
                        for name, item in call.items():
                            if name == "index":
                                continue
                            if name == "function":
                                function = tool.setdefault("function", {})
                                for field, fragment in item.items():
                                    function[field] = function.get(field, "") + fragment
                            else:
                                tool[name] = item
                elif key in ("content", "reasoning_content", "refusal") and isinstance(value, str):
                    message[key] = (message.get(key) or "") + value
                elif value is not None:
                    message[key] = value

    def result(self) -> dict:
        if self.failed or not self.done or self.response is None:
            raise ProtocolError("upstream_stream_incomplete")
        if self.chat:
            self.response["choices"] = [self.choices[i] for i in sorted(self.choices)]
            for choice in self.response["choices"]:
                calls = choice["message"].get("tool_calls")
                if calls is not None:
                    choice["message"]["tool_calls"] = [calls[i] for i in sorted(calls)]
        return self.response
