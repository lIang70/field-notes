# 可观测性 (Observability)

> 本篇不重复 [`../agent.md`](../agent.md) 的"控制循环"和 [`../multi_agent.md`](../multi_agent.md) 的"编排模式"；本篇聚焦 agent 系统**作为生产服务**时必须有的"观测基建"：日志结构、trace 协议、cost 追踪、失败归因、评测集转化。
>
> 日志的具体落点见 [`./control_loop.md`](./control_loop.md)；失败事件的字段约定见 [`./resilience.md`](./resilience.md)；trace 与 checkpoint 的关系见 [`./state.md`](./state.md)。

## TL;DR

- **可观测 = 日志 + trace + cost + 归因**。日志是"事件流"，trace 是"带因果的图"，cost 是"开销截面"，归因是"把失败定位到 (step / agent / tool / prompt 段)"——四件事一起做才有用。
- **每一步 LLM 调用都是"一次原子事件"**：`timestamp / model / tokens_in / tokens_out / latency / tool_calls / result_hash` 是最小字段集，少一个后面就归因不到。
- **trace 协议选型**：能上 OpenTelemetry 就上（标准化、跨语言、与现有 APM 共用）；LangSmith / Langfuse 是 LLM-native 的"省事"选项——但要预想到"未来要导出"。
- **trace context 必须跨 turn / 跨 agent 显式传递**：`trace_id` / `parent_span_id` 写入 message metadata、tool input、sub-agent 入参；不传就退化成"孤立日志"。
- **失败归因靠"四元组"**：(turn, agent, tool, prompt_segment)；trace 里给每个字段打 tag，定位从"哪里错了"压到"哪一行 prompt 错了"。
- **trace → dataset** 是评测的"原料接口"：把线上 trace 转成 (input, expected_trajectory, expected_outcome)，就能离线 replay 任何失败 case。
- **多 agent 维度的 cost 拆分**找到"成本热点 agent"：同一份 trace 按 `agent.name` group by，直接看钱花在谁身上。
- **反直觉**：**没有 trace 的 agent 系统出问题，几乎必然陷入"玄学调试"**——log 只有"调用了一次 LLM"，不知道它在想什么、引用了什么、为什么这么决定。**上线前先解决观测**。

## 1. 一次 LLM 调用的日志结构（最小字段集）

> 一次"调用" = 一次完整的 `model.generate()` / `model.stream()` / 一次 tool round-trip。每一步都按这个 schema 落一条结构化日志（JSON），不要混进 print / 普通 logger。

```json
{
  "timestamp": "2026-06-08T10:23:45.123Z",
  "trace_id": "0af7651916cd43dd8448eb211c80319c",
  "span_id": "b7ad6b7169203331",
  "parent_span_id": "00f067aa0ba902b7",
  "turn": 3,
  "session_id": "sess_2026_06_08_user_42",
  "agent": {
    "name": "researcher",
    "role": "sub",
    "parent_agent": "orchestrator"
  },
  "model": {
    "provider": "anthropic",
    "name": "claude-sonnet-4-5",
    "version": "20250929"
  },
  "tokens": { "in": 12480, "cache_write": 0, "cache_read": 8192, "out": 423 },
  "latency_ms": 1830,
  "tool_calls": [
    {
      "name": "web_search",
      "args_hash": "sha256:7f3a...",
      "result_hash": "sha256:b21c...",
      "latency_ms": 920
    }
  ],
  "result_hash": "sha256:9d4e...",
  "result_preview": "The 2026 Q1 revenue rose 12%...",
  "stop_reason": "end_turn",
  "error": null,
  "cost_usd": 0.0193
}
```

**字段工程含义**：

- `trace_id` / `span_id` / `parent_span_id`：组成调用树；没有这 3 个字段，trace 不成"图"。
- `tokens.cache_read` 单列：缓存命中是省钱的关键信号；要能按"缓存命中率"算成本节省。
- `tool_calls[].args_hash` / `result_hash`：工具参数和结果**不存原文**只存 hash，原文按 hash 存对象存储；同输入不同 result 是"模型不稳定"的硬指标。
- `cost_usd`：**算好后落库**，不要事后 join model 单价表（单价会变，事后算的数对不上账）。
- `latency_ms` 含**整段 wall-clock**（含网络、queue、tool 调用）；`stop_reason` 必填——`max_tokens` / `safety` / `end_turn` 各自的失败模式完全不同。

