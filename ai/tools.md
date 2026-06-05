# 工具使用（Tool Use / Function Calling / MCP）

## TL;DR

- "Tool" 不是模型能力的延伸，而是**模型与确定性世界的接口**：把"猜"换成"调用 + 验证"。spec 写得好不好，决定模型能不能挑对、填对参数。
- 协议层四大主流：**OpenAI function calling、Anthropic tool use、Google Gemini function calling、MCP（Model Context Protocol）**。前三是"模型 API 内置的工具语义"，MCP 是 Anthropic 2024 推出的**跨厂商、跨进程开放协议**，把"暴露工具"和"调用工具"解耦。
- Tool spec 的核心是 **JSON Schema**：`name / description / parameters / required` 决定模型怎么挑、怎么填。**description 比代码重要**——`tool_choice=auto` 时模型的全部决策依据就是 description。
- 工具数量爆炸时（>20–50 个），靠 prompt 全列已经失效。要么**分组 / lazy load**，要么**检索式 tool catalog**（Gorilla / ToolLLM 的思路）。
- 安全的工具系统至少三层：**参数校验**（拒绝 SSRF / 路径穿越 / 注入）+ **执行沙箱**（E2B / Daytona / Docker / WASM / gVisor）+ **审计与最小权限**。

## 相关链接

- `agent.md`：智能体闭环——tool 是"Act"那一段
- `prompt.md`：tool description 本质上是 prompt
- `prompt_engineering.md`：ReAct、结构化输出、Self-Consistency 与 tool 的搭配
- `evals.md`：tool 调用准确率、参数准确率、agent-level 任务成功率的评测方法

## 1. 为什么需要 tool

> 模型能"推理与表达"，但不能"知道实时世界 / 做确定性计算 / 改变外部状态"。Tool 是把这两者**显式划分**的工程手段。

模型**不能可靠做**（不靠工具就会幻觉）：实时 / 私有信息（当前时间、汇率、库存、内部 wiki）、确定性计算（精确算术、统计、JSON 解析）、改变状态（发邮件、转账、写文件、关机）、可验证检索（从已知文档原文摘录并给出位置）。

模型**应该做**（不要交给工具）：理解与改写自然语言、任务分解与决策、综合 tool 结果给可读结论、失败时复盘换路径。

> 判断准则：**需要"对/错"硬判定的事都通过 tool 落地**；"好/不好"软判断让模型做。

Toolformer 论文（Schick et al., 2023, arXiv:2302.04761）证明：让模型自己决定**什么时候调用** API（calculator / QA / search / translate / calendar），只在"调用后能更好预测下文"时才保留——稀疏使用既保留语言能力，又提升下游表现。

## 2. Tool 抽象与 spec

几乎所有协议都用 **JSON Schema**（或子集）作为工具定义的事实标准。一个 tool 至少包含 `name`（唯一标识，**自我解释**优于编号）、`description`（决定**选择正确率**，告诉模型"什么时候用、不用"）、`parameters`（决定**填参正确率**；enum / 默认值大幅降错）、`required`（越少越好）、返回 schema（决定模型如何**读结果**；结构化优于自由文本）。

### 写好 description 的要点

> Description 是模型在 `tool_choice=auto` 模式下**唯一**的选择依据。它是 prompt，不是文档。

- **先讲"做什么"，再讲"什么时候用"**；明确**反例**："do not use for X"
- **参数 description 要具体**：`location: "City name, postal address, or 'lat,lon'"` 远好过 `location: "place"`
- **优先 enum，不要 boolean**：`units: enum["metric", "imperial"]` 比 `is_metric: bool` 更稳
- **不要把上下文塞进参数**：用户 ID、时间这些能从代码侧拿到的，不让模型填
- **同义合并、近义拆分**：模型容易在相似工具间随机选——要么合并，要么 description 写得对立

### 最小 spec 示例

