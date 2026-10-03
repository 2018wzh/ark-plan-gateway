# Ark Plan Gateway

火山方舟 Agent Plan / Coding Plan 个人版网关。按模型选择可用密钥，在额度耗尽时切换账号；WebUI 提供账号、额度和路由管理。`POST /v1/responses`、`POST /v1/chat/completions` 均支持同步与实时 SSE，`POST /v1/responses/compact` 原生透传压缩请求，`/v1/responses` 还提供 WebSocket 接入，`GET /v1/models` 提供 OpenAI 格式的模型列表。

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

数据库必须使用独立目录，目录中只放该数据库及其 SQLite 辅助文件；启动时会拒绝共享目录，并收紧已有文件权限。POSIX 使用目录 `0700`、文件 `0600`，Windows 使用仅允许运行用户访问的 ACL；权限设置失败时停止启动。配置文件在写入密钥前已限制权限。管理会话签名从外部主密钥派生，不在数据库保存签名密钥；升级到此实现后需重新登录，正常重启不影响有效会话。

## 管理界面

账号页支持按名称、模型、套餐和状态筛选；全局状态展示账号池可用性、可用模型和合并的账号状态数量，不受筛选影响。有冷却账号时显示最短剩余冷却倒计时；部分恢复时间未知时标记为最早已知时间，全部未知时不估算等待时间。各账号的限制窗口逐行显示剩余额度圆环、已用量和重置时间，展开可查看查询状态与共享额度主体。新增账号可同时填写 AK/SK；编辑时密钥留空表示不修改。刷新额度和手动恢复位于每行的更多操作菜单。

统计页展示模型 Token 消耗与等效价格；模型定价和服务设置均需点击保存，离开有未保存修改的页面时会提醒。账号列表后台更新不会覆盖正在填写的表单，网络暂时失败时保留当前数据并提示重试。窄屏通过左上角菜单切换页面。

## 请求审计

管理页的“请求审计”保存 `/v1/` HTTP 请求和 Responses WebSocket 请求帧的历史，支持按时间、来源、完整错误码、模型、HTTP 状态和审计 ID 筛选、分页及展开账号尝试。默认只显示失败、客户端断开和账号切换后恢复的请求；错误分布按最终失败计数，不把切换后成功的请求算作失败。

每条记录包含网关生成的审计 ID、时间、接口模板、模型、账号 ID、套餐、耗时、最终结果，以及上游状态码、请求 ID 和 `error.code/type/param` 元数据。上游 HTTP 错误、HTTP 200 内的 SSE 错误和本地传输/协议故障分别标记来源；传输异常记录异常类型，不记录异常文本。来源表示观测位置，不代表已查明根因。WebSocket 记录不伪造 HTTP 状态码；同一请求最多保留前 15 次和最后一次账号尝试，同时显示实际尝试总数。

审计数据保存在现有私有 SQLite 数据库，重启后保留，最长 30 天且最多 10,000 条，启动、写入和查询时自动清理。日志只记录诊断元数据，不记录请求或响应正文、提示词、生成内容、原始错误消息、Authorization/Cookie、完整 URL、查询参数或 Response ID。已知访问密钥会脱敏；非标准、过长或带控制字符的元数据标记为 `[omitted]`。审计不改写下游错误体或添加诊断字段，已有上游请求 ID 响应头继续透传。

只允许已登录的管理员访问 `GET /api/audit`。例如 `GET /api/audit?days=7&errors_only=true&error_code=MissingParameter`；`limit` 为 1–200，后续页使用响应的 `next_cursor` 作为 `before`。可用来源为 `upstream`、`gateway`、`client`；筛选来源或错误码也会匹配账号尝试记录，因此可以找到切换后恢复的配额错误。审计查询自身不写入请求历史。

日志写入并发有界；存储失败或写入拥塞不会改变请求结果，服务日志给出限频告警，管理页显示本次启动累计的写入失败数。这种情况下历史可能不完整。历史仅从启用此功能之后开始，不回填旧版本发生的错误，也不保证在进程崩溃或断电时保留尚未完成的请求。

