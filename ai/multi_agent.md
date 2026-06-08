# 多 Agent 编排 (Multi-Agent Orchestration)

> 这篇不重复 [`agent.md`](agent.md) 里"单 agent 闭环"的工程细节；本篇把焦点放在**多个 agent 怎么协作**：编排模式、状态如何共享、任务如何交接、失败如何恢复、业界主流框架的取舍。  
> 单 agent 的感知 / 规划 / 工具 / 评估见 [`agent.md`](agent.md)；Tool Use / MCP 见 [`tools.md`](tools.md)；RAG 见 [`rag.md`](rag.md)；评测见 [`evals.md`](evals.md)。

## TL;DR

- **多 agent 不是"为多而多"**。它的真正价值是**把单 agent 撑不下的能力维度拆开**：上下文窗口、工具数量、专精深度、可调试性；一个单 agent + 工具能解决时，不要拆。
- 主流编排只有 **4 大基本模式**：**Chain（链）/ Parallel（并行）/ Cycle（循环）/ Graph（DAG）**；crew / team / swarm / network 几乎都是这 4 类的嵌套 / 路由 / 组合。先问"任务的图是什么形状"再选框架。
- **State 管理有 3 种思路**：共享全局 state（LangGraph）、消息总线（AutoGen group chat）、显式传递（smolagents / Swarm）。前者易调试、中者贴近人类会议、后者易复用。
- **Agent-as-Tool**（把 agent 当另一个 agent 的"工具"调用）在多数工程场景里比"造超复杂图"更可控——它复用工具的 spec / 错误处理 / 鉴权。
- **协议层正在分层**：**MCP** = agent ↔ 工具 / 资源；**A2A** = agent ↔ agent 互操作；**AG-UI** = agent ↔ 用户应用。三个协议叠加是 2025 之后的事实标准方向。
- **可观测、可恢复、可评估**是工程化硬门槛：trajectory 级别 trace、checkpoint 持久化、trajectory + outcome 双轨评测，缺一不可。

## 1. 为什么需要多 agent

单 agent + 工具在很多场景已经够用；强行拆多 agent 反而带来协调成本与不可观测面。

### 1.1 单 agent 的能力上限

- **上下文窗口**：会话越长，关键事实越易"被淹没"（Lost-in-the-Middle，见 [`prompt_engineering.md`](prompt_engineering.md)）。
- **工具数量**：超过 20-30 个，模型挑错 / 填错概率显著上升。
- **专精度**：同一个 prompt 既写代码又做访谈又写法律意见，模型会"折中"——所有方向都到 60 分。
- **可调试性**：50 步轨迹都属于"一个 agent"时，定位错误的难度和"系统变黑盒"成正比。

### 1.2 多 agent 拆开的真正收益

- **任务分解**：把大任务显式拆给不同专精的 agent，每个的 prompt / 上下文 / 工具集都更小更准。
- **并行加速**：独立子任务并行跑（trpc-agent-go `ParallelAgent`、LangGraph fan-out），把"串行 LLM 调用"压成"并行"。
- **可观测性**：每步归属于哪个 agent、传入了什么 state、为什么失败，比"一锅粥的 LLM 链"好定位得多。
- **可复用性 / 失败隔离**：写一次"代码 review agent"在多个上层工作流里复用；子 agent 抽风不会污染主流程。

> **反直觉点**：拆多 agent **不一定要增加总成本**。每个子 agent 上下文更短、prompt 更专精，单次成本可能更低；总成本由"调用次数 × 单价"决定，拆得好能反而省。

### 1.3 不要拆的场景

任务可以用一个 prompt + 工具集完成；子任务之间强依赖、几乎无分支；团队没有评测体系；可观测 / 可恢复的基建没搭好（没有 trace / checkpoint / replay）。**先单 agent 闭环 + 评测，再谈多 agent**。

## 2. 核心概念

