"""Volcengine plan management queries signed with the official SDK signer."""
from __future__ import annotations

import json
import time

import httpx
from volcengine.auth.SignerV4 import SignerV4
from volcengine.Credentials import Credentials
from volcengine.base.Request import Request

from .pool import exhausted_windows
from .store import Store

MANAGEMENT_URL = "https://ark.cn-beijing.volcengineapi.com/"


async def management_call(action: str, ak: str, sk: str, body: dict) -> dict:
    query = {"Action": action, "Version": "2024-01-01"}
    text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    headers = {"Host": "ark.cn-beijing.volcengineapi.com", "Content-Type": "application/json"}
    request = Request()
    request.host = headers["Host"]
    request.path = "/"
    request.method = "POST"
    request.headers = headers
    request.body = text
    request.query = query
    SignerV4.sign(request, Credentials(ak, sk, "ark", "cn-beijing"))
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(MANAGEMENT_URL, params=query, headers=headers, content=text.encode())
        response.raise_for_status()
        data = response.json()
        if data.get("ResponseMetadata", {}).get("Error"):
            raise RuntimeError("management API rejected request")
        return data.get("Result", {})


async def refresh_account(store: Store, account: dict) -> None:
    if not account["access_key"] or not account["secret_key"]:
        store.update(account["id"], quota_error="未配置 AK/SK")
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
                              "reset_time": int(item["ResetTime"]) / 1000}
            if not usage:
                raise RuntimeError("empty AFP usage")
            store.record_quota(account["quota_group"], "agent", usage, now)
            exhausted = exhausted_windows(usage)
            until = max((x["reset_time"] for x in exhausted), default=None)
            for member in store.accounts():
                if member["quota_group"] == account["quota_group"] and member["plan"] == "agent":
                    store.update(member["id"], usage_json=json.dumps(usage), quota_checked_at=now,
                                 quota_error=None, cooldown_kind="quota" if exhausted else None,
                                 cooldown_until=until)
        else:
            result = await management_call("GetPersonalPlan", account["access_key"], account["secret_key"], {"Plan": "CodingPlan"})
            expired = result.get("Status") != "Running"
            store.update(account["id"], expired=int(expired), quota_checked_at=now, quota_error=None)
    except Exception as exc:
        store.update(account["id"], quota_error=f"查询失败：{type(exc).__name__}")