设计参考 [OWASP 日志指南](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html)的数据排除、日志注入防护、保留上限和日志失败处理建议。

## New API 接入

在 New API 建立普通 **OpenAI Responses** 渠道：Base URL 设为网关地址（不要附加 `/v1`），Key 填 `ARK_GATEWAY_SERVICE_TOKEN`，模型配置为两类套餐共同支持的模型名，如 `ark-code-latest`。关闭该渠道自动禁用，避免账号池的额度错误禁用整条渠道；将额度错误配置为停止 New API 的重复重试。网关内部已对候选账号做一次安全切换。

New API 的不同版本可能调整 Base URL 拼接及错误包装。部署前用 `/v1/responses` 分别验证同步、SSE 和上游错误的传递；不能假定 `Retry-After` 响应头会穿过 New API。未经实例联调的版本不列为已验证版本。

直连示例：

```sh
curl http://127.0.0.1:8000/v1/responses \
  -H "Authorization: Bearer $ARK_GATEWAY_SERVICE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"model":"ark-code-latest","input":"Reply OK","stream":false}'
```

Chat Completions 与模型发现：

```sh
curl http://127.0.0.1:8000/v1/models \
  -H "Authorization: Bearer $ARK_GATEWAY_SERVICE_TOKEN"

curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $ARK_GATEWAY_SERVICE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"model":"ark-code-latest","messages":[{"role":"user","content":"Reply OK"}],"stream":false}'
```

OpenAI 兼容客户端的 Base URL 填 `http://127.0.0.1:8000/v1`，使用下游访问令牌。模型列表只返回已启用、未过期且鉴权有效账号配置的模型（去重后包含路由别名）；暂时冷却的模型仍保留，调用时返回等待信息。列表不会自动发现上游所有模型，需先在账号配置中添加模型。

两种生成接口均以 `stream: true` 请求对应套餐上游，适配部分上游要求 `stream must set to be true` 的情况。客户端省略 `stream` 或设为 `false` 时，网关收集流并返回原协议的完整 JSON；设为 `true` 时实时转发 SSE。Chat Completions 额外请求 `stream_options.include_usage: true`，将 `prompt_tokens` / `completion_tokens` 纳入统计与计价，支持汇总工具调用参数与 `reasoning_content`。不做 Chat 与 Responses 之间的协议转换。中途断流不会切换账号重放；非流式请求返回 502，流式请求发送错误事件。Chat 多轮上下文由客户端在 `messages` 中传入，Responses 的 `previous_response_id` 仍绑定原账号。

上游 HTTP 错误保留原状态码、响应体和端到端响应头（包括 Content-Type、Retry-After、请求 ID、限流头及供应商扩展字段）；不添加错误分类字段。网关自身错误使用方舟的 `error.code/message/type` 结构，涉及具体参数时才包含 `param`，等待时间通过 `Retry-After` 响应头提供。缺少等待时间时不进行自动循环调用：

```python
import time
import httpx

response = httpx.post(url, headers=headers, json=body, timeout=180)
if response.status_code == 429:
    seconds = response.headers.get("Retry-After")
    if seconds is not None and seconds.isdigit():
        time.sleep(int(seconds))
```

## 下游错误约定

