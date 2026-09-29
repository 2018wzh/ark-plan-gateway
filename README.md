# Ark Plan Gateway

火山方舟 Agent Plan / Coding Plan 个人版 Responses 网关。按模型选择可用密钥，在额度耗尽时切换账号；WebUI 提供账号、额度和路由管理。`POST /v1/responses` 支持同步与实时 SSE。

## 启动

需要 Python 3.11+、Node 22+，或 Docker。初始化私有配置：

```sh
python -m pip install -e '.[test]'
python -m gateway.setup --source-env /path/to/existing/.env
cd web && npm ci && npm run build && cd ..
python -m uvicorn gateway.main:app --host 127.0.0.1 --port 8000
```

`--source-env` 可省略，此时在生成的 `.env` 中填写 `ARK_AGENT_PLAN_KEYS`、`ARK_CODING_PLAN_KEYS`。两变量均以分号分隔多个密钥。设置脚本不会覆盖已有 `.env`。初始管理密码和下游令牌写在私有 `.env` 中，首次登录后可在设置页轮换；账号密钥在 SQLite 中加密。访问 `http://127.0.0.1:8000/`。

Docker Compose：

```sh
python -m gateway.setup --source-env /path/to/existing/.env
docker compose up -d --build
```

单实例运行，持久卷为 `./data`。若从 GHCR 拉取已发布镜像，先运行 `docker pull ghcr.io/2018wzh/ark-plan-gateway:latest`，再运行 `docker compose up -d --no-build`。公开服务时须通过反向代理启用 HTTPS，并设置可信访问控制。

## New API 接入

在 New API 建立普通 **OpenAI Responses** 渠道：Base URL 设为网关地址（不要附加 `/v1`），Key 填 `ARK_GATEWAY_SERVICE_TOKEN`，模型配置为两类套餐共同支持的模型名，如 `ark-code-latest`。关闭该渠道自动禁用，避免账号池的额度错误禁用整条渠道；将额度错误配置为停止 New API 的重复重试。网关内部已对候选账号做一次安全切换。

New API 的不同版本可能调整 Base URL 拼接及错误包装。部署前用 `/v1/responses` 分别验证同步、SSE、额度错误中 `error.metadata` 的传递；不能假定 `Retry-After` 响应头会穿过 New API。未经实例联调的版本不列为已验证版本。

直连示例：

```sh
curl http://127.0.0.1:8000/v1/responses \
  -H "Authorization: Bearer $ARK_GATEWAY_SERVICE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"model":"ark-code-latest","input":"Reply OK","stream":false}'
```

下游等待以 `error.metadata.retry_after_seconds` 为准；字段缺失时 `plan_quota_exhausted` 表示重置时间未知，不进行自动循环调用：

```python
import time
import httpx

response = httpx.post(url, headers=headers, json=body, timeout=180)
if response.status_code == 429:
    error = response.json().get("error", {})
    if error.get("code") == "plan_pool_cooling_down":
        seconds = error.get("metadata", {}).get("retry_after_seconds")
        if seconds is not None:
            time.sleep(seconds)
```

## 额度语义

Agent Plan 配置对应账号 AK/SK 后，用官方签名 SDK 请求 `GetAFPUsage`；多限制窗口任一耗尽即冷却，恢复时间取已耗尽窗口最晚重置时间。Coding Plan 个人版的 `GetPersonalPlan` 只报告套餐状态，不报告剩余额度；未知额度显示为未知，依据上游套餐耗尽错误切换。普通 RPM/TPM 限流独立短退避。无可信重置时间时返回 `plan_quota_exhausted`；有可信时间时返回 `plan_pool_cooling_down` 和 `Retry-After`。

同一套餐下属于同一额度主体的多个密钥可在账号编辑页指定同一个额度主体。不同套餐的同名模型可互为候选；需要别名时，在账号编辑页按 `别名=上游模型` 配置模型映射。`previous_response_id` 始终回到创建它的账号。密钥、提示词、完整上游错误不写入日志；数据库、`.env` 和静态构建物不进入 Git。请备份 `.env` 中的主密钥，否则数据库中的账号密钥无法解密。

## 开发

```sh
python -m pytest -q tests
cd web && npm run build
```
