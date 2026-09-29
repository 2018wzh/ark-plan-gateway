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

## 管理界面

账号页支持按名称、模型、套餐和状态筛选；全局状态展示账号池可用性、可用模型和合并的账号状态数量，不受筛选影响。有冷却账号时显示最短剩余冷却倒计时；部分恢复时间未知时标记为最早已知时间，全部未知时不估算等待时间。各账号的限制窗口逐行显示剩余额度圆环、已用量和重置时间，展开可查看查询状态与共享额度主体。新增账号可同时填写 AK/SK；编辑时密钥留空表示不修改。刷新额度和手动恢复位于每行的更多操作菜单。

统计页展示模型 Token 消耗与等效价格；模型定价和服务设置均需点击保存，离开有未保存修改的页面时会提醒。账号列表后台更新不会覆盖正在填写的表单，网络暂时失败时保留当前数据并提示重试。窄屏通过左上角菜单切换页面。

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

Agent Plan 配置对应账号 AK/SK 后，用官方签名 SDK 请求 `GetAFPUsage`；多限制窗口任一耗尽即冷却，恢复时间取已耗尽窗口最晚重置时间。Coding Plan 配置 AK/SK 后请求[官方 Ark CLI 所用的 `GetCodingPlanUsage`](https://github.com/volcengine/ark-cli/blob/main/skills/arkcli-usage/references/arkcli-usage-plan.md)，展示各窗口官方已用百分比和重置时间。推理 API Key 不能直接查询该管理接口；未配置 AK/SK 时额度显示“未知”，仍根据请求中观察到的套餐耗尽错误切换账号。普通 RPM/TPM 限流独立短退避。无可信重置时间时返回 `plan_quota_exhausted`；有可信时间时返回 `plan_pool_cooling_down` 和 `Retry-After`。

同一套餐下属于同一额度主体的多个密钥可在账号编辑页指定同一个额度主体。不同套餐的同名模型可互为候选；需要别名时，在账号编辑页按 `别名=上游模型` 配置模型映射。`previous_response_id` 始终回到创建它的账号。密钥、提示词、完整上游错误不写入日志；数据库、`.env` 和静态构建物不进入 Git。请备份 `.env` 中的主密钥，否则数据库中的账号密钥无法解密。

## AK/SK 与统计

登录[火山引擎 API 访问密钥页面](https://docs.volcengine.com/docs/6291/65568?lang=zh)，为当前身份创建 Access Key ID 和 Secret Access Key；如果使用 IAM 子用户，在该用户详情的「密钥」页创建。该凭据与模型推理 API Key 不同，应属于套餐所在的账号，并具有查询方舟管理接口的权限。进入网关「账号与额度」→「编辑」，分别填写 Access Key 和 Secret Key，保存后点「刷新」。界面不会回显密钥。

「额度与统计」页可选择全部账号或单个账号、最近 7/30/90 天。官方额度查询结果保存为历史快照，同一额度主体在全局当前额度中只计算一次。Coding Plan 返回百分比而非绝对 Token 数；全局展示多个额度主体时用平均百分比。请求统计按账号和模型记录网关发起的上游尝试，包含结果、耗时及上游返回的 Token 用量；切换账号时一次下游请求会产生多次尝试。未报告的 Token 数不作估算。统计不保存提示词或生成内容，额度快照和每日请求汇总保留 90 天。

「模型定价」页按人民币/百万 Token 设置默认输入、输出定价，并可逐模型覆盖。统计页的“等效价格”只按已报告 Token 和配置单价估算，不是实际账单；缺少单价的 Token 单独计数。旧版请求统计没有模型名称，升级后保留并使用默认定价估算。

统计页以模型消耗为主：输入、输出和总 Token、等效价格汇总，以及每日 Token 堆叠柱状图和价格折线图。支持按账号、模型、近 7/30/90 天筛选；日期按 UTC 汇总。官方额度和查询历史位于下方折叠区，按账号展示，不跟随模型筛选。

## 开发

```sh
python -m pytest -q tests
cd web && npm run build
```


### 公开价格与分档计价

`docs/public-pricing.json` 收录 2026-09-29 核对的火山方舟常规在线推理公开价格，来源为 [官方模型价格](https://docs.volcengine.com/docs/ark/model-pricing?lang=zh)。运行中的本地服务可执行：

```sh
python scripts/import_pricing.py
```

导入仅新增未配置的模型，保留原默认价与已配置模型。导入后可在专属定价页编辑各档单价。所有金额均为人民币/百万 Token；按非音频普通输入估算，不计缓存折扣、缓存存储、音频及低延迟服务附加费用，也不是套餐实际账单。

分档模型按每次请求的官方输入 Token 数选取档位（千 Token 按 1000 换算），该档输入、输出单价适用于整次请求。每日汇总保留输入长度分组，调整价格后可重新估算。旧汇总没有单次长度，或新请求未报告输入长度，以及超出最大定价档位时，分档 Token 标为未定价，不猜测档位。时段价使用请求开始时间，北京时间周一至周五 09:00–12:00、14:00–18:00 为高峰；历史缺少时段的数据不猜测时段。

统计优先记录上游返回的模型名；未返回时使用显式模型映射或请求模型。`ark-code-latest` 可能在控制台切换实际模型，没有固定官方单价，因此不导入猜测价格。公开快照不自动刷新，不包含已失效的旧价格与没有核实的其他厂商价格。