错误约定以字节火山方舟 [Agent / Coding Plan 推理错误码](https://docs.volcengine.com/docs/ark/error-codes?lang=zh)为准。上游 `error.type`、完整 `error.code`（包含点号后缀）、`message`、`param`、请求 ID 和其他原有字段均保持不变，未知错误也不替换成通用错误。保留错误原有 HTTP 状态码，例如 Coding Plan 的 `400 / Forbidden / InvalidSubscription` 不改成 403。

套餐耗尽错误 `AccountQuotaExceeded`、Coding Plan 的 `QuotaExceeded` 与 Agent Plan 的 `QuotaExceeded.AgentPlanQuotaExceeded` 按原样返回，并暂停同一配额组，尝试其他可用组；账号切换最终失败时返回最后一次上游错误。重置时间以明确的上游时间或 `Retry-After` 为准，支持 `+0800 CST` 等带数字时区的格式；没有可信时间时保持配额暂停，等待额度刷新或手动恢复。内部分类只影响账号调度和管理页，不增加下游字段。SSE 错误事件原样转发，上游已发出明确错误后不追加网关错误；成功响应与 SSE 也保留上游端到端响应头。

没有上游响应可供返回时，网关自身错误沿用方舟错误体结构与 `BadRequest`、`Unauthorized`、`Forbidden`、`TooManyRequests`、`InternalServerError` 等类型。自身错误码使用 `Gateway.` 前缀，例如 `Gateway.invalid_tool_arguments`、`Gateway.plan_pool_cooling_down`；这是网关的错误码，不冒充字节官方错误码，不伪造上游 Request ID。`/v1/` 的鉴权、404 和 405 错误也使用该结构；管理界面 `/api/` 的错误不属于推理协议。

按 [HTTP 代理规则](https://www.rfc-editor.org/rfc/rfc9110.html#section-7.6.1)移除逐跳头及 `Connection` 指定的字段。上游响应经过解压或 SSE 汇总时，不沿用失效的编码、长度或内容校验头。必须进行的适配仅包括账号路由和鉴权、模型别名、上游强制 SSE 的请求方式、非流式汇总及 WebSocket 传输；不重写提示词、工具输出或上游错误含义。

## 原生上下文压缩

`POST /v1/responses/compact` 按 [OpenAI 原生压缩接口](https://developers.openai.com/api/docs/guides/compaction)转发到所选套餐的 `/responses/compact`。仅替换上游鉴权和已配置模型别名，不添加 `stream`、`stream_options` 或摘要提示词，不过滤 reasoning、compaction、空内容或工具历史。上游返回的压缩窗口和错误保持原样；压缩结果 ID 不作为可检索的普通 Response ID 保存。

原生压缩与生成接口中的 `context_management` 均取决于所选字节套餐、模型和上游端点实际支持；网关不模拟加密压缩项，也不自动退回生成接口。上游不支持或参数不符合其要求时，保留原始 400/404 等错误。已验证请求路由与字段保持，尚未验证真实方舟模型的压缩成功率。

`MissingParameter` 只表示上游认为缺少参数，需结合原始 `message`、`param`、请求路径和请求 ID 定位。旧版 `gateway_error / upstream_request_rejected` 封装不能证明具体缺少哪个字段；没有实际请求记录时，不据此补造或修正历史内容。

## Responses WebSocket

连接 `ws://127.0.0.1:8000/v1/responses`（HTTPS 部署使用 `wss://`），握手携带 `Authorization: Bearer <下游访问令牌>`。Codex 自定义 provider 使用 HTTP Base URL，客户端会自行选择 WebSocket：

```toml
model_provider = "ark_gateway"

[model_providers.ark_gateway]
name = "Ark Gateway"
base_url = "http://127.0.0.1:8000/v1"
env_key = "ARK_GATEWAY_SERVICE_TOKEN"
wire_api = "responses"
supports_websockets = true
```

每轮发送顶层 JSON `{"type":"response.create","model":"ark-code-latest","input":"Reply OK"}`。上游 SSE 中的每个 JSON 事件转成一条 WebSocket 文本消息，工具与错误事件保持原内容；HTTP 请求错误使用 WebSocket 的 `type: error`、`status` 外层承载上游原 JSON，不添加 metadata，连接仍可继续使用。反向代理须允许 WebSocket Upgrade。

网关自身 Responses 流错误使用 OpenAI 的顶层 `type/code/message/param/sequence_number`；Chat 流错误使用嵌套 `error`。WS 请求错误使用嵌套 `error` 与 `status`；网关检出的 `invalid_stream_id`、`previous_response_not_found` 遵循 OpenAI 对应协议码和参数字段，其他自身错误使用 `Gateway.` 前缀。预热创建和完成事件带有序号。字节上游错误不会改写为这些网关错误。

这是 WebSocket 到方舟 HTTP/SSE 的桥接。支持多轮、`generate: false` 预热和增量工具输出。预热只在网关内保存请求状态、返回零用量的预热响应，不发起上游生成；下一轮引用该 ID 时合并预热输入。网关保留每个分路最近一次终止响应的输入和完整 output，支持 `store: false` 续轮和从缓存响应跨分路分叉。单帧、待执行消息总量及整个连接的历史缓存各最多 8 MB，缓存超出时淘汰最旧分路历史；缓存不会写入数据库。断开后缓存消失，未存储的会话应重连并发送完整上下文。

按[OpenAI WebSocket 模式](https://developers.openai.com/api/docs/guides/websocket-mode)，同一 `stream_id` 按接收顺序执行，不同分路可以并行，最多 16 个活跃响应、32 个命名分路，默认分路不占命名分路名额。超出并发数的请求排队，队列字节上限之外返回网关队列错误，命名分路超限返回 `websocket_stream_limit_reached`。完全由本地缓存重建、未引用上游存储状态的续轮优先使用原账号；原账号不可用或明确拒绝时，可在其他账号用完整上下文开始这一轮。仍依赖上游 `previous_response_id` 的请求保持原账号绑定。桥接不实现 mid-turn steering，不保证减少上游 Token 或延迟。已开始输出后的失败不会重放，客户端断开会取消全部活跃及排队转发并释放连接。

## 额度语义

Agent Plan 配置对应账号 AK/SK 后，用官方签名 SDK 请求 `GetAFPUsage`；多限制窗口任一耗尽即冷却，恢复时间取已耗尽窗口最晚重置时间。Coding Plan 配置 AK/SK 后请求[官方 Ark CLI 所用的 `GetCodingPlanUsage`](https://github.com/volcengine/ark-cli/blob/main/skills/arkcli-usage/references/arkcli-usage-plan.md)，展示各窗口官方已用百分比和重置时间。推理 API Key 不能直接查询该管理接口；未配置 AK/SK 时额度显示“未知”，仍根据请求中观察到的套餐耗尽错误切换账号。普通 RPM/TPM 限流独立短退避。没有当前上游响应时，无可信重置时间返回 `Gateway.plan_quota_exhausted`；有可信时间返回 `Gateway.plan_pool_cooling_down` 和 `Retry-After`。

同一套餐下属于同一额度主体的多个密钥可在账号编辑页指定同一个额度主体。不同套餐的同名模型可互为候选；需要别名时，在账号编辑页按 `别名=上游模型` 配置模型映射。转发给上游的 `previous_response_id` 始终回到创建它的账号。密钥、提示词、完整上游错误不写入日志；数据库、`.env` 和静态构建物不进入 Git。请备份 `.env` 中的主密钥，否则数据库中的账号密钥无法解密。

## 调用边界

单进程共用资源限额：最多 16 个 WebSocket 连接、32 个在处理的 HTTP 请求或 WebSocket 调用；每连接仍最多 16 个并发调用、32 个命名通道。每连接待处理和在途原始帧合计最多 256 条、8 MB，全局原始帧及历史缓存分别最多 64 MB；历史使用序列化数据保存，断开连接会释放占用。达到限额时返回网关错误，不启动额外上游调用。

推理请求体、非流式上游响应、单个 SSE 事件及 Chat 汇总最多 8 MB，管理请求体最多 512 KB，登录请求最多 4 KB，额度查询响应最多 1 MB；读取请求体的总时限为 30 秒。推理及 SSE JSON 使用 UTF-8，最多嵌套 256 层，工具参数独立解析时同样限制层级，字符串里的括号不计入层级。上游 gzip/deflate 在有限大小的解压块中读取，超过限额会关闭连接并返回网关错误。登录校验在工作线程执行，最多同时 2 次，进程最多突发 10 次、每 10 秒补充 1 次；正常登录、密码轮换和上游错误透传保持原有约定。

响应账号绑定最多保存 100,000 条、最长 30 天，超量清理最旧记录；读取时检查有效期，成功删除上游响应后同时删除绑定。每次生成只记录首个与最终响应 ID，避免每个中间事件写入数据库。过期或被清理的绑定返回原有未知响应错误。

| 场景 | 行为 |
| --- | --- |
| 参数、权限或内容校验失败 | 原样返回，不切换账号修补请求 |
| 套餐耗尽、账号失效或临时限流 | 更新对应账号、配额组或模型的状态，尝试其他可用候选 |
| 连接建立失败 | 按有限重试策略切换；保留已有的最后一份上游错误 |
| 发送结果不明确、已开始输出的断流 | 不重放；关闭连接并释放占用 |
| SSE 正常终止或上游明确流错误 | 原样转发终止事件，结束该次转发，不追加第二个错误 |
| SSE 使用 BOM、LF、CRLF 或 CR | 支持增量解析；未完整终止的事件不伪造为成功 |
| 生成配额冷却期间读取或删除已有响应 | 仍向绑定账号转发，保留上游状态和空响应体 |
| 密钥轮换、账号删除、模型配置改变 | 预留时重新检查；旧密钥或旧配额组的在途结果不污染新状态 |
| 官方额度数据异常或查询失败 | 保留已有暂停状态，不把无效数据当作额度恢复 |
| 全部账号不可用或服务断网 | 返回实际错误与可信的等待时间；不承诺永不失败 |

此网关不能保证上游的可用性，也不把有限用例测试视为穷尽所有边界。持续调用需要可用账号、足够额度、稳定网络与客户端按协议恢复。已授权的 5xx 可用性切换策略见下文；可能已执行的请求存在重复计算风险。

## 上游错误与恢复

错误策略按[方舟错误码文档](https://docs.volcengine.com/docs/ark/error-codes?lang=zh)区分影响范围：

| 情况 | 处理 |
| --- | --- |
| 账号/API 限流、并发超限、无法识别的 429 | 对额度主体短暂退避，切换其他账号，不标记套餐耗尽 |
| 模型/接入点 RPM、TPM、IPM | 只对该额度主体的相应上游模型退避，路由别名共享状态 |
| `ServerOverloaded`、`RequestBurstTooFast` | 同套餐、同上游模型短暂退避，避免连续轮换密钥冲击同一服务；429 或临时 500/502/503/504 时可尝试另一套餐 |
| 已识别套餐额度耗尽 | 保留已知/未知重置时间和原有冷却返回契约 |
| 模型未开通、不支持、设置的模型限额/免费试用耗尽 | 持久化隔离该账号对应模型，其他模型仍可用；修改路由或手动恢复后重新尝试 |
| API Key 无效 | 标记该密钥鉴权失败并切换；更新密钥或手动恢复后再试 |
| 欠费、账号状态异常 | 暂停对应额度主体，等待管理员处理后手动恢复 |
| 工具/MCP 凭据、资源权限、参数、内容、文件/Session 等请求错误 | 返回错误，不停用整把推理密钥，不自动换账号 |
| 临时 500/502/503/504、连接建立失败或超时 | 对该账号模型短暂退避，立即尝试其他可用账号；明确的参数/权限等错误不因 5xx 而重试 |
| 其他 5xx、发送结果不明、已接受流式响应后失败 | 当前请求不重放；服务/传输故障对该账号模型短暂退避 |
| 网关连接池等待超时 | 返回可重试的 503，不隔离上游账号 |

临时服务/连接故障在一次生成请求中最多尝试 3 次，每个账号最多一次；不在请求内等待冷却。切换未成功时返回最后一次上游 HTTP 错误的原状态码与响应体。没有上游 HTTP 错误可供返回时，全部候选不可用返回池状态及可用的 `Retry-After`；达到尝试上限但仍有候选时返回 `503 Gateway.upstream_failover_exhausted`。额度、鉴权等已明确拒绝的切换不消耗临时故障次数。`previous_response_id` 仍绑定原账号，不跨账号重放。5xx 切换优先保障可用性，但可能产生重复的上游计算或计费，无法保证所有上游故障时调用成功。

默认退避按连续失败次数在 10、20、40…300 秒窗口内取 50%–100% 随机延迟；上游有效 `Retry-After` 是最短等待下限，不截短。冷却结束后只允许一个并发请求试探，完整成功才恢复；较早的成功请求不能清除新发生的失败状态。模型隔离、失败次数和冷却会跨重启保留。账号详情展示原因码及恢复时间，手动恢复清除同一额度主体的冷却、模型隔离和鉴权失败状态。

错误分类与白名单原因码仅用于内部账号调度和管理页，不改写下游上游错误，也不持久化完整上游错误消息。请求参数错误不自动切换账号，不重复发送相同请求。SSE 错误事件直接透传；将 SSE 汇总为非流式响应时，返回原错误事件 JSON 和 HTTP 502。

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

计价优先使用上游返回模型的专属定价。已确认的版本名 `doubao-seed-2-1-turbo-260628` 在未单独配置时使用 `doubao-seed-2.1-turbo` 的定价；统计仍保留上游原始模型名，历史用量也会重新估算。其他未知版本、`auto` 和 `ark-code-latest` 不猜测模型身份，未配置专属价时仍按默认定价处理，默认价为空则显示未定价。

分档模型按每次请求的官方输入 Token 数选取档位（千 Token 按 1000 换算），该档输入、输出单价适用于整次请求。每日汇总保留输入长度分组，调整价格后可重新估算。旧汇总没有单次长度，或新请求未报告输入长度，以及超出最大定价档位时，分档 Token 标为未定价，不猜测档位。时段价使用请求开始时间，北京时间周一至周五 09:00–12:00、14:00–18:00 为高峰；历史缺少时段的数据不猜测时段。

统计优先记录上游返回的模型名；未返回时使用显式模型映射或请求模型。`ark-code-latest` 可能在控制台切换实际模型，没有固定官方单价，因此不导入猜测价格。公开快照不自动刷新，不包含已失效的旧价格与没有核实的其他厂商价格。

### v0.8.0 展示与统计

- 首页按 5 小时、每日、每周、每月汇总已启用且未过期的额度主体；共享密钥去重，跨套餐使用已知剩余比例的等权平均，不混加 AFP 与百分比。未知额度单独计数，不按满额计算；未提供每日窗口的套餐不参与每日统计。
- AFP 余额显示至 4 位小数，剩余百分比至 3 位，重置时间至秒。聚合保留查询失败的旧值提示。
- 消耗统计只保留 Token 与等效费用，支持账号、模型、日期范围筛选，默认按小时显示趋势与模型明细，可切换每日；费用显示至 6 位小数。停止收集与返回额度查询历史，账号当前额度继续用于调度。
- 修复每日配额错误识别、冷却被额度刷新提前清除，以及缺失窗口或无可信重置时间时的处理。

小时统计按请求完成时间存入 UTC 小时桶，保留 90 天，定价仍采用请求开始时的价格时段。旧版仅存每日汇总，不能还原小时分布，仍可在每日视图查看；页面标明小时采集起点。

### 工具参数解析失败与 400

`InvalidParameter` 属于请求错误：同一请求不切换账号、不自动重放，也不将账号标记为额度冷却。返回 `retryable: false` 和 `requires_request_change: true`。独立的下游重复请求仍是新的请求，客户端必须修正输入后再发起。

网关在发送前检查 Responses `input[].function_call` 和 Chat `messages[].tool_calls` 中完整的函数参数 JSON，发现损坏时返回 `400 Gateway.invalid_tool_arguments` 及字段位置，不把同一损坏历史继续发送给供应商。不修改工具参数、不伪造工具执行结果，也不校验流式参数片段或 custom 工具的自由格式输入。原生压缩请求不经过该生成历史校验。

Codex 遇到工具解析失败后连续出现 400，需要结合脱敏的供应商错误确认工具历史、调用/结果对应关系或服务端上下文是否有效。网关不会通过切换账号或自动修补 JSON 改变调用语义。
