# 状态管理与 Checkpoint（State & Checkpoint）

> 本篇是 [`../agent.md`](../agent.md)（agent 闭环）和 [`../multi_agent.md`](../multi_agent.md)（多 agent 拓扑）之外的**第三维**：让 agent 真的"不丢活"——进程挂了能 resume、副作用不会重发、轨迹可回放。  
> 多 agent 状态管理的**拓扑**层面（共享 / 总线 / 显式传递）见 [`../multi_agent.md`](../multi_agent.md) §4；checkpoint 作为**拓扑属性**的工程含义见该篇 §6.3。Memory 系统（mem0 / Letta / LangMem）见 [`../memory.md`](../memory.md)；进程崩溃后由谁负责 resume 见 [`./lifecycle.md`](lifecycle.md)。

## TL;DR

- **State ≠ log**。Checkpoint 是**强一致 + 原子写**的 snapshot；把"加个 log"当 checkpoint 用，resume 一定重复执行已成功的副作用。
- **最小不变量是"三者一致"**：**state 快照 + 已发副作用日志 + trace**，三者必须写到同一时刻，否则 resume 时要么重做已做的事，要么丢失正在做的事。
- **副作用幂等三件套**：**idempotency_key**（每次副作用带唯一键） + **副作用日志**（先写日志再发请求，outbox 模式） + **重做策略**（resume 时按日志去重）。少任何一件，resume 就会变成"双扣款"。
- **落盘频率是工程取舍，不是越高越好**：每步都落 → 慢、噪声大、可能写出不完整状态；只在关键决策点落 → 故障时丢失中间步骤。配合 outbox + 幂等键，**频率可降到"决策点级别"**而不丢一致性。
- **存储选型按量级**（不是按"哪个最酷"）：单进程 demo → SQLite；低延迟热路径 → Redis；关系查询 / 跨服务 → Postgres；超大 artifact / 长期归档 → 对象存储。flowcraft 同时提供 Postgres + SQLite 两种 store 是典型做法。
- **状态机序列化 ≠ 状态序列化**：把"task 跑到第几步"用 FSM 显式表示，比"agent 内部对象图 JSON 化"好——可重放、可分支、可合并、可人工审计。

## 1) Session 内 vs 跨 Session 状态

State 的边界**就是进程边界 + 显式终止信号**。判断一个东西"该不该落盘"的简单准则：

> **进程挂了以后，这个东西还得继续对吗？** 是 → 落盘。否 → 留在内存里。

按"该不该落盘"分四层：

- **In-flight（不落）**：当前 LLM 调用的 prompt / streaming chunk / tool call 中间产物——**活的 step 不要序列化**。
- **Session 内（resume 要）**：conversation、scratchpad（计划 / 已读文件 / 中间结论）。Scratchpad 看起来"在内存里就行"，但 resume 后 agent 必须**在不知道之前想什么的状态下重启**——这等于让模型从零开始重新推理一遍。**没有 scratchpad 的 checkpoint 是废的**。
- **一次 run + 之后（强一致）**：trajectory（每步哪个 tool / 什么参数 / 什么结果 / 多少 token）、side-effect log（已发请求的 idempotency_key + 结果）。
- **跨 session / 跨 user**：task plan（FSM 节点 / 子任务完成度）、long-term memory（见 [`../memory.md`](../memory.md)）。

## 2) Checkpoint 策略

### 2.1 触发频率

- **每步落**：最安全、最慢、噪声最大；除非你要做"步步可重放"的研究系统，否则不推荐。
- **关键决策点落**（推荐默认）：进入新 FSM 节点、跨工具调用、跨网络副作用前、可逆性边界（创建资源前）——见 [`./control_loop.md`](control_loop.md) 何时触发落盘。
- **定时器落**：后台 agent、长挂起任务、跨分钟 / 小时的流程——每隔 N 秒 / N 步兜底一次。
- **混合**：决策点为主 + 定时器兜底 + 每步只写 trace 不写 snapshot。

