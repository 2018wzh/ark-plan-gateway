"""Volcengine plan management queries signed with the official SDK signer."""
from __future__ import annotations

import json
import math
import time

import httpx
from volcengine.auth.SignerV4 import SignerV4
from volcengine.Credentials import Credentials
from volcengine.base.Request import Request

from .pool import exhausted_windows
from .store import Store

AGENT_MANAGEMENT_HOST = "ark.cn-beijing.volcengineapi.com"
CODING_MANAGEMENT_HOST = "open.volcengineapi.com"


def parse_coding_usage(result: dict) -> dict:
    usage = {}
    for item in result.get("QuotaUsage", []):
        if not isinstance(item, dict) or item.get("Level") not in ("session", "weekly", "monthly"):
            continue
        try:
            percent = float(item["Percent"])
            if not math.isfinite(percent) or percent < 0 or percent > 100:
                continue
            raw_reset = float(item.get("ResetTimestamp", -1))
            reset_time = (raw_reset / 1000 if raw_reset > 1e11 else raw_reset) if raw_reset > 0 else None
        except (ValueError, TypeError, KeyError):
            continue
        usage[item["Level"]] = {"quota": 100.0, "used": percent, "reset_time": reset_time, "unit": "percent"}
    return usage


async def management_call(action: str, ak: str, sk: str, body: dict) -> dict:
    query = {"Action": action, "Version": "2024-01-01", "Region": "cn-beijing"}
    text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    host = CODING_MANAGEMENT_HOST if action == "GetCodingPlanUsage" else AGENT_MANAGEMENT_HOST
    headers = {"Host": host, "Content-Type": "application/json"}
    request = Request()
    request.host = headers["Host"]
    request.path = "/"
    request.method = "POST"
    request.headers = headers
    request.body = text
    request.query = query
    SignerV4.sign(request, Credentials(ak, sk, "ark", "cn-beijing"))
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(f"https://{host}/", params=query, headers=headers, content=text.encode())
        response.raise_for_status()
        data = response.json()
        if data.get("ResponseMetadata", {}).get("Error"):
            raise RuntimeError("management API rejected request")
        return data.get("Result", {})


async def refresh_account(store: Store, account: dict) -> None:
    if not account["access_key"] or not account["secret_key"]:
        store.update(account["id"], quota_error="未配置 AK/SK；推理 API Key 无法查询官方额度")
        return
    now = time.time()
    try:
        if account["plan"] == "agent":
            result = await management_call("GetAFPUsage", account["access_key"], account["secret_key"], {})
            usage = {}
            for key in ("AFPFiveHour", "AFPDaily", "AFPWeekly", "AFPMonthly"):
                item = result.get(key)
                if not isinstance(item, dict) or "Quota" not in item:
                    continue
                usage[key] = {"quota": float(item["Quota"]), "used": float(item["Used"]),
                              "reset_time": int(item["ResetTime"]) / 1000, "unit": "afp"}
            if not usage:
                raise RuntimeError("empty AFP usage")
            store.record_quota(account["quota_group"], "agent", usage, now)
            exhausted = exhausted_windows(usage)
            until = max((x["reset_time"] for x in exhausted), default=None) if all(x.get("reset_time") for x in exhausted) else None
            for member in store.accounts():
                if member["quota_group"] == account["quota_group"] and member["plan"] == "agent":
                    state = {"usage_json": json.dumps(usage), "quota_checked_at": now, "quota_error": None}
                    if exhausted or member["cooldown_kind"] == "quota":
                        state.update(cooldown_kind="quota" if exhausted else None, cooldown_until=until)
                    store.update(member["id"], **state)
        else:
            result = await management_call("GetCodingPlanUsage", account["access_key"], account["secret_key"], {})
            usage = parse_coding_usage(result)
            if not usage:
                raise RuntimeError("empty Coding Plan usage")
            store.record_quota(account["quota_group"], "coding", usage, now)
            exhausted = exhausted_windows(usage)
            until = max((x["reset_time"] for x in exhausted), default=None) if all(x.get("reset_time") for x in exhausted) else None
            expired = result.get("Status") not in (None, "Running")
            for member in store.accounts():
                if member["quota_group"] == account["quota_group"] and member["plan"] == "coding":
                    state = {"usage_json": json.dumps(usage), "expired": int(expired),
                             "quota_checked_at": now, "quota_error": None}
                    if exhausted or member["cooldown_kind"] == "quota":
                        state.update(cooldown_kind="quota" if exhausted else None, cooldown_until=until)
                    store.update(member["id"], **state)
    except Exception as exc:
        store.update(account["id"], quota_error=f"查询失败：{type(exc).__name__}")
