# Harness I/O：流式 / 中断 / HITL / 通道

> 这篇聚焦 **agent harness 的"用户边界"**：从模型吐 token 出来到用户在 UI 上看见、按键、批准、中途改主意这一路。每个 I/O 决策都和"控制循环怎么转、下一步派什么、状态存哪"耦合在一起——单点优化没用，要把它当成运行时一等公民设计。  
> 编排 / State / Checkpoint 见 [`../multi_agent.md`](../multi_agent.md)；单 agent 闭环见 [`../agent.md`](../agent.md)；流式与控制循环的接口见 [`./control_loop.md`](./control_loop.md)；等待时的状态见 [`./state.md`](./state.md)；长时任务进程见 [`./lifecycle.md`](./lifecycle.md)。

## TL;DR

- **I/O 是控制循环的另一半**。流式输出同时承担"UX 反馈"和"中断物理通道"两个角色；没有流就没有可中断性。
- **三种粒度**要分清：token 级（边吐边显）、step 级（每步汇报 + 中间产物）、event 级（AG-UI 16 种标准事件）。三粒度服务于不同通道，不要混用。
- **HITL 是第一类公民**，不是"必要时问一下"。等待原因 / 上下文 / 选项 / 超时要在 UI 上一直可见，否则用户对系统的信任会迅速崩塌。
- **中断不是"kill switch"**——是"把新指令合入当前 turn 的下一段 prompt 边界"。粗暴 abort 会留下半成品副作用和孤儿 token。
- **通道不是装饰**。CLI / IDE 插件 / Web / Slack / IM 承载能力差一个量级；harness 必须**把"能力契约"建在 channel 抽象上**，而不是每个通道各写一套。
- **prompt 里的"高风险必须先确认"是 policy**；harness 负责**把 policy 兑现成 UI 通知 + 等待 + 超时降级**。policy 与运行时解耦，才能改、才能评测、才能审计。

## 1. 这张图里有什么

agent harness 的"用户面"可以拆成 5 块职责：

- **流（Stream）**：模型 / 工具 / 子 agent 的输出以事件形式推到 UI。粒度从 token 到 step 到结构性事件。
- **中断（Interrupt）**：用户任意时刻按键 / 发新消息，harness 不丢失当前 turn 的中间状态，把新指令合入 prompt 的下一个边界。
- **HITL 等待（Wait）**：agent 主动挂起，等用户批准 / 补充信息 / 选择。**等待 ≠ 报错**。
- **进度（Progress）**：长时任务必须有"现在到哪了 / 下一步是什么 / 还需多久"的可见信号。
- **通道适配（Channel）**：CLI / IDE / Web / Slack / IM 承载能力差异巨大；harness 与通道之间是契约，不是一堆 if-else。

> **核心反直觉点**：很多人把 I/O 当"展示层"放在最后做，结果发现 prompt、控制循环、checkpoint 都被 UI 反向绑架。**I/O 模型必须在 harness 设计的第 0 天定下来**，否则后面每加一个通道都要重写一遍。

## 2. 流式输出：三种粒度

### 2.1 token 级流

把 LLM 的 delta（每 N 个 token 一推）直接打到 UI。  
**协议**：SSE（`text/event-stream`）是事实标准；WebSocket 对单向流是过度设计；HTTP/2 stream、gRPC server-streaming 是更"工程化"的替代。  
**价值**：用户看到字一个字跳出来，"系统在工作"的反馈感最强。**也是中断的物理基础**——用户看到 token 才能"在哪儿打断"。

```text
event: token
data: {"delta": "今天"}
event: token
data: {"delta": "我们"}
```

### 2.2 step 级流

每个 LLM step 结束、每次 tool call 开始 / 结束、每个子 agent 交接都打一个事件。粒度比 token 粗，但**信息密度高**——"现在在调哪个工具 / 哪个子 agent 在跑 / 计划到第几步"。

```text
event: step_started   data: {"step_id": "s7", "type": "tool_call", "tool": "search_web"}
event: tool_call_args data: {"step_id": "s7", "args": {"q": "harness io design"}}
event: tool_call_end  data: {"step_id": "s7", "ok": true, "latency_ms": 1240}
event: step_finished  data: {"step_id": "s7", "tokens_in": 1820, "tokens_out": 47}
```

### 2.3 结构化 event 流（AG-UI 16 事件）