```json
{
  "name": "weather_current",
  "description": "Get current weather for a location. Use only when the user explicitly asks about weather; do not use for general questions about a city.",
  "parameters": {
    "type": "object",
    "properties": {
      "location": {
        "type": "string",
        "description": "City name, postal address, or 'lat,lon'"
      },
      "units": {
        "type": "string",
        "enum": ["metric", "imperial"],
        "default": "metric"
      }
    },
    "required": ["location"],
    "additionalProperties": false
  }
}
```

## 3. 协议对比：OpenAI / Anthropic / Gemini / MCP

> 前三是**模型 API 自带的工具语义**，请求与响应里直接带 `tools` / `tool_use` / `function_call`；MCP 是**跨进程协议**，把"暴露工具"做成可独立部署的 server。

### 3.1 OpenAI Function Calling / Tools

- **消息流**：发送 prompt + tools → 收到 `tool_calls` → 本地执行 → 回填 `role: "tool"`（带 `tool_call_id`）→ 再发送拿最终答案。Responses API 用 `function_call` / `function_call_output` 替代
- **`tool_choice`**：`auto` / `required`（至少一次）/ `{name: ...}`（强制某个）/ `none`；并行默认开（`parallel_tool_calls=false` 关闭）
- **`strict: true`**：基于 Structured Outputs **保证**参数符合 schema；要求 `additionalProperties: false` 且所有字段在 `required` 里（可选字段用 `["string", "null"]`）
- **进阶**：`allowed_tools` 切换子集而不破坏 prompt cache；`tool_search` + `defer_loading` 懒加载大量工具
- **典型坑**：reasoning 模型必须把 reasoning items 与 tool output 一起回传，否则对话断裂

### 3.2 Anthropic Claude Tool Use

- **消息流**：模型停止时 `stop_reason: "tool_use"`，content 里出现 `tool_use` block（`id / name / input`）；本地执行后用 `tool_result` block 回传（含 `tool_use_id`）
- **client tools vs. server tools**：客户端工具由你的代码执行；server tools（`web_search`、`code_execution`、`web_fetch`、`tool_search`）由 Anthropic 服务器执行
- **`tool_choice`**：`auto / any / tool / none`（`any` ≈ OpenAI `required`）；`strict: true` 保证 schema 匹配
- **MCP connector**：API 直接接入远程 MCP server
- **Token 计费**：`tools` 参数、`tool_use` / `tool_result` block 都按 token 算；启用还会注入隐式系统提示（290–800 token）

### 3.3 Google Gemini Function Calling

- **声明**：`tools.functionDeclarations[]`，基于 OpenAPI 子集；模式 `AUTO / ANY / NONE / VALIDATED`——`VALIDATED` 是 Gemini 独有的"schema 严格 + 允许自然语言混合"
- **并行 & 组合调用**：每调用唯一 `id`；Python SDK 的 automatic function calling 可代跑整个循环
- **Gemini 3 / 2.5 thinking**：必须回传 `thought_signature`（SDK 自动），否则推理上下文断裂
- **多模态 functionResponse**：可直接夹 image / PDF / inline data；**原生 MCP** 挂载支持

### 3.4 MCP（Model Context Protocol）

> 2024 年 11 月 Anthropic 推出的开放标准，把"工具/数据/提示"做成可独立部署的 server，被任意 host（Claude Desktop / Cursor / VS Code / ChatGPT）复用。官方比作 "USB-C for AI"。

**架构**：

- **Host**（AI 应用）→ **Client**（每个 server 一个 client）→ **Server**（暴露工具/数据/提示的进程，本地或远程）
- **传输层**：`stdio`（本地子进程）/ `streamable HTTP`（远程，POST + 可选 SSE 流，支持 OAuth）
- **数据层**：**JSON-RPC 2.0** 双向协议，完整 lifecycle（initialize → capability negotiation → operation → shutdown）

**核心原语（server 暴露给 client）**：