> **反直觉点**：落盘频率**不是**"越高越安全"。**太频繁的 checkpoint 反而引入不一致**：如果 step 5 的 snapshot 写到了 step 4 之后、step 6 副作用发完之后，resume 时程序以为自己在 step 5，实际环境里 step 6 的副作用已经发生——典型的"快照-现实割裂"。

### 2.2 落盘位置（按量级）

- **单进程 demo / 本地 agent** → **SQLite**：零运维、文件即库、fsync 即一致。
- **低延迟热路径 / 大量小 checkpoint** → **Redis**：sub-ms 写、可设 TTL；缺点是容量小、贵、不便复杂查询。
- **跨服务 / 关系查询 / 长期任务** → **Postgres**：事务、回放、跨表 JOIN、replication；主流生产选择（LangGraph、flowcraft、AG-UI 后端）。
- **超大 artifact（截图、生成的 PDF）** → **对象存储（S3 / GCS）**：便宜、版本化；metadata 仍写 Postgres / SQLite。
- **混合**：PG / SQLite 做 state + S3 做 blob——小状态在 DB、大对象在对象存储、引用以 URL 形式挂在 state 里。

### 2.3 原子写模式

"原子写"不是"尽力写"——它是 checkpoint 工程上**最容易翻车**的点。

- **写时复制（Copy-on-Write / WAL）**：先写 `checkpoint-xxx.tmp` → `fsync` → 原子 rename 成 `checkpoint-xxx`；崩溃后最多丢失"正在写的那一个"，不会写出"半截 JSON"。
- **Outbox 模式**：副作用日志先写本地 outbox 表（与 state 同库同事务）→ 异步把 outbox flush 到目标服务；resume 时按 outbox 决定"该发还是已发"。
- **不要做**："先发请求，再写 state"——发出去挂了就再也对不上账。**先写日志、再发副作用**是硬规则。

```sql
-- 副作用 outbox（与 state 同一事务，强一致）
CREATE TABLE side_effects (
  idempotency_key TEXT PRIMARY KEY,
  step_id         TEXT NOT NULL,
  kind            TEXT NOT NULL,   -- http / file / db
  target          TEXT NOT NULL,   -- URL / path / table
  request         JSONB NOT NULL,
  result          JSONB,
  state           TEXT NOT NULL,   -- pending / sent / failed
  created_at      TIMESTAMPTZ DEFAULT now()
);

-- 状态机 snapshot
CREATE TABLE checkpoints (
  step_id         TEXT PRIMARY KEY,
  fsm_state       TEXT NOT NULL,
  state_payload   JSONB NOT NULL,
  trace_id        TEXT NOT NULL,
  created_at      TIMESTAMPTZ DEFAULT now()
);
```

## 3) Resume-after-crash：最小不变量

进程崩溃 / OOM / 升级 / 部署时，怎么从最后一个安全状态继续：

> **最小不变量**：**state 快照 + 已发副作用日志 + trace ID** 三者**写到同一时刻**。Resume 时按 step_id 同时加载三份，缺一不可。

具体动作：

1. **找到最近一个完整的 checkpoint**（fsync 成功的、FK 完整的；不要相信写一半的 .tmp）。
2. **重放 trace 中"pending"的副作用**（按 idempotency_key 去重；已 sent 的跳过，未 sent 的按 outbox 重发）。
3. **重建 LLM 上下文**（conversation + scratchpad + tool result 历史），**不要**让模型"重新推理"——它应该接上"上一个 checkpoint 时的状态"继续。
4. **回到 FSM 的对应节点**，按当前 state 的"该做什么"决定下一步。

> **反直觉点**：很多 harness 失败时让 agent "从头思考"——这等于丢弃了所有中间推理，**等价于把整个 task 重新跑一遍**，成本是 O(N) 而不是 O(剩余步骤)。正确的 resume 是 O(剩余) 起步：第 47 步挂了，resume 后从第 47 步继续，而不是从第 1 步。

崩溃后**由谁负责触发 resume**——子进程看门狗、scheduler、k8s liveness、还是用户手动——是 [`./lifecycle.md`](lifecycle.md) 的事，不是 state.md 的事。