- **Agent**：在环境中闭环运行的决策系统（详见 [`agent.md`](agent.md)）。
- **Role**：agent 在多 agent 系统中的"身份标签"，绑定 instruction / 工具集 / 输出 schema（CrewAI `Agent.role`、MetaGPT 的"PM / 架构师 / 工程师"）。
- **Orchestration**：决定"下一步由哪个 agent 跑、传什么 state、收到结果后做什么"的上层控制逻辑。编排者本身可以是一个 LLM（AutoGen `GroupChatManager`）、一段确定性代码（trpc-agent-go `ChainAgent`）、或一个图（LangGraph / Google ADK Workflow）。
- **Handoff**：把当前任务 / 对话的所有权从一个 agent 转给另一个 agent（Swarm handoff、AutoGen group chat、AG-UI transfer 事件）。
- **State**：跨 agent 之间共享 / 传递的事实——消息历史、计划进度、中间产物、记忆。State 模型是"这个多 agent 系统"最难选的部分。
- **Trajectory**：一次完整执行里所有 agent / 消息 / 工具调用 / 状态变更的有序记录，是 agent 系统专有的评价维度。
- **Checkpoint**：在某一步把 state 持久化，使任务可以从该步恢复（断点续跑 / 长挂起 / 升级框架后回放）。

> **多 agent 系统 = 一组 agent + 一个编排者 + 一份 state + 一组工具 / 资源 + 观测 / 恢复机制**。其中"编排者"和"state"是选型时最该花时间想的两个变量。

## 3. 四大编排基本模式

任何花式架构——crew、team、swarm、graph、network——几乎都能拆成这 4 种基本模式的**嵌套 / 路由 / 组合**。

### 3.1 Chain / Pipeline（链式）

A → B → C → ...；前一步的输出喂给下一步。  
**场景**：需求分析 → 架构设计 → 编码 → 测试 → 部署；查资料 → 总结 → 翻译 → 校对。  
**代表**：trpc-agent-go `ChainAgent`、LangGraph 顺序边、Google ADK 顺序 Workflow、CrewAI `Process.sequential`。  
**优劣**：最易理解、易调试；任何一步失败整条链断；没有"回头改"回路。

### 3.2 Parallel / Fan-out & Fan-in（并行）

调度者把同一份任务复制 N 份派给 N 个 agent，等 N 个都完成后把结果合并。  
**场景**：同一份合同让"法律 / 财务 / 风控"三方同时审；同一段代码用"性能 / 安全 / 可读性"三个 reviewer 并行看；多模型对同一问题取多数票。  
**代表**：trpc-agent-go `ParallelAgent`、LangGraph fan-out、Google ADK fan-out / fan-in、Agno `coordinate`。  
**优劣**：把串行 LLM 调用变成并行，**wall-clock 时间接近最慢那个**；fan-in 易成新瓶颈；并行实例之间**没有共享 state**（否则引入竞态）。

### 3.3 Cycle / Iterative（循环 / 迭代）

planner → executor → 校验 → (未通过则) 回到 planner 修正；直到 stopping rule 满足。  
**场景**：代码生成 + 跑测试 + 改 bug 循环；研究类任务"读 → 总结 → 找空缺 → 再读"；Self-Refine。  
**代表**：trpc-agent-go `CycleAgent`、LangGraph 条件回边、AutoGen nested chat、smolagents `managed_agent`。  
**优劣**：对"输出需要打磨"的任务显著提质；**死循环风险**——必须显式设最大步数与 stopping rule。

```text
[plan] -> [act] -> [check] -pass-> [done]
                       |
                       fail
                       v
                    [replan] -> [plan]
```

### 3.4 Graph / DAG（图 / 有向无环图，必要时加条件回边成状态机）

节点 = agent / 工具 / 子图；边 = 数据 / 控制流；**条件边**根据 state 决定下一步。  
**场景**：需要"分支、汇聚、回退"的复杂任务；长事务（跨分钟 / 小时 / 天）的 agent 流程。  
**代表**：LangGraph、Google ADK Workflow、`trpc-agent-go GraphAgent`（"type-safe graph workflows with multi-conditional routing"）、flowcraft `graph.GraphDefinition`、Pydantic AI `Graph`、Mastra Workflows。  
**优劣**：表达力最强；天然支持 checkpoint / 中断 / 恢复；可视化最好做。学习曲线最陡；图越大 state 污染风险越大。