| 原语        | 作用                                        | 类比                          |
| :---------- | :------------------------------------------ | :---------------------------- |
| `tools`     | 可执行动作（有副作用）                      | OpenAI/Anthropic 的 function  |
| `resources` | 只读上下文数据（文件、表 schema、API 响应） | RAG 检索结果，server 主动暴露 |
| `prompts`   | 可复用提示模板（含少样本、system prompt）   | 团队共享的 prompt 库          |

**客户端原语（client 反向暴露给 server）**：`sampling/createMessage`（server 反向请求 host LLM 推理，不绑模型 SDK 也能用 LLM）、`elicitation/create`（server 要求用户补充信息或确认）、`logging`（server 把日志推给 client）。

**机制亮点**：`tools/list` + `tools/call` 两步（先发现再调用，server 可发 `notifications/tools/list_changed` 通知 client）；handshake 时双方做能力协商；生态包括 reference servers（`filesystem / git / fetch / memory / sequential-thinking / time`，在 `modelcontextprotocol/servers`）与社区 server（GitHub / Postgres / Slack / Sentry / Puppeteer / Brave Search，在 MCP Registry）。

> MCP 的关键价值不是"换种 spec"，而是**把工具开发和模型开发解耦**：一个 GitHub MCP server 可以同时被 Claude / ChatGPT / Cursor 使用，不必为每个模型 API 重写。

### 3.5 一张表理解差异

| 维度             | OpenAI tools          | Anthropic tool use            | Gemini function calling | MCP                             |
| :--------------- | :-------------------- | :---------------------------- | :---------------------- | :------------------------------ |
| 定位             | 模型 API 内置语义     | 模型 API 内置语义             | 模型 API 内置语义       | 跨进程开放协议（JSON-RPC 2.0）  |
| Spec 格式        | JSON Schema           | JSON Schema（`input_schema`） | OpenAPI 子集            | JSON Schema（inputSchema）      |
| 并行             | 默认开                | 支持                          | 支持（每调用唯一 `id`） | 由 client/host 决定             |
| 严格模式         | `strict: true`        | `strict: true`                | `VALIDATED`             | 由 server 校验                  |
| 内置 server tool | 有限                  | web_search/code_exec          | Google Search/Maps/code | 不适用（server 本身就是开放的） |
| 错误模型         | tool 消息字符串自定义 | `is_error: true`              | response 自定义         | JSON-RPC 标准错误码 + 自定义    |
| 跨厂商复用       | 否                    | 否                            | 否                      | **是**——一个 server 多 host 用  |

> 实务上：**应用内一次性工具用厂商 API（更省事），可复用、跨团队、跨 host 的工具上 MCP**。两者不互斥——OpenAI / Anthropic / Gemini SDK 都已支持挂 MCP server。

## 4. 框架与平台对比

> 协议解决"模型怎么说工具"，框架/平台解决"工具怎么写、怎么集成、怎么共享"。

- **LangChain Tools**：`@tool` 装饰器把函数变工具，`args_schema` 用 Pydantic；`ChatModel.bind_tools()` 把同一组工具转换成 OpenAI / Anthropic / Google 各自 spec。**最广覆盖的适配层**，缺点是抽象层多、调试栈深
- **Composio**：SaaS 化 tool 集成平台（也提供 MCP endpoint），帮你管 100+ 服务（GitHub / Slack / Notion / Gmail …）的 OAuth；`composio.create(user_id=...)` → `session.tools()` 返回 provider-format 的 tool 列表，**auth 被 session 抽象掉**
- **trpc-agent-go**（腾讯 trpc 生态）：`tool` 包提供 Function tool、MCP tool、DuckDuckGo / file toolset；`examples/agenttool` 演示 **agent-as-tool**；`chainagent / parallelagent / cycleagent` 是与工具调用正交的组合层
- **Browser Use**：把 Chromium 做成工具，`Agent(task=..., llm=..., browser=Browser())`；动作 `open / state / click <index> / type / screenshot`
- **E2B / Daytona**：把"隔离 VM 里跑 LLM 生成代码"做成 API。E2B 主打 Linux VM + SDK；Daytona 开源、OCI 兼容、**< 90ms 冷启动**、stateful snapshot 跨 session 持久化
- **其他**：LlamaIndex `FunctionTool / QueryEngineTool`（偏 RAG）、OpenAI Agents SDK、Vercel AI SDK（Next.js 友好）、Microsoft Semantic Kernel（企业 plugin）

