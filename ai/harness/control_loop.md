# 控制循环 (Control Loop / Agent Driver)

## TL;DR

- 控制循环是把 [`agent.md`](../agent.md) 里 "Observe → Think → Act → Check → Repeat" 的概念图**落到代码**的 driver；它本身不"思考"，只负责把模型的决策**分发、执行、回灌、复测**。
- 一个 session = 一个 while-loop + 显式状态机：`idle → running → awaiting_tool / awaiting_user → done | failed`，每轮 `build prompt → call LLM → parse → dispatch tool → observe → 拼回`。
- 多 agent 4 模式（Chain / Parallel / Cycle / Graph）共享**同一套 driver**；区别只在"下一步"由谁选、怎么等。
- 终止条件是**分层防御**——协议 `finish_reason`、步数上限、超时、成本预算、语义 stopping rule 互相补位，单层永远会被绕过。
- 反直觉点：循环复杂度 ≠ 智能；智能在模型里，driver 是"行动表达的接口"。把状态藏在调用栈、想让一个 loop 套所有事、相信 `finish_reason="stop"` 都属于典型坑。

## 1. 概念 → 接口的映射

[`agent.md`](../agent.md) 第 44-48 行给了概念骨架，对应到 driver 的具体动作是：

| 概念步  | Driver 里的具体动作                                                                 |
| :------ | :---------------------------------------------------------------------------------- |
| Observe | 拼 prompt：用户输入 + 系统消息 + 工具结果消息 + 历史；可能做 retrieval / 上下文裁剪 |
| Think   | 调 LLM（带 `tools` spec）；按需 streaming                                           |
| Act     | 解析 `tool_call` → 路由到具体 handler → 在 sandbox 里执行                           |
| Check   | 把 tool 结果回填为 `role: tool` 消息；verifier / schema 校验；判定 stopping rule    |
| Repeat  | 状态机转移；满足任一终止条件则退出                                                  |

> Driver 是 IO 编排器，**不是决策器**。决策在 LLM / verifier / routing node；driver 只负责"把决策落地"。

## 2. 核心循环的代码形态

最小可用的 driver 是一个 while-loop + 显式状态机：

```python
def run(session: Session) -> Result:
    session.state = State.RUNNING
    while session.state not in TERMINAL:
        step = session.advance()                 # build prompt, pick next agent
        response = llm.stream(step.prompt, tools=step.tools)
        for chunk in response:
            session.observe(chunk)               # text_delta → user; tool delta → buffer
        match response.finish_reason:
            case "tool_use" | "tool_calls":
                for call in response.tool_calls:
                    result = sandbox.run(call, timeout=sandbox_timeout)
                    session.inject_tool_result(call.id, result)
                session.state = State.AWAITING_TOOL
            case "stop" | "end_turn":
                session.mark_done(response.text)
            case "length" | "content_filter" | "error":
                handle_termination(response, session)
        if exceeded(session):                     # 终止条件分层判断
            break
    return session.result
```

### 状态机视图

一个 session 是显式 FSM，不是隐式调用栈：

```text
              ┌──────────┐
   create ──▶ │  idle    │
              └────┬─────┘
                   │ submit
                   ▼
              ┌──────────┐
        ┌────▶│ running  │◀──────────┐
        │     └────┬─────┘           │
  text  │  tool_use│  stop           │ tool result
  delta │          ▼                 │
        │   ┌──────────────┐         │
        │   │awaiting_tool │─────────┘
        │   └──────┬───────┘
        │          │ verifier_pass
        │          ▼
        │   ┌──────────┐
        │   │   done   │
        │   └──────────┘
        │  running 期间 user 中断 / 需要补充信息
        │          ▼
        │   ┌──────────────┐
        └───┤awaiting_user │
            └──────┬───────┘
                   │ user reply
                   ▼
              (回 running)
   任意状态遇到 error / 超出终止阈值 ─▶ ┌──────────┐
                                        │ failed   │
                                        └──────────┘
```

> **显式状态 vs 隐式栈**：step-by-step 的局部变量当 state，进程一挂就要从头跑。state 必须持久化、checkpoint、恢复——见 `./state.md`。

## 3. 解析输出：三种 finish_reason 的分发

LLM 一轮的返回里 driver 只关心三件事，按出现顺序处理：

- **text_delta / message**：累积成 assistant 消息；流式下逐 chunk 推前端
- **tool_call / tool_use**：把每个 call 路由到对应 handler；多轮并行 call 在同一 `finish_reason=tool_use` 里一起 dispatch
- **finish_reason**（终止信号 + 协议合规判断）：

| finish_reason               | driver 行为                                                     |
| :-------------------------- | :-------------------------------------------------------------- |
| `stop` / `end_turn`         | 自然结束 → `done`                                               |
| `tool_use` / `tool_calls`   | 协议要求必须执行至少一个 tool，否则视作协议违规                 |
| `length`                    | 截断——是否补一轮（提醒 "continue"）还是直接 `failed` 是策略选择 |
| `content_filter` / `safety` | 按 `failed` 处理；可能需要 escalate 或人工审核                  |
| `error`                     | driver 层面重试（见 [`./resilience.md`](./resilience.md)）      |

## 4. 终止条件：分层防御

> 终止条件不是"一个 `if`"，是**多层**——单层永远会被绕过。

按"协议 → 资源 → 语义"自下而上：

- **协议层**：`finish_reason ∈ {stop, end_turn}` → `done`
- **步数层**：`step_count >= max_steps` → `failed`（死循环的硬卡，必须有）
- **时间层**：`wall_clock > max_duration` → `failed`
- **成本层**：`token_used > cost_budget` → `failed`
- **错误层**：连续 N 次同类型错误 → `failed`（同时是 replan 的触发器）
- **语义层（stopping rule）**：verifier pass / 目标信号出现 → 提前 `done`

