"""Quota-aware account selection and upstream failure classification."""
from __future__ import annotations

import asyncio
import datetime as dt
import email.utils
import json
import math
import re
import time

from .store import Store


def exhausted_windows(usage: dict) -> list[dict]:
    return [v for v in usage.values() if isinstance(v, dict) and v.get("quota") is not None
            and float(v["quota"]) > 0 and float(v.get("used", 0)) >= float(v["quota"])]


def remaining_ratio(usage: dict) -> float | None:
    windows = [max(0, (float(v["quota"]) - float(v.get("used", 0))) / float(v["quota"]))
               for v in usage.values() if isinstance(v, dict) and v.get("quota") and float(v["quota"]) > 0]
    return min(windows) if windows else None


def reset_from_error(data: dict, headers: dict, now: float) -> float | None:
    raw = data.get("reset_time") or data.get("resetTime")
    error = data.get("error") or {}
    if isinstance(error, dict):
        raw = raw or error.get("reset_time") or error.get("resetTime")
    if raw is None:
        msg = str(error.get("message", "")) if isinstance(error, dict) else ""
        m = re.search(r"(?:reset(?:s| at)?|恢复(?:于|时间)?)[^\d]{0,12}(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:?\d{2})?|\d{10,13})", msg, re.I)
        raw = m.group(1) if m else None
    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float)) or str(raw).isdigit():
            val = float(raw)
            return val / 1000 if val > 1e11 else val
        return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def short_backoff(headers: dict, now: float) -> float:
    value = headers.get("retry-after", "")
    try:
        return now + min(max(float(value), 1), 60)
    except (ValueError, TypeError):
        try:
            return min(email.utils.parsedate_to_datetime(value).timestamp(), now + 60)
        except (TypeError, ValueError, IndexError):
            return now + 10


def classify_error(status: int, body: bytes, headers: dict, now: float) -> tuple[str, float | None]:
    try:
        data = json.loads(body[:65536])
    except (ValueError, UnicodeError):
        data = {}
    err = data.get("error", {}) if isinstance(data, dict) else {}
    code = str(err.get("code", "")) if isinstance(err, dict) else ""
    msg = str(err.get("message", "")) if isinstance(err, dict) else ""
    if status in (401, 403):
        return "auth", None
    if status == 429:
        if code.startswith("QuotaExceeded.AgentPlan") or (code == "QuotaExceeded" and re.search(r"(?:5.hour|weekly|monthly|usage quota|额度.*(?:耗尽|超出))", msg, re.I)):
            return "quota", reset_from_error(data, headers, now)
        if "RateLimit" in code or "TooManyRequests" in code or "RPM" in msg or "TPM" in msg:
            return "rate", short_backoff(headers, now)
    return "other", None


class AccountPool:
    def __init__(self, store: Store):
        self.store = store
        self.lock = asyncio.Lock()
        self.inflight: dict[str, int] = {}
        self.probing: set[str] = set()
        self.sequence = 0

    def _group_state(self, accounts: list[dict], group: str) -> tuple[str | None, float | None]:
        members = [a for a in accounts if a["quota_group"] == group]
        quota = [a for a in members if a["cooldown_kind"] == "quota"]
        if quota:
            values = [a["cooldown_until"] for a in quota]
            return "quota", max(values) if all(x is not None for x in values) else None
        rate = [a["cooldown_until"] for a in members if a["cooldown_kind"] == "rate" and a["cooldown_until"]]
        if rate:
            return "rate", max(rate)
        return None, None

    async def candidates(self, model: str, pinned: str | None = None, exclude: set[str] | None = None) -> list[dict]:
        now = time.time()
        exclude = exclude or set()
        async with self.lock:
            accounts = self.store.accounts(private=True)
            eligible = []
            for a in accounts:
                if a["id"] in exclude or model not in a["models"] or (pinned and a["id"] != pinned):
                    continue
                if not a["enabled"] or a["auth_failed"] or a["expired"]:
                    continue
                kind, until = self._group_state(accounts, a["quota_group"])
                if kind == "quota" and until is None:
                    continue
                if until and until > now:
                    continue
                if a["quota_group"] in self.probing:
                    continue
                ratio = remaining_ratio(a["usage"])
                eligible.append((a, ratio, bool(kind == "quota")))
            self.sequence += 1
            eligible.sort(key=lambda entry: (entry[1] is None, -(entry[1] or 0),
                                             self.inflight.get(entry[0]["id"], 0),
                                             (hash(entry[0]["id"]) + self.sequence) % max(len(accounts), 1)))
            return [a for a, _, _ in eligible]

    async def reserve(self, account: dict) -> bool:
        async with self.lock:
            current = self.store.account(account["id"])
            if not current:
                return False
            group = current["quota_group"]
            if group in self.probing:
                return False
            kind, until = self._group_state(self.store.accounts(), group)
            if kind == "quota" and until is None:
                return False
            if until and until > time.time():
                return False
            if kind == "quota":
                self.probing.add(group)
            self.inflight[account["id"]] = self.inflight.get(account["id"], 0) + 1
            return True

    async def release(self, account: dict):
        async with self.lock:
            self.inflight[account["id"]] = max(0, self.inflight.get(account["id"], 0) - 1)
            self.probing.discard(account["quota_group"])

    def update_result(self, account: dict, kind: str, until: float | None):
        if kind == "quota":
            for a in self.store.accounts():
                if a["quota_group"] == account["quota_group"]:
                    self.store.update(a["id"], cooldown_kind="quota", cooldown_until=until)
        elif kind == "rate":
            self.store.update(account["id"], cooldown_kind="rate", cooldown_until=until)
        elif kind == "auth":
            self.store.update(account["id"], auth_failed=1)
        elif kind == "ok":
            for a in self.store.accounts():
                if a["quota_group"] == account["quota_group"]:
                    self.store.update(a["id"], cooldown_kind=None, cooldown_until=None)

    def unavailable(self, model: str, pinned: str | None = None):
        now = time.time()
        accounts = [a for a in self.store.accounts() if model in a["models"] and (not pinned or a["id"] == pinned) and a["enabled"]]
        states = []
        for a in accounts:
            if a["auth_failed"] or a["expired"]:
                states.append(("invalid", None))
            else:
                states.append(self._group_state(accounts, a["quota_group"]))
        if not states or all(k == "invalid" for k, _ in states):
            return 503, "no_valid_account", None, False
        quota = [(k, v) for k, v in states if k == "quota"]
        known = [v for _, v in quota if v and v > now]
        if known:
            return 429, "plan_pool_cooling_down", min(known), len(quota) == len(states) and len(known) == len(quota)
        if quota:
            return 429, "plan_quota_exhausted", None, False
        rate = [v for k, v in states if k == "rate" and v and v > now]
        if rate:
            return 429, "rate_limited", min(rate), True
        return 503, "no_available_account", None, False