## 4) 副作用幂等：幂等三件套

任何"对外发出的动作"（HTTP / gRPC / 写文件 / 改 DB / 发消息）都必须满足：

1. **idempotency_key**：每次副作用带一个**全局唯一**的 key（一般是 `run_id + step_id + action_seq` 的组合 hash）。
2. **副作用日志（outbox）**：**先写日志，再发动作**。日志与 state 在同事务中落盘，确保"是否发过"与"现在在第几步"原子一致。
3. **重做策略**：resume 时按 key 去重——**目标服务支持幂等就直接重发**（带同样的 key），**不支持幂等就跳过 + 记录**（"已发过，结果在日志里"）。

```python
def execute_side_effect(step_id, action):
    key = f"{run_id}:{step_id}:{seq}"
    with db.transaction():
        # 1. 先写 outbox，原子登记
        existing = db.fetch_one(
            "SELECT result, state FROM side_effects WHERE idempotency_key=%s",
            (key,),
        )
        if existing and existing.state == "sent":
            return existing.result  # 2. 已发过，直接返回
        db.execute(
            "INSERT INTO side_effects (idempotency_key, step_id, request, state) "
            "VALUES (%s, %s, %s, 'pending') ON CONFLICT DO NOTHING",
            (key, step_id, action.to_json()),
        )
    # 3. 真正发动作
    result = action.send()
    # 4. 异步把 outbox 标为 sent
    db.execute(
        "UPDATE side_effects SET state='sent', result=%s WHERE idempotency_key=%s",
        (result.to_json(), key),
    )
    return result
```