把 step / tool / state / message / interrupt 全部规范化。AG-UI（CopilotKit 主导，约 16 种事件）就是这件事的协议形态：`STEP_STARTED` / `TEXT_MESSAGE_CONTENT` / `TOOL_CALL_START` / `STATE_DELTA` / `MESSAGES_SNAPSHOT` / `RUN_FINISHED` / `RUN_ERROR` / ... 详见 [`../multi_agent.md`](../multi_agent.md) §7.3。

> **三粒度不互斥**：token 流给"打字感"，step 流给"进度感"，event 流给"前端可编程的协议面"。生产级 harness 三层都发，前端按通道能力选择渲染策略。

### 2.4 工程要点

- **背压（backpressure）**：客户端渲染慢于推送时，要么丢（体验差）、要么堵（OOM）。正确做法是**双速率适配**——服务端按窗口推，客户端按帧渲染。
- **断点续打**：用户刷新 / 切设备后能否从断点继续？需要 event id + 服务端 replay 缓冲（见 [`./state.md`](./state.md)）。
- **可重放**：把 stream 当 log 持久化，调试时回放任意 step 之后的 UI 状态。
- **取消语义**：客户端断开 ≠ agent 停止；要分清"用户离开了"与"用户取消了"。

## 3. 用户中断：合入下一段 prompt 边界

### 3.1 中断的三种语义

- **Cancel**：用户要停。`context.Cancel` 一路传到工具调用、LLM stream、子 agent；并触发"补偿事务"——回滚已发出的副作用（幂等键 + 副作用日志，见 [`../multi_agent.md`](../multi_agent.md) §9）。
- **Steer / Redirect**：用户要改方向。当前 turn 不中断，但**新指令合入 prompt 的下一段边界**——下一个 LLM step 拿到的 prompt 是"原指令 + 增量指令"或"原指令被替代"。
- **Inject**：用户在当前 step 末尾加一段补充。等价于"先 steer 再继续"。

> **反直觉点**：**没流就没中断**。如果 harness 等 LLM 完全跑完才把结果推给 UI，用户根本没有"打断"的物理基础——按钮按了也无效。token 级流是中断能力的前提，不是装饰。

### 3.2 与现有 prompt 的合并策略

- **Append（追加）**：在 message 列表末尾加一条 user 消息。最温和，但模型可能"既执行原计划又执行新指令"，产出混乱。
- **Replace-last（替代最近）**：把最后一条 user 消息替换。适合"用户改主意"的场景。
- **Prefix-rewriter（前缀重写）**：让一个轻量 LLM 把"新指令 + 原指令"重写成"新指令"或"合并版"。最干净但多一次调用。
- **Plan-revise（计划重写）**：把新指令合入 planner 的 plan（任务清单），下一步从修正后的 plan 继续。最适合"打断后还想继续"的长任务。

> **实战经验**：长任务（>10 step）用 **Plan-revise**；短任务用 **Append** + 模型自己消歧；用户在 token 流里"中途喊停"用 **Steer** + Append。**每种都要有明确事件发出**（AG-UI `RUN_FINISHED` 之后跟一个新 `RUN_STARTED` 是常见模式）。

## 4. HITL：等待一等公民

### 4.1 触发后 UI 必须表达什么

> **HITL 不是"必要时问一下"——它是第一类公民**。UI 上要看得见"系统在等什么、卡在哪、要不要批准"，否则用户对系统的信任会迅速崩塌。

四件套：

- **等待原因（Why）**：把"动作 + 目标 + 风险"用一行话写出来。
- **当前上下文（What）**：批准前能看见"agent 看到了什么 / 计划下一步做什么"。通常是"展开看 diff / 展开看 plan"。
- **可选动作（Choices）**：批准 / 拒绝 / 修改后批准 / 转给同事。不只是 yes/no。
- **超时（When）**：等多久超时？超时后默认什么行为？**必须显式写在 UI 上**，不能藏在 docs 里。

```text
event: input_required
data: {
  "request_id": "r-1234",
  "kind": "approval",
  "summary": "删除 production 库 users 表 12 条记录",
  "diff": "DELETE FROM users WHERE last_login < '2024-01-01'",
  "default_action": "deny",
  "timeout_at": "2026-06-08T15:30:00Z",
  "options": ["approve", "deny", "edit", "delegate"]
}
```

### 4.2 运行时：怎么"挂起 + 恢复"