> **选型小抄**：一条直线 → Chain；可同时做几件独立的事 → Parallel；需要"打磨直到对" → Cycle；有分支 / 聚合 / 回退 → Graph。Graph 几乎能模拟前三种（顺序边 = Chain；扇出扇入 = Parallel；条件回边 = Cycle），但简单任务用专门模式更直观。

## 4. State 管理：3 种主流模型

| 模型                      | 代表                                            | 优势                          | 劣势                                |
| :------------------------ | :---------------------------------------------- | :---------------------------- | :---------------------------------- |
| **共享全局 State**        | LangGraph、flowcraft、trpc-agent-go graph state | 状态集中，最易"暂停-恢复"     | 节点都"看见一切"，state 易污染      |
| **消息总线 / Group chat** | AutoGen GroupChatManager、CrewAI、MetaGPT       | 贴近"人类会议"心智模型        | Manager 易成 LLM 调用热点；归因变难 |
| **显式传递 / Handoff**    | OpenAI Swarm、smolagents `managed_agent`        | 边界最清晰、测试 / 复用最简单 | 链路长时"手动传参"啰嗦              |

> **怎么选**：调试优先 + 单任务长生命周期 → 共享 state；人类会议式协作 + 角色多元 → 消息总线；复用单元 + 强边界 → 显式传递。一个系统可以**在不同层级混用**：顶层 Graph 共享 state，单节点内部显式传递，节点间用消息总线握手。

## 5. Handoff / 委托

Handoff 是"让对话 / 任务从 agent A 转到 agent B"的语义。几种实现：

- **函数返回触发（Swarm 风格）**：`Agent` 的某个 tool 函数返回一个新的 `Agent`，框架把执行权转过去。状态通过 `context_variables` 显式传入。
- **Manager 调度（AutoGen 风格）**：GroupChatManager 是个（通常是 LLM 驱动的）调度者，看完消息后选下一个发言者。
- **路由节点（Graph 风格）**：图里有个"路由"节点，根据 state 选下一步走哪条边；本质是把"handoff 决策"外置为确定性代码或纯 LLM。
- **Agent-as-Tool**：不真正"交接"，而是把"调另一个 agent"当成一次 tool call；调用者得到的是返回值而不是控制权。

> **A2A 协议的设计目标正是 handoff 标准化**——当 agent 跑在不同进程 / 公司 / stack 上时，需要一套公共语言描述"我有什么能力 / 我要你做什么 / 任务生命周期怎么走"。见 §7。

## 6. DAG 执行引擎深入

Graph 是表达力最强的模式，值得单独拆开。

### 6.1 Graph Definition

- **节点（Node）**：执行单元，类型可以是 LLM agent / 纯函数 / 子图 / 远程服务。
- **边（Edge）**：数据 / 控制流；分**静态边**（A→B 必走）和**条件边**（function 看 state 决定下一步）。
- **Fan-out / Fan-in**：一对多 / 多对一边；多用于"并行多 reviewer → 合并"。
- **起点与终点**：特殊节点 `START` / `END`；多入口 / 多出口用于"重试某段"。

> 一个常见错误：图里有"环"但没设条件边——立即死循环。所有环必须配合 stopping rule 与最大步数。

### 6.2 Node Factory

- **静态注册**：图编译时节点 ID 与实现绑定。
- **动态工厂（Factory）**：每次运行前由 factory 函数"造出"对应节点。trpc-agent-go 提供 `RunnerWithAgentFactory`——**按 request 动态构造 agent**（per-user prompt、per-tenant 工具集），避免"一个超大的 agent 处理所有场景"。

```go
r := runner.NewRunnerWithAgentFactory("my-app", "assistant",
    func(ctx context.Context, ro agent.RunOptions) (agent.Agent, error) {
        a := llmagent.New("assistant",
            llmagent.WithInstruction(ro.Instruction),
            llmagent.WithTools(pickToolsFor(ro.UserID)),
        )
        return a, nil
    })
```