## 5. Tool 选型与路由

> 模型"挑哪个工具"本质上是基于 description 的**语义检索问题**。工具 < 10 能在 prompt 里看完再挑，> 50 几乎必然漏选或乱选。

工程化几条路：**分组与命名空间**（OpenAI `"type": "namespace"` 把工具分组如 `crm` / `billing`）；**Lazy load**（OpenAI 的 `defer_loading: true` + `tool_search`；MCP 的 `tools/list_changed` 通知）；**Retrieval-based tool catalog**（把工具描述写进向量库，按 query 检索 top-K 再放进 prompt——Gorilla（Patil et al., 2023, arXiv:2305.15334）与 ToolLLM（Qin et al., 2023, arXiv:2307.16789）的核心做法）；**Router 模型**（用便宜的小模型先做工具路由，再让大模型填参数）；**启发式过滤**（基于用户角色、会话上下文、权限白名单提前裁剪）。

> ToolBench / ToolLLM 在 16,464 个 RapidAPI 工具上验证：直接全列不工作；必须**先检索后调用**，并配合 DFSDT（Depth-First Search Decision Tree）做规划。

## 6. 错误处理与重试

> Tool 与外部世界对接，**5xx / 超时 / 限流 / 参数错**是常态。

铁律：

- **错误必须返回给模型，且"模型能读懂"**：纯 stack trace 没用，要告诉它"是哪类错、能不能重试、是不是参数问题"
- **区分"模型该改 vs. 不该管"**：
  - 参数错（schema 不通过、enum 越界）→ 让模型重试，返回"参数 X 不合法，期望 Y"
  - 外部 5xx / 网络抖动 → 工具内部自重试（带退避），不让模型重试
  - 鉴权失败 → 立刻 abort，不让模型乱试
- **幂等性**：发邮件、转账、写文件**必须**带幂等 key
- **重试上限**：硬性卡 N 次（如 3 次），超出 abort（与 `agent.md` "卡在循环"呼应）
- **结构化错误**：OpenAI 的 tool 消息 / Anthropic 的 `tool_result.is_error=true` / MCP 的 JSON-RPC error code——用这些分类，不要全塞 content
- **void 函数也要明确返回**：OpenAI 官方建议无返回值的函数也回 `"success"`，否则模型会"猜"是不是失败

> 反直觉点：**返回过于详细的错误反而有害**——模型会把堆栈里出现的字段当"可调参数"反复试。给模型看摘要 + 错误码，给开发者看全栈。

## 7. 并行、串行与依赖

模型一轮"应该并行调多个工具吗"是**实际收益 vs. 实际风险**的权衡：

- **天然独立 → 并行**：查 3 个城市天气、读 5 个文件；OpenAI / Anthropic / Gemini 默认就并行
- **有依赖 → 串行**：先 `get_current_location()` 再 `get_weather(location)`；强行并行会让模型编参数
- **写操作 → 默认串行**：并行写有竞态、有部分失败、难回滚
- **混合**：先并行读、汇总后再串行写；这是 ReAct（Yao et al., 2023, arXiv:2210.03629）的典型结构

可以在 prompt / tool description 显式提示："独立查询可并行；写操作请逐个执行并验证"。或在框架层（LangGraph、trpc-agent-go）用 DAG 把依赖固化，不依赖模型自觉。

## 8. 安全与沙箱

> 工具是 agent 系统**最大的攻击面**。一行 `os.system(user_input)` 风格的 tool = 任意命令执行。