> **反直觉**：`stop_reason` = `max_tokens` 几乎一定是上游问题（prompt 写太宽 / output schema 没限制 / 模型跑偏没收敛）——这类失败用 retry 治不好。

## 2. Trace 协议：选什么、怎么传

### 2.1 选型对比

| 协议 / 平台              | 定位                           | 适用场景                      |
| :----------------------- | :----------------------------- | :---------------------------- |
| **OpenTelemetry**        | 通用 trace 协议（行业标准）    | 中大型系统、与现有 APM 共用   |
| **LangSmith**            | LangChain 生态 LLM-native      | LangChain 系项目、快速验证    |
| **Langfuse**             | 开源 LLM-native trace + eval   | 自托管 / 不想被 vendor 锁     |
| **Phoenix (Braintrust)** | trace + eval + experiment 平台 | 商业项目、需要 eval 闭环      |
| **OpenLLMetry**          | OTel 的 LLM 扩展               | 已有 OTel 基建，想加 LLM 语义 |

> **选型小抄**：已有 OTel / Datadog / Honeycomb 基建 → OpenTelemetry + OpenLLMetry；纯 LangChain 想快 → LangSmith；想自托管 + 留数据出口 → Langfuse。**避免 vendor lock**——选型时确认"能不能把我的 trace 导出成标准格式"。

### 2.2 Trace context 怎么跨 turn / 跨 agent 传

> **trace context 必须显式传递**，不能依赖"全局变量 / 线程局部"。这是多 agent 系统观测的命门。

**传递的 3 个层次**：(1) **进程内**用 `context.Context` 携带 `trace_id` / `parent_span_id`；(2) **跨进程 / 跨服务**通过 HTTP header（OTel W3C `traceparent`、A2A `X-Trace-Context`、MCP `Mcp-Trace-Id`）；(3) **跨 turn / 跨 session** 存到 session state / checkpoint 里，resume 时先把 trace 链拼回去。

```text
[session] trace_id=0af7
  └─ [turn 1: orchestrator]
       └─ [span A: orchestrator.think]
            └─ [span B: researcher.run]   parent=A, agent=researcher
                 ├─ [span C: llm.call]   parent=B, model=sonnet
                 └─ [span D: web_search] parent=B, tool=web_search
       └─ [span E: writer.run]          parent=A, agent=writer
  └─ [turn 2: user feedback]
       └─ [span G: orchestrator.think] parent=session, agent=orchestrator
```

**关键工程动作**：sub-agent 启动时显式接 `parent_span_id`（不要让 sub-agent 自己"开新 trace"）；tool 调用把 `trace_id` 透传给下游（MCP server / 远程 API），下游报错时把 trace_id 带回来；prompt / tool input 必须**带 `trace_id` 进入模型**（写在 system message 末尾的隐藏前缀）；跨 turn 时把上一 turn 的 last_span_id 写到下一 turn 的 parent——session 维度上 trace 是一棵树，不是孤立点。

> **反直觉**：**很多 agent 框架的"自动 trace"其实是"自动 log"**——它记录了"调了一次 LLM"，但没有"这次调用是上一次调用的孩子"的语义。要在框架 hook 里**手动建 span 并设 parent**，不能信框架默认行为。

## 3. Token / Cost 追踪

### 3.1 计费模型先理清

**必须把 4 类 token 分开记**（base input / cache write / cache read / output）。典型倍率：base 1×，cache write 1.25×，cache read **0.1×**（最便宜），output **5×–15×**（最贵）。

> **关键经济学**：output 是 base input 的 5–15×。**控制 output 长度比控制 input 长度更省**——把"让模型多写"换成"让模型少说，先列结构再展开"。

### 3.2 三维拆分

按 `model.name` / `agent.name` / `session_id` 三个维度分别 group by（SQL 见 §6）。**工程动作**：把 `cost_usd` **在 span 落库时就算好**（用当时的单价表），不要事后 join——单价会调整，事后算的对不上账。每周更新单价表，对历史 span 做"成本复算"写回独立字段（如 `cost_usd_v2`），**不要覆盖原值**。