> 动态工厂让"一个部署服务支持 N 个业务场景"成为可能，但**不能牺牲可观测**——给每个动态构造的 agent 注入一致的 trace ID 与元数据。

### 6.3 Checkpoint & Resume

> **运行时实现**（存储后端 / 原子写 / 幂等键 / 副作用日志）见 [harness/state.md](harness/state.md)。本节只描述 checkpoint 作为多 agent 拓扑的固有属性。

Checkpoint 让"长事务 agent 流程"成为可能：在拓扑层面，它把执行切成可恢复的"决策点序列"——**Resume** 让失败或中断的 step 单独重跑而无需从头开始；**Wait** 把需要外部输入的节点挂起而不阻塞整张图；**Time-Travel** 允许从任意历史决策点回放，便于离线对比"如果当时走 A 而不是 B 会怎样"。三者共同要求图的边与状态对"挂起 / 恢复"语义友好。

### 6.4 Interrupt / Wait 与 Event Bus

Interrupt 是图执行中的"显式挂起点"，event bus 则是解耦"生产 / 消费"的拓扑手法：每个 step 用结构化 envelope + subject 发出，下游按兴趣订阅，避免图边退化成 N×N 硬连线。

- **Interrupt / Wait 的运行时挂起 / 通知 / 恢复**见 [harness/io.md](harness/io.md)
- **跨进程事件总线**的载体是 [harness/lifecycle.md](harness/lifecycle.md)

## 7. Inter-agent 协议（MCP / A2A / AG-UI 三件套）

2024-2025 出现的"三大协议"正在分层解决多 agent 系统的不同边界问题。**互补**而非竞争。

| 协议      | 解决什么                   | 一句话定位                         |
| :-------- | :------------------------- | :--------------------------------- |
| **MCP**   | agent ↔ 工具 / 资源 / 数据 | "给 agent 接上工具和外部世界"      |
| **A2A**   | agent ↔ agent              | "让 agent 能和别的 agent 平等对话" |
| **AG-UI** | agent ↔ 前端应用 / 用户    | "让 agent 流畅地出现在用户面前"    |

### 7.1 MCP（Model Context Protocol）