**参数校验**（必做）：开 `strict` / `VALIDATED` 让模型层就拒绝越界；业务层再用白名单 / 正则 / 路径 normalize 二次校验。常见攻击面：路径穿越（未 normalize `path`）、SSRF（HTTP 工具未限制 host，被诱导访问 `169.254.169.254`）、命令注入（拼字符串进 shell——shell 工具应**只允许结构化参数**）、SQL 注入（用参数化查询，不要 string concat）。

**执行沙箱**（按隔离强度 vs. 启动成本排序）：

| 方案                    | 隔离 | 冷启动  | 适用                 |
| :---------------------- | :--- | :------ | :------------------- |
| 同进程（Python `exec`） | 无   | 0       | **不要在生产使用**   |
| Docker                  | 中   | 秒级    | 内部工具、单机       |
| gVisor                  | 中高 | 亚秒    | 多租户、轻量隔离     |
| WASM (wasmtime/wasmer)  | 高   | 毫秒级  | 受限语言、纯计算     |
| E2B / Daytona           | 高   | < 100ms | LLM 生成代码常规执行 |
| Firecracker microVM     | 极高 | 百毫秒  | 多租户云、对外服务   |

**最小权限**：每个工具单独配权限（filesystem 只读某目录、HTTP 只能访问白名单 host、DB 只读不写）；危险动作二次确认（MCP `elicitation/create` 就是为此设计）；审计日志（用户/agent/tool/参数/结果）；rate limit（卡死循环的最后防线）。

## 9. Tool composition：agent-as-tool 与 tool chain

**Tool chain**：模型自己把工具按依赖串起来。ReAct（Thought → Action → Observation → …）是最经典的 chain；Gemini 的 "compositional function calling" 把这种串行写进了 SDK。要点：每一步要有 Observation（不要"盲跑"）、失败时回退（换参数/换工具/放弃，不无限重试）、stopping rule（硬限制步数/耗时/成本）。

**Agent-as-tool**：把一个完整 agent（带自己的 prompt / 工具 / 记忆）包装成另一 agent 的 tool。trpc-agent-go 的 `examples/agenttool` 与 OpenAI Agents SDK 都支持。优点：封装复杂性（"代码评审 sub-agent"对外只暴露 `review_code(diff: str) -> Review`）、分层路由（顶层挑大方向，sub-agent 负责领域细节）、独立优化（sub-agent 用更便宜模型）。风险：嵌套调用爆炸（需全局深度上限）、成本不可见（上层一个 tool call = 背后 N 次 LLM 推理）、错误传播（要约定结构化错误格式）。

> 经验：**agent-as-tool 适合"功能可单独验收"的子任务**（代码评审、数据查询、格式转换）；不适合与上层强耦合的子任务。

## 10. 评估

按粒度分层（与 `evals.md` 一致）：

- **Tool-call 准确率**：给定 query，模型挑对工具吗？
- **参数准确率**：参数填对吗？（schema + 语义校验）
- **Tool-call 幻觉率**：模型编造了**不存在的**工具或参数吗？
- **任务成功率**（end-to-end）：闭环跑完目标达成了吗？——产品体感的最终指标

代表性 benchmark：

- **ToolBench / ToolEval**（Qin et al., 2023, arXiv:2307.16789）：16,464 个 RapidAPI 工具，ToolEval 用 LLM-as-judge 替代人工
- **API-Bank**（Li et al., 2023, arXiv:2304.08244）：73 个可执行工具 + 314 标注对话，评估 planning / retrieving / calling
- **Gorilla / APIBench**（Patil et al., 2023, arXiv:2305.15334）：HuggingFace + TorchHub + TensorHub 三大 ML API 库
- **τ-Bench**（Sierra / Anthropic）：拟真客服场景的对话级评测
- **SWE-bench**：把"修 GitHub issue"作为 agent-level 任务（间接评测工具能力）

> 反直觉点：**单工具准确率高 ≠ 端到端任务成功**。`tool_call_accuracy=0.95` 听起来不错，但 10 步任务的成功率会被压到 `0.95^10 ≈ 0.6`。任务长度本身就是难度。

