"""Provider error policy. Raw messages are used only for classification."""
from __future__ import annotations

import datetime as dt
import email.utils
import json
import math
import random
import re


def gateway_error(status: int, code: str, param: str | None = None, message: str | None = None) -> dict:
    """Local errors use Ark's HTTP error shape, without posing as provider errors."""
    error_type = {401: "Unauthorized", 403: "Forbidden", 404: "NotFound", 409: "Conflict",
                  413: "PayloadTooLarge", 424: "FailedDependency", 429: "TooManyRequests"}.get(
        status, "InternalServerError" if status >= 500 else "BadRequest")
    error = {"code": "Gateway." + code, "message": message or "Gateway: " + code, "type": error_type}
    if param is not None:
        error["param"] = param
    return {"error": error}


MODEL_ERRORS = {"OperationDenied.ServiceNotOpen", "ModelNotOpen", "UnsupportedModel",
                "InvalidEndpointOrModel.NotFound", "InvalidEndpointOrModel.ModelIDAccessDisabled"}
ACCOUNT_ERRORS = {"OperationDenied.ServiceOverdue", "AccountOverdueError", "InvalidAccountStatus", "InvalidSubscription"}
OVERLOAD_ERRORS = {"ServerOverloaded", "RequestBurstTooFast"}
RATE_ERRORS = {"TooManyRequests", "AccountRateLimitExceeded", "APIAccountRpmRateLimitExceeded",
               "ModelAccountRpmRateLimitExceeded", "ModelAccountTpmRateLimitExceeded",
               "ModelAccountIpmRateLimitExceeded", "InflightBatchsizeExceeded",
               "RateLimitExceeded.EndpointRPMExceeded", "RateLimitExceeded.EndpointTPMExceeded",
               "RateLimitExceeded.EndpointFlexTPMExceeded"}
KNOWN_CODES = MODEL_ERRORS | ACCOUNT_ERRORS | OVERLOAD_ERRORS | RATE_ERRORS | {
    "AuthenticationError", "InvalidApiKey", "InvalidAPIKey", "MissingHeader",
    "QuotaExceeded", "QuotaExceeded.AgentPlanQuotaExceeded", "SetLimitExceeded", "SessionQuotaExceeded",
    "MissingParameter", "InvalidParameter", "AccessDenied", "OperationDenied.InvalidState",
    "OperationDenied.UnsupportedPhase", "OperationDenied.FileQuotaExceeded", "OperationDenied.ArkAccessRoleNotFound",
    "OperationDenied.TosAccessDenied", "QuotaExceeded.DoubaoSearchFreeQuotaExceeded",
    "MCPInvalidCredential", "MCPNeedsReauth", "MCPNetworkDenied", "MCPInvalidResponse", "UpstreamUnavailable",
    "RequestTooLarge", "RequestBodyTooLarge", "InputTextRiskDetection", "OutputTextRiskDetection",
    "ContentSecurityDetectionError", "InternalServiceError", "ServiceUnavailable", "PathNotFound",
}


def error_data(body: bytes) -> tuple[dict, dict]:
    try:
        data = json.loads(body[:65536])
    except (ValueError, UnicodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    error = data.get("error", {})
    return data, error if isinstance(error, dict) else {}


def safe_error_code(body: bytes) -> str:
    _, error = error_data(body)
    code = error.get("code")
    if isinstance(code, str) and code.startswith("InvalidParameter."):
        return "InvalidParameter"
    return code if isinstance(code, str) and code in KNOWN_CODES else "UnknownUpstreamError"


def retry_after(headers: dict, now: float) -> float | None:
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = float(value)
        if not math.isfinite(seconds):
            return None
        stamp = now + max(1, seconds)
        return stamp if math.isfinite(stamp) and stamp < 253402300799 else None
    except (ValueError, TypeError, OverflowError):
        try:
            stamp = email.utils.parsedate_to_datetime(value).timestamp()
            return max(now + 1, stamp) if math.isfinite(stamp) else None
        except (TypeError, ValueError, IndexError, OverflowError):
            return None


def short_backoff(headers: dict, now: float, failures: int = 0) -> float:
    # A provider delay is a lower bound, never shortened to a local cap.
    given = retry_after(headers, now)
    delay = min(10 * 2 ** min(failures, 5), 300)
    local = now + random.uniform(delay * .5, delay)
    return max(local, given) if given is not None else local


def reset_from_error(data: dict, now: float) -> float | None:
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    raw = data.get("reset_time") or data.get("resetTime") or error.get("reset_time") or error.get("resetTime")
    if raw is None:
        match = re.search(r"(?:reset(?:s| at)?|恢复(?:于|时间)?)[^\d]{0,12}(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:?\d{2})?|\d{10,13})",
                          str(error.get("message", "")), re.I)
        raw = match.group(1) if match else None
    try:
        if isinstance(raw, (int, float)) or str(raw).isdigit():
            stamp = float(raw)
            stamp = stamp / 1000 if stamp > 1e11 else stamp
        else:
            parsed = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            # A timezone-free upstream reset is not a trustworthy UTC instant.
            if parsed.tzinfo is None:
                return None
            stamp = parsed.timestamp()
        return stamp if math.isfinite(stamp) and now < stamp < 253402300799 else None
    except (ValueError, TypeError, OverflowError):
        return None


def classify_error(status: int, body: bytes, headers: dict, now: float) -> tuple[str, float | None]:
    data, error = error_data(body)
    code = str(error.get("code", ""))
    message = str(error.get("message", ""))
    # Tool credentials and resource permissions must not disable the plan key.
    if code.startswith("MCP") or code == "SessionQuotaExceeded":
        return "request", None
    if code in {"AccessDenied", "OperationDenied.InvalidState", "OperationDenied.UnsupportedPhase",
                "OperationDenied.FileQuotaExceeded", "OperationDenied.ArkAccessRoleNotFound",
                "OperationDenied.TosAccessDenied", "QuotaExceeded.DoubaoSearchFreeQuotaExceeded"}:
        return "permission", None
    if code.startswith("InvalidParameter.") or code in {"MissingParameter", "MissingHeader", "InvalidParameter", "RequestTooLarge", "RequestBodyTooLarge",
                "InputTextRiskDetection", "OutputTextRiskDetection", "PathNotFound"}:
        return "request", None
    if code in ACCOUNT_ERRORS:
        return "account", None
    if code in MODEL_ERRORS:
        return "model", None
    if status == 401 or code in {"AuthenticationError", "InvalidApiKey", "InvalidAPIKey"}:
        return "auth", None
    if code == "SetLimitExceeded" or (code == "QuotaExceeded" and re.search(r"free trial|免费试用", message, re.I)):
        return "model_limit", None
    if code.startswith("QuotaExceeded.AgentPlan") or (code == "QuotaExceeded" and re.search(
            r"(?:5.hour|daily|per.day|weekly|monthly|usage quota|每日|每天|日配额|额度.*(?:耗尽|超出))", message, re.I)):
        resets = [value for value in (reset_from_error(data, now), retry_after(headers, now)) if value is not None]
        return "quota", max(resets) if resets else None
    if status == 429 or code in OVERLOAD_ERRORS | RATE_ERRORS:
        if code in OVERLOAD_ERRORS:
            return "overload", retry_after(headers, now)
        if code.startswith(("RateLimitExceeded.Endpoint", "ModelAccount")):
            return "model_rate", retry_after(headers, now)
        # Unknown 429s are temporary throttles, not invented plan exhaustion.
        return "rate", retry_after(headers, now)
    if status >= 500:
        return "server", retry_after(headers, now)
    if status == 403:
        return "permission", None
    return "request", None