"agent ↔ 工具 / 资源 / 数据源" 的协议。详见 [`tools.md`](tools.md) 与 [modelcontextprotocol.io](https://modelcontextprotocol.io/)。  
**类比**：USB-C 端口——把数据源 / 工具 / 工作流以标准方式接进来。解决**单个 agent 的能力扩展**，不解决"两个 agent 怎么对话"。

### 7.2 A2A（Agent-to-Agent）

"agent ↔ agent" 互操作协议，强调"as agents, not just as tools"，由 Google 推动并被多家厂商接纳。关键概念：

- **Agent Card**：描述 agent 能力 / 认证 / 端点的"名片"，用于发现（Agent Discovery）。
- **Task Lifecycle**：一个 task 的状态机（submitted → working → input-required / completed / failed / canceled）。
- **Transport**：JSON-RPC 2.0 over HTTP(S)，支持同步、stream（SSE）、异步 push notification。
- **Opacity**：agent 的内部状态对调用方不可见，只暴露输入 / 输出——让"跨厂商 / 跨语言 / 跨栈"协作成为可能。

### 7.3 AG-UI（Agent-User Interaction Protocol）

"agent ↔ 前端应用" 的事件流协议，补齐"用户在 UI 侧怎么和 agent 实时互动"这一段。关键点：

- 约 **16 种标准事件类型**（`STEP_STARTED` / `TEXT_MESSAGE_CONTENT` / `TOOL_CALL_START` / `STATE_DELTA` / ...），让前端不必适配每个 agent 后端。
- Transport 无关：SSE / WebSocket / Webhook 都能跑。
- 支持**双向 state 同步**、**generative UI**、**human-in-the-loop**、**前端 tool 调用**。
- 与 CopilotKit 深度集成；参考实现支持 LangGraph / CrewAI / Microsoft Agent Framework / Google ADK / AWS Strands / Mastra / Pydantic AI / Agno / LlamaIndex / AG2 等多个框架。

> 一个真实的多 agent 部署往往三者都用：每个 agent 通过 MCP 接工具；agent 之间通过 A2A 协作；用户界面通过 AG-UI 实时显示。

## 8. Agent-as-Tool：把多 agent 简化成"工具嵌套"

> 一个工程上很有用的反直觉：把 agent B **当 tool** 暴露给 agent A，往往比"造一个超复杂图"更可控。

- **形态**：在 agent A 的视角里，B 就是一个普通 tool，spec 是 B 的输入 schema；调用 B 得到 B 的最终输出。
- **代表**：flowcraft `sdk/kanban`（"Multi-agent collaboration without a graph DSL: exposes any agent as a tool to any other agent"）、trpc-agent-go `agenttool` 例子、smolagents `managed_agent`。
- **优势**：复用工具的 spec / 错误处理 / 鉴权 / 限流；边界清晰，B 是 A 的"外部依赖"，可独立升级 / 替换 / 灰度；评测简单。
- **劣势**：A 看不到 B 的"中间过程"，需要 B 自己暴露 trace；串行依赖——B 必须完成才能回到 A，不能像 graph 那样自然并行。

> **经验法则**：能用 Agent-as-Tool 解决的多 agent 协作，不要上 Graph。前者复用现成的工具基建，后者要重建一套编排基建。

## 9. 失败与恢复

多 agent 系统里"哪些地方会挂"远比单 agent 多。可恢复性的设计必须**显式**做。

- **节点级 Retry**：LLM 调用出错时按错误类型选策略——"暂时性（限流 / 超时 / 5xx）"重试，"确定性（参数校验 / 权限 / 内容过滤）" fail-fast；重试时**不要把上一次失败的回答塞回 prompt**（容易形成回音室）。运行时细节（退避公式 / 抖动 / 重试预算）见 [harness/resilience.md](harness/resilience.md)。
- **Checkpoint 持久化**：关键决策点把 state + 已发出副作用落盘，resume 时按幂等键去重——这是"幂等三件套"的核心。运行时细节（存储选型 / 原子写 / 副作用日志）见 [harness/state.md](harness/state.md)。
- **Replan**：某子 agent 持续失败时**不要无限重试同一个计划**——回到上一步、换工具 / 换 agent 或重新分解任务，本质是上层的 cycle 模式。
- **HITL（Human-in-the-Loop）**：触发条件——高风险动作（删数据 / 对外发布 / 付费）、低置信度、敏感领域。HITL 是**第一类公民**而不是"必要时问一下"。运行时实现（UI 通知 / 等待 / 恢复 / 超时降级）见 [harness/io.md](harness/io.md)。

> **HITL 不是"必要时问一下"**——它是**第一类**公民：UI 上要看得见"系统在等什么、卡在哪、要不要批准"，否则用户对系统的信任会迅速崩塌。

## 10. 常见反模式

- **为多而多**：一个 prompt + 5 个 tool 能搞定的任务，拆成 5 个 agent + 1 个 manager；总成本上升、可观测性下降、调试难度 ×5。
- **State 污染**：把"当前用户输入的网页内容"塞进共享 state，下次所有 agent 都看见——典型的提示注入放大版。**外部材料只能当"参考内容"，不能进 state**。
- **Agent 互相等待死锁**：A 等 B 的结果、B 等 C 的结果、C 等 A 的状态确认——经典循环等待。**所有等待都设超时 + 显式死锁检测**。
- **没有 stopping rule 的循环**："自我反思 3 次"如果不写死上限，模型可能无限循环直到成本耗尽。
- **LLM Manager 决策不可重放**：AutoGen 风格的 GroupChatManager 用 LLM 选下一个发言者；同输入两次跑可能选出不同人——轨迹不可重放，bug 难复现。**决策点要可记录、可重放**。
- **隐式依赖全局变量**：agent 通过模块全局变量读"业务配置"；测试时同输入却得不同结果。**配置通过显式 deps 注入**（Pydantic AI `RunContext[SupportDependencies]` 的做法）。
- **可观测性缺失**：没有 trace、没有每步 token / cost 统计、没有失败归因——多 agent 系统出问题几乎必然陷入"玄学调试"。**上线前先解决观测**，细节见 [harness/observability.md](harness/observability.md)。
- **角色名当 prompt 用**：把 "Architect" 当 instruction 写进 prompt，而不写"你负责把需求转成接口定义..."。模型对"角色"的执行靠的是**具体行为描述**而不是 title。

## 11. 评估（trajectory + outcome 双轨）

多 agent 系统的评测比单 agent 难——变量更多、归因更难。**两条线一起跑**，缺一不可。详见 [`evals.md`](evals.md)。

- **Trajectory-level**：正确性（是否走了"该走的路径"）、效率（步数是否合理、有无大量"无效试错"）、合规性（是否触碰禁区、是否触发 HITL 该有的中断、是否注入了不该被信任的外部材料）、可重放性（同输入同 seed 能否复现出同轨迹）。
- **Outcome-level**：任务成功率（在 N 个 case 上完成率）、质量指标（人评或 LLM-as-Judge 评最终输出质量，rubric 化）、副作用正确性（写库数据、邮件内容、有无"计划外"副作用）。
- **资源指标**：Latency（wall-clock p50 / p95 / p99）、Cost（每任务总 token / 美元、哪个 agent 是"成本热点"）、Tool / Retry 次数。

> **评测的归因**：多 agent 失败时，先按 trajectory 找到"哪一步错了"（agent A 的判断 / agent B 的工具调用 / agent C 的格式化），再回到单 agent 层的评测集做局部回归。

## 12. 主流方案横向对比

> 下表只列**与编排 / 多 agent 直接相关**的维度。**State** = 跨 agent 状态管理；**CKPT** = 是否内建 checkpoint。

| 框架 / 协议                 | 核心抽象                                                           | 编排模式                          | State                 | CKPT                  | 典型场景                   |
| :-------------------------- | :----------------------------------------------------------------- | :-------------------------------- | :-------------------- | :-------------------- | :------------------------- |
| **LangGraph**               | `StateGraph` + 节点 + 边 + reducer                                 | Chain / Parallel / Cycle / Graph  | 共享 + reducer        | 内建                  | 复杂长事务 agent           |
| **AutoGen**（v0，维护模式） | `AssistantAgent` + `AgentChat` + Core                              | Group chat / handoff / nested     | 消息总线              | Core API 自行接       | 会议式协作、研究 demo      |
| **MS Agent Framework**      | AutoGen 的"接任者"；AgentChat / Workflow / Core                    | Workflow + AgentChat              | 混合                  | 一等公民              | 企业级生产部署             |
| **CrewAI**                  | `Crew` / `Agent` / `Task` / `Process`（sequential / hierarchical） | 顺序 / 分层委派 / Flow            | Crew 状态 + Memory    | 通过 Flow             | 角色化协作、内容生产       |
| **Google ADK (Python)**     | `Agent`（LLM 驱动）+ `Workflow`（确定性 DAG）                      | routing / fan-out / loops / retry | Workflow + Session    | Session 持久化        | 代码优先的 agent 平台      |
| **OpenAI Swarm**            | `Agent` + handoff                                                  | 显式 handoff                      | context_variables     | 无（client 持有）     | triage 路由；已被 SDK 替代 |
| **Agno**（前 phidata）      | `Agent` 平台 + `Team`（route / coordinate / collaborate）          | 3 种 team 模式                    | Session / Memory      | 支持                  | 平台化 agent 服务          |
| **Pydantic AI**             | `Agent[Deps, Out]`（类型安全）+ `Graph`                            | 顺序 / Graph                      | typed deps + state    | graph + storage       | 严格类型的生产 agent       |
| **Mastra**                  | `Agent` + Workflows（`.then` / `.branch` / `.parallel`）           | LLM agent + 显式 Workflow         | Working + semantic    | 内建 storage          | TS 全栈 agent 平台         |
| **smolagents**              | `CodeAgent` + `ToolCallingAgent` + `managed_agent` 嵌套            | 单 agent + 嵌套                   | agent memory          | 不主打                | 极简、可审计的实验         |
| **trpc-agent-go**           | `LLMAgent` / `Chain` / `Parallel` / `Cycle` / `Graph`              | 4 大 + 动态 factory               | graph.State + Session | graph state 可外接    | Go 后端 agent 平台         |
| **flowcraft**               | `graph.GraphDefinition` + `node.Factory` + subject bus + kanban    | 4 大 + 插件化 engine              | event envelope        | 一等公民（PG+SQLite） | 高可恢复性 agent 服务      |
| **MCP**（协议）             | host / client / server（JSON-RPC + resources / tools / prompts）   | 不管编排                          | 资源 / 提示模板       | 进程级                | agent 工具共享             |
| **A2A**（协议）             | Agent Card / Task lifecycle / JSON-RPC over HTTP+SSE               | 不管编排                          | task 状态机           | task-level 可重入     | 跨厂商 agent 协作          |
| **AG-UI**（协议）           | 约 16 种事件 / 双向 state 同步                                     | 不管编排                          | state delta           | 客户端重放            | 实时 agent UI              |
| **MetaGPT**（研究）         | `Code = SOP(Team)`；PM / 架构师 / 工程师 / QA                      | SOP 流水线                        | artifact 链           | 不主打                | 一行需求 → 完整代码仓库    |
| **ChatDev**（研究）         | 角色 + 阶段（design / code / test / doc）+ chat-chain              | chat-chain / DAG                  | 消息链                | 不主打                | 小型软件项目自动生成       |

**论文 / 灵感来源**（一行版）：LangGraph ← NetworkX 风格图；AutoGen ← arXiv:2308.08155；MetaGPT ← `Code = SOP(Team)`（arXiv:2308.00352）；ChatDev ← chat-chain（arXiv:2307.07924，ACL 2024）；smolagents ← "core code < 1000 LOC"；flowcraft ← 插件化 engine + kanban；MCP ← USB-C 类比；A2A ← "as agents, not just as tools"；AG-UI ← 与 CopilotKit 深度集成。

### 12.1 怎么用这张表选型

- 要"图" + 要"可恢复" + 偏 Python → LangGraph / Google ADK / Pydantic AI Graph。
- 角色化协作 + 不想自己写编排 → CrewAI / Agno。
- TS / Node 全栈 → Mastra。
- Go 后端 + 强可恢复 → flowcraft / trpc-agent-go。
- 极简 + 可审计 → smolagents。
- 跨厂商 / 跨公司 agent 协作 → 上 A2A；想接工具 → 上 MCP；要做 UI → 上 AG-UI。
- 实验 / 研究"软件公司"风格多 agent → MetaGPT / ChatDev（更适合当学习材料而非生产组件）。

> **一个工程判断**：**协议 > 框架**。MCP / A2A / AG-UI 是"行业正在收敛的接口"；具体框架会变（AutoGen 转维护、Swarm 被 Agents SDK 替代都是例子），但协议一旦被广泛接受，迁移成本会高得多。**生产级系统应在"框架"之上优先拥抱"协议"**——比如即使内部用 LangGraph，对外暴露 A2A 端点 / 提供 MCP server / 走 AG-UI 接前端。

## 13. 工程实践：搭一个最小可用的多 agent 系统

按"由小到大"四步走。每一步都先有评测，再加复杂度。

1. **Agent-as-Tool**：写 2-3 个独立 agent（各管一摊事），互不感知；在一个"主管"agent 里把它们当 tool 调，串成 Chain。复用工具基建，边界清晰，评测简单。
2. **加 DAG / 条件路由**：当"主管"里的 Chain 出现明显分支（"如果是 A 路径则调 X；如果是 B 路径则调 Y"），把决策外置为条件边——通常用 LangGraph / flowcraft / trpc-agent-go `GraphAgent`。决策点显式化、trace 可视化、可加 checkpoint。
3. **加 Checkpoint + 副作用幂等**：在每个关键节点落盘 state；所有对外副作用加 idempotency_key。可断点续跑、可时间旅行调试、故障可恢复。
4. **加 HITL + Inter-agent 协议**：高风险动作加 interrupt + 人工审批；对外暴露 A2A 端点；接 MCP server；前端走 AG-UI 实时显示。与企业 / 跨厂商生态对齐。

> **反序提醒**：很多团队一上来就"图 + 协议 + HITL 全配齐"，结果是图还没跑通，先在协议层卡一周。**先闭环最小流程，再加工程化**——这与 [`evals.md`](evals.md) 里 "Eval-Driven Development" 的精神一致。

## 14. 上线前检查清单

- 任务图是 4 种基本模式中的哪一种明确写下，并解释了为什么
- 状态模型已选型，"外部材料不进 state"有显式约束
- 每个对外副作用有 idempotency_key + 副作用日志（["幂等三件套"见 harness/state.md](harness/state.md)）；关键节点落 checkpoint 且 resume 路径已演练
- cycle 模式都有显式最大步数 + stopping rule
- 高风险动作有 HITL 中断 + 合理超时 + 降级路径
- 决策点（路由 / handoff / replan）可记录、可重放
- trajectory + outcome 双轨评测集已建并跑通基线
- cost / latency / token 监控上线，按 agent 维度拆分（细节见 [harness/observability.md](harness/observability.md)）
- 接入协议（MCP / A2A / AG-UI）的范围与版本明确
- "为多而多"自检：每个 agent 都能回答"为什么我不能由另一个 agent 兼任"

## 相关链接

- [`agent.md`](agent.md)：单 agent 闭环、感知 / 规划 / 工具 / 评估的工程抓手
- [`tools.md`](tools.md)：Tool Use / Function Calling / MCP 的细节与工具 spec 设计
- [`rag.md`](rag.md)：检索增强生成（agent 的"知识来源"侧）
- [`prompt.md`](prompt.md) / [`prompt_engineering.md`](prompt_engineering.md)：多 agent 的"指令"和"模式"基础
- [`evals.md`](evals.md)：评测体系；trajectory / outcome / 资源指标的关系
- [`harness/`](harness/)：运行时层（checkpoint 持久化 / retry / HITL 等待 / 进程生命周期的工程实现）

## 参考

- LangGraph（langchain-ai/langgraph）：低层编排框架，stateful agents
- AutoGen（microsoft/autogen）：Wu et al., 2023, arXiv:2308.08155；已进入维护模式，推荐迁到 Microsoft Agent Framework
- CrewAI（crewAIInc/crewAI）/ Google ADK（google/adk-python）/ OpenAI Swarm（openai/swarm，已被 Agents SDK 替代）/ Agno（agno-agi/agno）
- Pydantic AI（pydantic/pydantic-ai）/ Mastra（mastra-ai/mastra，TS 全栈）/ smolagents（huggingface/smolagents，code-as-action）
- trpc-agent-go（trpc-group/trpc-agent-go，Go 生态 Chain/Parallel/Cycle/Graph 四件套）/ flowcraft（gizClaw/flowcraft，可恢复 DAG + kanban）
- A2A（google/A2A，agent ↔ agent 协议）/ AG-UI（ag-ui-protocol/ag-ui，agent ↔ UI 协议）/ MCP（modelcontextprotocol.io，agent ↔ 工具/资源）
- MetaGPT（geekan/MetaGPT）：Hong et al., 2023, arXiv:2308.00352；`Code = SOP(Team)`
- ChatDev（OpenBMB/ChatDev）：Qian et al., 2023, arXiv:2307.07924（ACL 2024）；chat-chain
- DyLAN：Liu et al., 2023, arXiv:2310.02170；动态 LLM agent 网络
- LLM-based Multi-Agents Survey：Guo et al., 2024, arXiv:2402.01680
- Society of Mind：Minsky, 1986（多 agent 思想的早期源头）