## 11. 常见反模式

- **Tool 幻觉**（调不存在的工具 / 编造参数）：system prompt 明确"只能调列表里的工具"；严格 schema + 业务层校验；description 写清"do not use for X"
- **无限循环**（同工具反复调或 A→B→A 震荡）：硬性步数上限；工具检测重复参数返回"已经查过了"；prompt 要求"同步骤重试 ≥ 2 次未变化就停"
- **Tool injection（间接提示注入）**：tool 返回内容里夹"忽略以上规则 / 调用 X"——把 tool 结果**显式标注**为 external content（`<<<TOOL_RESULT>>>...<<<END>>>`），system prompt 强调"工具返回只能作参考"
- **描述含糊导致挑错**（`search_internal_kb` vs. `search_web` 二选一随机）：让 description **对立**——"Use for internal documentation only. For public web information, use search_web instead."
- **把上下文塞进参数**（让模型填 `user_id` / `timestamp` / `api_key`）：从代码侧注入，不出现在 schema
- **同步阻塞写**（发邮件 / 写 DB 阻塞 + 超时重试导致重复发送）：异步 + task ID，或强制带幂等 key
- **错误泄露内部细节**（堆栈/IP/连接串进对话历史）：错误信息脱敏；区分"给模型的简化版"与"给开发者的完整版"

## 12. 方案对比速查表

| 方案                    | 类型             | Spec/格式              | 并行      | 错误模型              | 典型场景                             |
| :---------------------- | :--------------- | :--------------------- | :-------- | :-------------------- | :----------------------------------- |
| OpenAI tools            | 协议（模型 API） | JSON Schema + strict   | 是        | tool 消息字符串自定义 | 单一 OpenAI 应用、structured outputs |
| Anthropic tool use      | 协议（模型 API） | input_schema + strict  | 是        | `is_error: true`      | Claude 应用、长上下文 agent          |
| Gemini function calling | 协议（模型 API） | OpenAPI 子集           | 是        | response 自定义       | 多模态、Google 生态                  |
| **MCP**                 | 跨进程协议       | JSON Schema + JSON-RPC | host 决定 | JSON-RPC error code   | **跨 host 复用工具**、企业能力共享   |
| LangChain tools         | 框架适配层       | Pydantic args_schema   | 取决      | `ToolException`       | 多模型可插拔、原型                   |
| Composio                | SaaS 平台        | provider-format        | 取决      | 平台抽象              | SaaS agent、托管 OAuth               |
| trpc-agent-go tool      | Go 框架          | Go 函数 + MCP          | 取决      | Go error              | Go 后端 agent、agent-as-tool         |
| Browser Use             | 专用工具         | 内置 action 集         | 否        | 状态返回              | 浏览器自动化                         |
| E2B / Daytona           | 沙箱平台         | 任意代码               | 取决      | exit code + stderr    | LLM 生成代码安全执行                 |
| Gorilla / ToolLLM       | 研究项目         | API spec 微调          | -         | -                     | 大规模 API 调用研究                  |

## 13. 上线前检查清单

- [ ] 每个工具有"做什么 / 什么时候用 / 什么时候不用"三段 description；`required` 尽量少；`parameters` 用 enum 取代 boolean
- [ ] 开启 `strict` / `VALIDATED`，业务层再校验一次（防 SSRF / 路径穿越 / 注入）
- [ ] 工具 > 20 时用分组 / lazy load / 检索；不要一股脑全列
- [ ] 所有外部 I/O 工具有超时、重试、幂等键、最小权限；写操作支持二次确认
- [ ] 执行类工具跑在沙箱；错误返回结构化（错误码 + 简化描述），堆栈不外泄
- [ ] tool 返回内容显式标记为"外部材料"，system prompt 含注入防护
- [ ] 评测覆盖：tool-call 准确率、参数准确率、幻觉率、端到端任务成功率
- [ ] 全链路日志：用户、agent、tool、参数、返回、耗时、token 成本