> **反直觉点**：很多团队"先发请求，成功后写日志"——失败在两者之间 = **重做时再发一次 = 双扣款 / 重复邮件 / 重复下单**。正确顺序是 **outbox 先写 + 事务提交 → 发动作 → 标记 sent**，三段式。这与 [Pat Helland 的 outbox pattern](https://microservices.io/patterns/data/transactional-outbox.html) 同源。

## 5) 时间旅行调试（Time-Travel Debug）

回放任意历史 checkpoint，"如果当时走 A 而不是 B 会怎样"。

- **基础**：checkpoint 完整（state + trace + 副作用日志），且 **LLM 调用带 deterministic seed + temperature=0**（或记录完整 response）。
- **做法**：从目标 checkpoint 恢复 state → **跳过"已发副作用"那段** → 让 LLM 重新决策"如果选 B 而不是 A" → 继续走 → 产出"平行宇宙"的轨迹。
- **边界**：LLM 本身可能有非确定性（推理引擎版本差异、浮点累计），完全可重放需要**记录 response** 而不是"再调一次"。
- **价值**：bug 复现、决策归因（"为什么模型选 A 不选 B"）、反事实分析（"如果限速再低 10% 会不会走通"）。

> **反直觉点**：时间旅行调试**不是"重跑一遍 task"**——它是"从某个决策点分叉，重跑**之后**的步骤"。前者慢、贵、改不到根因；后者快、精确、能定位"哪一步的决策错了"。

## 6) 状态机持久化（FSM Snapshot）

长事务 agent 流程（多步 / 跨分钟 / 跨 session）应该把"task 跑到哪一步"用**显式 FSM** 表示，而不是用 Python 对象图 + 隐式控制流。

```text
[task: refund_request]
  START
    -> [validate_order]    (call: orders.get)
  validate_order
    -> [check_policy]      (call: policies.check)
  check_policy
    -> [refund_decision]   (LLM call: yes/no)
  refund_decision
    -(yes)-> [process_refund] (idempotency_key=refund_xxx)
    -(no)->  [send_decline_email] (idempotency_key=email_xxx)
  process_refund / send_decline_email
    -> [END]
```

**序列化的关键字段**：

- `fsm_state`：当前节点 ID
- `state_payload`：进入该节点时需要的事实（订单 ID、用户输入、refund 金额）
- `next_actions`：候选转移（由 FSM 静态决定，不在 LLM 里）
- `pending_side_effects`：尚未 ack 的副作用 outbox

**优势**：可重放（按 node ID 重启）、可分支（同一节点做不同决策）、可合并（多入口或多出口）、可审计（人看 FSM 就能明白进度）、可迁移（snapshot 跨服务 / 跨版本）。

## 7) 单 agent 私有 vs 多 agent 共享 State

- **存哪**：单 agent → 本地变量 / 本地 SQLite / 同进程文件；多 agent → 共享 store（Postgres / Redis） / 消息总线 / 显式参数。
- **一致性**：单 agent 强（无竞争）；多 agent 弱（需事务 / 锁 / reducer / version vector）。
- **大小**：单 agent 可大（一个 agent 装得下）；多 agent 要克制（state 越共享越容易污染）。
- **可观测**：单 agent 一条轨迹即可；多 agent 需按 agent 维度拆分 trace，否则归因变难。
- **典型反模式**：单 agent 把"对话历史"当 state（污染）；多 agent 把"当前网页内容"塞进共享 state（注入放大版）。

> **反直觉点**：**多 agent 共享 state 看起来"方便"，实际最容易污染**——所有 agent 看见所有内容 = 提示注入风险 ×N。**外部材料只能进消息流 / context，不能进 shared state**（见 [`../multi_agent.md`](../multi_agent.md) §10）。  
> 多 agent 状态管理**模型**（共享 / 总线 / 显式传递）见 [`../multi_agent.md`](../multi_agent.md) §4，本节只讨论**存储差异**。

## 8) 反直觉点（小结）

把全文的反直觉点压成七条，方便日后回查：

1. **"加个 log" ≠ checkpoint**：log 是 best-effort，checkpoint 必须是强一致原子写。
2. **三者一致**（state + 副作用 + trace）缺一不可，缺哪个就出对应类型的 bug。
3. **先写 outbox，再发动作**——失败在中间 = 双扣款。
4. **不要每步都落**——决策点 + 定时器兜底，配合幂等三件套就够了。
5. **Resume 不是重跑**，是"从最近 checkpoint 继续"——否则成本 O(N) 而非 O(剩余)。
6. **状态机序列化 > 对象图 JSON 化**：FSM 可重放、可分支、可审计。
7. **多 agent 共享 state 越大越容易污染**：外部材料**不**进 shared state。

## 9) 最小可用清单（上线前自检）

- [ ] Session 内 vs 跨 session 状态已显式分层
- [ ] Checkpoint 频率 = 关键决策点 + 定时器兜底（不是每步）
- [ ] 落盘是原子写（WAL / 写时复制 / outbox），不是"加个 log"
- [ ] 每个对外副作用带 idempotency_key + outbox 强一致写
- [ ] Resume 路径演练过：杀进程后能从最近 checkpoint 继续，副作用不重发
- [ ] State 序列化是显式 FSM（不是对象图 JSON 化）
- [ ] Trace 与 checkpoint 同 step_id 关联
- [ ] 多 agent 共享 state 有 schema + 写入白名单 + 外部材料隔离
- [ ] 时间旅行的"平行宇宙回放"在测试集上能跑通

## 相关链接

- [`../multi_agent.md`](../multi_agent.md) — 多 agent 状态管理的拓扑层（共享 / 总线 / 显式传递 3 种模型；checkpoint 作为拓扑属性的工程含义见 §6.3）
- [`../memory.md`](../memory.md) — 长期 memory（mem0 / Letta / LangMem 等）与 checkpoint 的边界：memory 是"知识累积"，checkpoint 是"运行状态"
- [`./control_loop.md`](control_loop.md) — 何时触发落盘（关键决策点判定、定时器间隔、步间契约）
- [`./resilience.md`](resilience.md) — 重试时 state 回滚（错误分类、replan 与 checkpoint 的关系）
- [`./lifecycle.md`](lifecycle.md) — 进程崩溃后由谁负责 resume（看门狗、scheduler、cancel 传播）
- [`../agent.md`](../agent.md) — 单 agent 闭环，state 是其"世界状态"子模块
- [`../evals.md`](../evals.md) — checkpoint / resume 路径本身要进回归集（"杀进程后能恢复"是可测的）