- **挂起点**：agent 在 tool call 之前生成"待执行动作"对象，调用一个特殊的 `await_approval()` 工具，harness 把当前 turn 的 state 落 checkpoint，发 `INPUT_REQUIRED` 事件，然后**让出控制权**（不是 sleep——是把 goroutine 挂起或任务 requeue）。
- **恢复路径**：用户点"批准" → harness 收到回包 → 从 checkpoint 恢复 → 把"批准"作为工具结果塞回那一步 → 继续。
- **并发用户**：多会话可同时挂在不同 request 上；harness 维护 `(run_id, request_id) -> callback` 表，到点唤醒。

### 4.3 超时与降级

- **硬超时**：到了就 fail-safe，**默认拒绝**高风险动作。**永远不要"超时自动批准"**。
- **软超时**：到点发 UI 提醒 + 默认转给同事 / 落到 inbox 异步批。

降级路径：

| 场景                         | 默认行为 | 升级路径                  |
| :--------------------------- | :------- | :------------------------ |
| 高风险（删 / 发 / 付）       | 超时拒绝 | 落到异步审批队列          |
| 中风险（写文件 / 改 config） | 超时回滚 | 提交 plan 给 owner review |
| 低风险（搜索 / 总结）        | 超时继续 | 任务跑完时附 warning      |
| 纯查询（不写）               | 永不挂起 | —                         |

> **反直觉点**：HITL 的"等待"与控制循环的"cycle"是两种不同的时间。**等待是"任务暂停在已知点"，cycle 是"任务在自驱迭代"**。UI 上要分清两种——前者要倒计时，后者要给阶段汇报。

## 5. 长时任务的进度上报

> 没有可见进度的长时任务 = 用户以为它死了 = 5 分钟后用户 ctrl-c = 不可恢复。

三件套：

- **当前步骤**：planner 的"任务清单"本身就是进度源（"第 7/20 步：调用 search_web(...)"）；不要在 UI 侧反推。
- **总体进度**：百分比 + 已用时 + ETA。**ETA 宁可粗一点不要"线性外推"**——LLM 调用和工具调用的延迟方差极大，用"已完成步数 × 历史平均 step 时长"更准。维护滑动窗口，前 3 步用平均，后续用 EMA，重试剔除离群点。
- **心跳**：每 5-10s 至少发一个 keepalive 事件。

> **反直觉点**：**ETA 不准比"没有 ETA"更糟糕**。给一个会骗人的倒计时，用户宁愿你承认"我不知道还多久，但我还在做"。

进度可重放：进度本身要进 checkpoint（见 [`./state.md`](./state.md)），恢复时无缝接上。通道不支持富文本（Slack / IM）时退化成"5/20 · ~2min left"式短文本——**绝不能没有**。

## 6. 多 I/O 通道

### 6.1 通道能力差异

| 通道                                | token 流             | 富 step 流      | 按钮 / 表单     | 长任务挂起             | 文本编辑回显      | 持续回连       |
| :---------------------------------- | :------------------- | :-------------- | :-------------- | :--------------------- | :---------------- | :------------- |
| **Web UI**                          | 优                   | 优              | 优              | 优                     | 优                | 优             |
| **IDE 插件**（VS Code / JetBrains） | 优                   | 优              | 优              | 优                     | 优（inline diff） | 优             |
| **CLI**（REPL / TUI）               | 优                   | 中（ANSI 表格） | 中（单选）      | 弱（要 detached 进程） | 中                | 弱（本地会话） |
| **Slack / Teams**                   | 中（chunk + typing） | 弱（文本）      | 优（Block Kit） | 优（thread）           | 弱                | 优             |
| **IM**（微信 / 飞书）               | 弱（合并推送）       | 弱              | 中              | 优                     | 弱                | 中             |
| **邮件 / 异步工单**                 | 无                   | 无              | 弱（reply-to）  | 优（小时级）           | 无                | 弱             |

### 6.2 通道契约

把"能力差异"显式建在 harness ↔ channel 的接口上：

- **必须有的能力**：发送 / 接收消息、token 流、step 流、HITL 等待与响应、长时任务挂起。
- **降级策略**：每个能力要么"完全支持"，要么"明确降级到 X"——不允许"假装支持"。
- **状态归属**：harness / server 端是真状态，通道是投影。**通道是 dumb pipe，harness 是 source of truth**。