### 3.3 每步 cost 上报

- 每个 span 落库时带 `cost_usd`；`cost_usd = (tokens.in - tokens.cache_read) * base_input_price + tokens.cache_write * cache_write_price + tokens.cache_read * cache_read_price + tokens.out * output_price`。
- **分项报**（input / cache / output 三列），方便定位"成本突然爆了"是哪一项。
- 每个 agent / sub-agent 维护自己的 token / cost 预算（`budget_usd_per_session`）；超出 80% 触发警告，100% 触发降级（缩短回答、关闭某个 tool）。

> **Cost 是 observability 的一个截面**。把它和 latency、error rate、tool call count 放同一张 dashboard 上看——单一指标骗人，组合指标才反映真实。

## 4. 失败归因

> 一次失败 = 某个 (turn, agent, tool, prompt_segment) 上的"应该 X 但实际 Y"。**没有 trace 的失败归因只能靠猜**。

### 4.1 归因四元组

把"失败"定位到 4 个坐标：**(turn, agent, tool, prompt_segment)**——分别对应 `session.turn_index` / `span.attributes.agent.name` / `span.attributes.tool.name` / prompt 上 `<seg id="...">` 标签落到 span attribute。

**关键动作**：

- **prompt 分段打 tag**（`<SEG id="system">...</SEG>` / `<SEG id="fewshot_1">...</SEG>` / `<SEG id="retrieved">...</SEG>`），构建时记录每个 tag 的字符数和 hash；失败时能定位"是不是某段 retrieved 内容触发的"。
- **tool 错误分类**：暂时性（429 / 5xx / timeout）→ 重试；确定性（参数校验 / 权限 / 内容过滤）→ fail-fast + 明确报错。分类信息落到 span attribute。
- **模型"软错误"**：模型没崩但**答错了 / 编了 / 漏了**——这才是 agent 系统最常见的失败，靠 **eval** 抓，靠 **trace** 定位（"它在哪个 step 之后开始跑偏"）。

### 4.2 失败事件的日志格式

```json
{
  "event": "llm.call.failed",
  "trace_id": "0af7...",
  "span_id": "b7ad...",
  "agent": "researcher",
  "tool": "web_search",
  "error_type": "rate_limit",
  "error_message": "429 Too Many Requests",
  "retry_count": 2,
  "recoverable": true,
  "prompt_segment_id": "system",
  "context_snapshot_hash": "sha256:..."
}
```

> **`recoverable` 字段必填**——决定上层是 retry / replan / fail-fast。**`context_snapshot_hash`** 指向失败那一刻的 context 快照（不是全文，是 hash），方便离线 replay。详见 [`./resilience.md`](./resilience.md)。

## 5. Trace → Dataset：评测的"原料接口"

> 评测集是"离线问题 + 期望轨迹 + 期望结果"；线上 trace 是"真实问题 + 真实轨迹 + 真实结果"——把后者转成前者，评测就**永远有活水源**。

```text
[线上 trace] → filter(失败/边界/高 cost/负反馈) → sample(均匀) → extract → annotate → dataset
       ↓
[评测集 dataset] → 离线 replay → 评分 → regression budget
```

**关键设计**：

- **每条 case 必带 `origin_trace_id`**：能反查"这道题来自哪次线上跑"，评测失败时直接跳回线上 trace 看完整上下文。
- **保留 cost / latency**：把"成功但 cost 暴涨"的 case 也入库——这些是 regression 隐患。
- **replay 接口**：从 (input, model) 重放一次，跑完和 origin trace 对比 trajectory；trajectory 不一致 = prompt 模板或模型行为漂移。
- **同步给 eval 平台**：pipeline 和 [`../evals.md`](../evals.md) §评测驱动开发 的 CI 串起来，每次 prompt 改动先在"上周真实失败集"上跑一遍。

> **反直觉**：评测集不是"建好就完了"——它是**从线上 trace 持续生长**的。没有 trace 就没有"真实问题"，没有"真实问题"的评测集是真空中的分数。

## 6. 多 agent 维度的 cost / latency 拆分