`finish_reason="stop"` **不可信**——模型可能"想停但说没完"，也可能"已经答完但忘了 finish"。语义层是真正决定"任务做完没"的一层，详见 [`./resilience.md`](./resilience.md) 和 [`../evals.md`](../evals.md)。

## 5. 同一个 driver，4 种拓扑

[`../multi_agent.md`](../multi_agent.md) §3 介绍的 Chain / Parallel / Cycle / Graph 共享**同一套 driver**——区别只是"下一步"由谁选、怎么等：

| 拓扑     | driver 的"下一步"逻辑                                         |
| :------- | :------------------------------------------------------------ |
| Chain    | `next = sequence[i+1]`；一次 driver 循环只调一个 agent        |
| Parallel | `next = fan_out(all agents)`；driver 等 `gather(*calls)` 完成 |
| Cycle    | `next = verifier_or_replan`；不通过则回到起点，形成条件回边   |
| Graph    | `next = routing_node(state)`；driver 退化为"走图边的一条循环" |

> 一个具体观察：LangGraph 的 `Pregel` runtime / trpc-agent-go 的 `GraphAgent` 都可以看成"**一个 driver 在执行一张图**"。把 driver 抽象出来后，多 agent 不再是"另一套系统"，而是 driver 的不同配置。

## 6. streaming vs non-streaming

token 级流式对 driver 的影响不在"快一点"，而在"**状态机可以在中途被打断 / 改道**"：

- **非流式**：一次拿完整 response → 一次解析 tool_call → 一次 dispatch。协议分发简单，但首 token latency = 整段生成时间，**用户无法在生成中干预**。
- **流式**：边收 chunk 边分发——text chunk 推前端、tool_call chunk 累积到完整 JSON 再 dispatch。
  - Anthropic SSE 用 `content_block_start` / `content_block_delta` / `content_block_stop` 标记 tool_call 块边界
  - OpenAI 用 `delta.tool_calls` 增量累积，需要按 `index` 拼装
- **中断语义**：流式下用户可以"在生成中按停"——abort signal 传到 LLM 连接、关 sandbox、partial state 落 checkpoint。详见 [`./io.md`](./io.md)。

> **反直觉**：流式的核心收益不是 latency，是**可交互性**。首 token 提前是副作用，能在 token 级别 cancel / steer / 追加输入才是设计目标。

## 7. tool_call 解析的工程细节

driver 要兼容三种 tool call 来源：

- **function calling JSON**（OpenAI `tools` / Anthropic `tool_use` / Gemini `functionDeclarations`）：结构化、`id` + `name` + `arguments`，driver 直接 dispatch
- **文本嵌入的工具指令**（ReAct 风格的 `Action: ... Action Input: {...}`）：需要正则 / grammar 解析；多轮 batch 时按出现顺序拼回
- **多轮 tool call 的批处理**：同一 `finish_reason=tool_use` 里可能有 N 个 call，driver 要**并行**调（前提是相互独立，详见 [`../tools.md`](../tools.md) §7），再统一回填

## 8. 反直觉点

> **控制循环不是 agent "思考"的引擎，而是 agent 表达"行动"的接口。**

工程上最容易踩的坑：

- **把"循环复杂"当"智能"**：loop 越复杂不代表 agent 越聪明；智能在模型里，driver 是脚手架
- **状态藏在调用栈**：step 的局部变量当 state，restart 就要从头跑。**state 必须显式 + 持久化**
- **一个 loop 套所有事**：单 agent / 多 agent / 反思 / 计划执行是不同拓扑，硬塞进同一个 while 只会越来越难调
- **相信 `finish_reason="stop"`**：模型可能答完但忘了 finish，也可能想停但说没完。一定要有语义层 stopping rule 兜底
- **driver 替模型做决策**：路由 / 选工具 / 选下一步应该是 LLM 或显式 routing node；driver 不要写"if step == 3 then ..."这种隐式策略

## 9. 上线前检查清单

- [ ] driver 显式维护 FSM；状态变更可观测（trace + log + checkpoint）
- [ ] 三种 finish_reason 都有分支；`length` / `content_filter` / `error` 有显式策略
- [ ] 多层终止条件齐全：协议 + 步数 + 时间 + 成本 + 语义 stopping rule
- [ ] 同一套 driver 跑得动 Chain / Parallel / Cycle / Graph 四种拓扑（参数化）
- [ ] 流式 + 非流式共用一套状态机（不要写两套）
- [ ] tool_call 解析兼容 function-calling JSON 与文本嵌入指令；多轮 batch 正确回填
- [ ] 用户中断、模型卡死、sandbox 崩溃各自有兜底；abort 信号能传到 LLM 连接
- [ ] 错误对模型"可读"（错类型 + 能否重试），不直接喂 stack trace
- [ ] 状态可恢复：checkpoint 持久化 + resume 路径演练过（与 [`./state.md`](./state.md) 对接）

## 相关链接

- [`../agent.md`](../agent.md)：概念层的控制循环定义（Observe → Think → Act → Check → Repeat）
- [`../multi_agent.md`](../multi_agent.md)：4 模式（Chain / Parallel / Cycle / Graph）由控制循环驱动
- [`./sandbox.md`](./sandbox.md)：每一步工具调用都在 sandbox 里执行
- [`./resilience.md`](./resilience.md)：终止条件、retry / backoff、stopping rule 的实现细节
- [`./io.md`](./io.md)：streaming 协议差异与用户中断
- [`../tools.md`](../tools.md)：tool 协议本身（function calling / MCP）