> **反直觉点**：把 Slack / IM 当"另一个 Web UI 来做"是常见错误——承载模型、交互模型、时延容忍度都不同，**正确的做法是 channel adapter 把"完整 I/O 能力"降级映射到"通道能做什么"**，而不是让 harness 知道 Slack 长什么样。

### 6.3 跨通道一致性

- **同一 run 可被多通道订阅**（用户在 Web 启动，在 Slack 收完成通知）。
- **channel id 与 run id 解耦**：一个 run 在 Web 是 `web#run-123`，在 Slack 是 `slack#C1234-thread`，harness 用统一的 `(tenant, run_id)` 标识。

## 7. 与 prompt 模板 / policy 的关系

[`../agent.md`](../agent.md) 里的"Agent 指令骨架"有这一条：

> 任何高风险操作必须先征得用户确认：删除 / 覆盖文件、执行破坏性命令、发送外部请求、付费 / 转账、对外发布内容。

这是 **policy**——"应该做什么"的描述，不负责执行。执行归 harness，分三步：

1. **检测**：tool call 出来时，harness 拿 policy 比对"这个工具 + 这些参数"是否高风险。
2. **挂起**：命中就转 HITL 等待，发 `INPUT_REQUIRED` 事件。
3. **审计**：批准 / 拒绝 / 超时三类结果都进 log，与 tool call、user、timestamp 绑定——**policy 执行可追溯**。

policy 不该硬编码在 harness 代码里。policy 是 prompt 的一部分（"高风险 = 删 / 改 / 发 / 付"），可以由系统 prompt 注入；harness 只负责"识别 + 等待 + 记录"。policy 改了，harness 不用发版。

> **反直觉点**：很多人把"高风险确认"写死在 tool 的 metadata 里（"这个工具的 `destructive: true`"），结果 policy 改不动、也评不了。**正确的分层：policy 在 prompt / config 里声明，harness 提供 enforce 机制**。

## 8. 常见反模式

- **没有流式，只有 batch 输出**：按钮按了没反应，最后一次性爆出一大段——体验和信任双崩。
- **中断 = kill switch**：用户按 ctrl-c 就 abort，留下半成品副作用（半条邮件、半写文件）且无法恢复。正确做法是"挂起到下一个 step 边界 + 补偿"。
- **HITL 只发个弹窗**：UI 上只有一个 yes/no，看不到"在做什么 / 为什么等 / 什么时候超时"——用户点 yes 是盲点。
- **超时自动批准**：高风险动作没人批就默认通过——把 HITL 当装饰。**永远不要这么做**。
- **ETA 假装精确**："还剩 2 分 13 秒"——LLM 调用延迟方差巨大，假的精确比没有更糟。
- **通道适配 = if-else**：加一个新通道要改 7 个地方。**用 channel adapter 把契约建在接口上**。
- **policy 写在代码里**：改"哪些是高风险"要发版——harness 与 policy 必须解耦。
- **流式丢消息**：客户端断网 / 重连后中间事件丢了，UI 状态对不上。**事件要有 id，服务端要有重放缓冲**。
- **跨通道状态分裂**：harness 是真状态，通道是投影，不能反过来。

## 9. 工程检查清单

- [ ] 流式输出有 token / step / event 三粒度，通道按能力选粒度
- [ ] 中断分 cancel / steer / inject；steer 合入下一段 prompt 边界，不 abort
- [ ] HITL 触发后 UI 表达四件套：等待原因 / 上下文 / 选项 / 超时
- [ ] 超时分硬 / 软；硬超时默认拒绝，软超时转异步
- [ ] 长时任务有 step 名 + 进度 + ETA + heartbeat；ETA 用滑动窗口不线性外推
- [ ] channel adapter 把能力差异建成契约，harness 不感知具体通道
- [ ] policy 在 prompt / config 中声明；harness 只 enforce
- [ ] 高风险操作的"等待 / 批准 / 拒绝 / 超时"全部进 audit log

## 相关链接

- [`../agent.md`](../agent.md)：单 agent 闭环、prompt 模板里的权限 policy
- [`../multi_agent.md`](../multi_agent.md)：HITL 触发条件（HITL 是"哪些动作需要人批"）；AG-UI 协议事件类型（§7.3）
- [`./control_loop.md`](./control_loop.md)：流式输出与控制循环的接口
- [`./state.md`](./state.md)：等待用户时的状态保存 / checkpoint
- [`./lifecycle.md`](./lifecycle.md)：长时任务的进程持有 / 跨进程消息