> 单 agent 看 cost 是一维的；多 agent 看 cost 是一个**矩阵**：`(agent × turn × model × tool)`。**目标是找到"成本热点 agent"**——往往是它 prompt 太长 / model 太贵 / 调了太多 tool。

| 拆分维度     | 看到什么                                | 典型行动                                |
| :----------- | :-------------------------------------- | :-------------------------------------- |
| **by agent** | 哪个 agent 花钱最多 / 调 LLM 次数最多   | 降级到更便宜 model / 压缩 prompt / 缓存 |
| **by turn**  | 哪一轮突然 cost 暴涨                    | 检查是否进入死循环 / prompt 是否退化    |
| **by tool**  | 哪个 tool 返了最多 token / 调了最多次   | 截断 tool 返回 / 加 cache / 换 tool     |
| **by user**  | 哪些用户是"贵客"（长会话 / 高频调 LLM） | 限流 / 引导精简输入                     |

**找到"成本热点 agent"**：

```sql
SELECT agent.name, COUNT(*) AS call_count, SUM(cost_usd) AS total_cost,
       AVG(latency_ms) AS avg_latency,
       SUM(cost_usd) / NULLIF(SUM(tokens.out), 0) AS cost_per_output_token
FROM spans
WHERE span.kind = 'llm.call' AND timestamp > NOW() - INTERVAL '7 days'
GROUP BY agent.name ORDER BY total_cost DESC LIMIT 10;
```

`cost_per_output_token` 是高信号指标——高 cost + 低 output 比例 = 模型"话多"或 prompt 浪费；高 cost + 高 output 比例 = 任务本身就要写很多。

> **进一步**：和 [`../multi_agent.md`](../multi_agent.md) §11 的 trajectory 评测联动——cost 高的 agent 是不是**也**是失败率高的 agent？高 cost + 高失败率 = 这个 agent 既贵又没用，是优化的第一目标。

## 7. 反直觉与工程建议

- **log 不等于 trace**：log 是"事件流"，trace 是"带因果的图"。把 log 当 trace 用 = 调试 agent 必陷入"玄学"。
- **框架的"自动 trace"多半是"自动 log"**：它记录"调了 LLM"，但不建立父子关系。**自己建 span、设 parent**，不能信默认行为。
- **"我跑得好好的"不是上线的依据**：没在生产跑过的 agent 是薛定谔的。**上线前先解决观测**，再谈其他。
- **cost 不是越低越好**：cost 暴跌通常意味着"模型被换便宜了"或"retrieval 漏了"或"few-shot 被砍了"，质量可能同步崩。要看 cost / quality 联合指标。
- **长上下文省 token = 省 cost 是错觉**：1M context × N 次调用 = 快速烧钱；缓存能救一部分，TTFT 仍与长度挂钩。
- **落库的 4 个不变式**：(1) 每一步 LLM 调用都有 span；(2) span 必含 `trace_id` + `parent_span_id`；(3) cost 算好再落库；(4) 失败事件带 `recoverable` + `error_type`。
- **采样策略**：线上全量落 span 会爆存储——按 (user_id hash, error) 采样；100% 保留**失败和边界 case**，正常 case 按 1%–10% 采样。
- **prompt 版本化**：每个 span 带 `prompt_version`（git SHA 或版本号）；CI 上把"prompt 改了"和"eval 跑了"绑在一起。
- **可视化是观测的下半场**：dashboard 要能按 trace_id 看到完整树（哪步、什么 agent、什么 prompt 段、花了多少钱）；业界参考 Langfuse / Phoenix 的 UI。

## 相关链接

- [`../evals.md`](../evals.md)：评测体系；如何消费 trace 作为评测集"原料"
- [`../multi_agent.md`](../multi_agent.md)：多 agent 维度的成本拆分；trajectory 评测
- [`./control_loop.md`](./control_loop.md)：每步调用的日志落点
- [`./resilience.md`](./resilience.md)：失败事件的日志格式与重试 / Replan 分类
- [`./state.md`](./state.md)：trace 与 checkpoint 的关系；resume 时如何拼回 trace 链
- [`../context_engineering.md`](../context_engineering.md)：长上下文的成本与缓存——cost 维度的上游
- [`../tools.md`](../tools.md)：tool 调用如何在 trace 里落地
