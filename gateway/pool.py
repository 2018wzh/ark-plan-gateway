"""Quota-aware account selection and upstream failure classification."""
from __future__ import annotations

import asyncio
import time

from .store import Store
from .errors import classify_error, short_backoff


def exhausted_windows(usage: dict) -> list[dict]:
    return [v for v in usage.values() if isinstance(v, dict) and v.get("quota") is not None
            and float(v["quota"]) > 0 and float(v.get("used", 0)) >= float(v["quota"])]


def remaining_ratio(usage: dict) -> float | None:
    windows = [max(0, (float(v["quota"]) - float(v.get("used", 0))) / float(v["quota"]))
               for v in usage.values() if isinstance(v, dict) and v.get("quota") and float(v["quota"]) > 0]
    return min(windows) if windows else None


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

    @staticmethod
    def upstream_model(account: dict, model: str) -> str:
        return account["model_mapping"].get(model, model)

    def model_block(self, account: dict, model: str) -> dict | None:
        real = self.upstream_model(account, model)
        return next((b for b in account["model_blocks"] if b["model"] == real), None)

    def _keys(self, account: dict, model: str) -> set[str]:
        keys = {"group:" + account["quota_group"], "model:" + account["id"] + ":" + self.upstream_model(account, model)}
        block = self.model_block(account, model)
        if block and block["kind"] == "overload":
            keys.add("provider:" + account["plan"] + ":" + self.upstream_model(account, model))
        return keys

    def _state(self, account: dict, model: str, accounts: list[dict]):
        if account["auth_failed"] or account["expired"] or account["cooldown_kind"] == "account":
            return "invalid", None
        states = [self._group_state(accounts, account["quota_group"])]
        block = self.model_block(account, model)
        if block:
            states.append((block["kind"], block["retry_at"]))
        held = [(k, t) for k, t in states if k]
        permanent = [(k, t) for k, t in held if t is None]
        return permanent[0] if permanent else max(held, key=lambda s: s[1]) if held else (None, None)

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
                kind, until = self._state(a, model, accounts)
                if kind and until is None:
                    continue
                if until and until > now:
                    continue
                if self._keys(a, model) & self.probing:
                    continue
                ratio = remaining_ratio(a["usage"])
                eligible.append((a, ratio, bool(kind == "quota")))
            self.sequence += 1
            order = {a["id"]: i for i, a in enumerate(accounts)}
            eligible.sort(key=lambda entry: (entry[1] is None, -(entry[1] or 0),
                                             self.inflight.get(entry[0]["id"], 0),
                                             (order[entry[0]["id"]] - self.sequence) % max(len(accounts), 1)))
            return [a for a, _, _ in eligible]

    async def reserve(self, account: dict, model: str | None = None) -> bool:
        async with self.lock:
            current = self.store.account(account["id"])
            if not current:
                return False
            model = model or account["models"][0]
            keys = self._keys(current, model)
            if keys & self.probing or not current["enabled"]:
                return False
            accounts = self.store.accounts()
            kind, until = self._state(current, model, accounts)
            if kind and until is None:
                return False
            if until and until > time.time():
                return False
            account["_probe_keys"] = set()
            if kind:
                if any(self.inflight.get(a["id"], 0) for a in accounts if a["quota_group"] == current["quota_group"]):
                    return False
                account["_probe_keys"] = keys
                account["_probe_group_states"] = {a["id"]: (a["cooldown_kind"], a["cooldown_until"], a["cooldown_failures"])
                                                   for a in accounts if a["quota_group"] == current["quota_group"]}
                account["_probe_model_state"] = self.model_block(current, model)
                self.probing.update(keys)
            self.inflight[account["id"]] = self.inflight.get(account["id"], 0) + 1
            return True

    async def release(self, account: dict):
        async with self.lock:
            self.inflight[account["id"]] = max(0, self.inflight.get(account["id"], 0) - 1)
            self.probing.difference_update(account.get("_probe_keys", set()))

    def update_result(self, account: dict, kind: str, until: float | None, model: str | None = None, code: str = "UnknownUpstreamError"):
        model = model or account["models"][0]
        now = time.time()
        if kind == "quota":
            for a in self.store.accounts():
                if a["quota_group"] == account["quota_group"]:
                    # Concurrent failures cannot shorten a known later reset.
                    if a["cooldown_kind"] == "quota":
                        until = max(until, a["cooldown_until"]) if until is not None and a["cooldown_until"] is not None else None
                    self.store.update(a["id"], cooldown_kind="quota", cooldown_until=until, cooldown_code=code)
        elif kind == "rate":
            members = [a for a in self.store.accounts() if a["quota_group"] == account["quota_group"]]
            failures = max(a["cooldown_failures"] for a in members)
            backoff = short_backoff({}, now, failures)
            until = max(backoff, until or 0, *(a["cooldown_until"] or 0 for a in members if a["cooldown_kind"] == "rate"))
            for a in members:
                if a["cooldown_kind"] not in ("quota", "account"):
                    self.store.update(a["id"], cooldown_kind="rate", cooldown_until=until, cooldown_failures=failures + 1, cooldown_code=code)
        elif kind == "account":
            for a in self.store.accounts():
                if a["quota_group"] == account["quota_group"]:
                    self.store.update(a["id"], cooldown_kind="account", cooldown_until=None, cooldown_code=code)
        elif kind in ("model", "model_limit", "model_rate", "overload", "server"):
            real = self.upstream_model(account, model)
            targets = [self.store.account(account["id"])]
            if kind == "overload":
                targets = [a for a in self.store.accounts() if a["plan"] == account["plan"] and
                           real in {self.upstream_model(a, m) for m in a["models"]}]
            elif kind in ("model_rate", "model_limit"):
                targets = [a for a in self.store.accounts() if a["quota_group"] == account["quota_group"] and
                           real in {self.upstream_model(a, m) for m in a["models"]}]
            failures = max((b["failures"] for a in targets for b in a["model_blocks"] if b["model"] == real), default=0)
            if kind in ("model_rate", "overload", "server"):
                until = max(until or 0, short_backoff({}, now, failures))
            for a in targets:
                old = next((b for b in self.store.account(a["id"])["model_blocks"] if b["model"] == real), None)
                if old and old["retry_at"] is None and until is not None:
                    continue
                if old and old["retry_at"] and until is not None:
                    until = max(until, old["retry_at"])
                self.store.block_model(a["id"], real, kind, until, code, failures + 1)
        elif kind == "auth":
            self.store.update(account["id"], auth_failed=1, cooldown_code=code)
        elif kind == "ok":
            # Only the recovery probe may clear a hold. An older concurrent
            # successful generation must not erase a newer rejection.
            if not account.get("_probe_keys"):
                return
            for a in self.store.accounts():
                snapshot = account.get("_probe_group_states", {}).get(a["id"])
                if snapshot == (a["cooldown_kind"], a["cooldown_until"], a["cooldown_failures"]) and a["cooldown_until"] is not None and a["cooldown_until"] <= now:
                    self.store.update(a["id"], cooldown_kind=None, cooldown_until=None, cooldown_failures=0, cooldown_code=None)
            current = self.store.account(account["id"])
            block = self.model_block(current, model)
            if block and block == account.get("_probe_model_state") and block["retry_at"] is not None and block["retry_at"] <= now:
                self.store.clear_model_blocks(account["id"], self.upstream_model(account, model))

    def unavailable(self, model: str, pinned: str | None = None):
        now = time.time()
        accounts = [a for a in self.store.accounts() if model in a["models"] and (not pinned or a["id"] == pinned) and a["enabled"]]
        states = []
        for a in accounts:
            if self._keys(a, model) & self.probing:
                states.append(("rate", now + 1))
            else:
                state = self._state(a, model, self.store.accounts())
                if state[0] and state[1] is not None and state[1] <= now and any(
                        self.inflight.get(member["id"], 0) for member in self.store.accounts() if member["quota_group"] == a["quota_group"]):
                    state = ("rate", now + 1)
                states.append(state)
        if not states or all(k == "invalid" for k, _ in states):
            return 503, "no_valid_account", None, False
        known = [(k, t) for k, t in states if t and t > now]
        if known:
            kind, earliest = min(known, key=lambda s: s[1])
            code = "plan_pool_cooling_down" if kind == "quota" else "upstream_unavailable" if kind in ("server", "overload") else "rate_limited"
            return (503 if kind in ("server", "overload") else 429), code, earliest, len(known) == len(states)
        if any(k == "quota" for k, _ in states):
            return 429, "plan_quota_exhausted", None, False
        if any(k in ("model", "model_limit") for k, _ in states):
            return 503, "model_unavailable", None, False
        return 503, "no_available_account", None, False
