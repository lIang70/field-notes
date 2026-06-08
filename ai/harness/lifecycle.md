# Agent 生命周期与 Harness 承载

> 上一篇 [`../multi_agent.md`](../multi_agent.md) 讲"多个 agent 怎么协作";本篇把镜头拉近一层,讲"**单个 agent 怎么活、怎么死、由谁承载**"。这一层常常被当成"基础设施细节"略过,实际是生产环境里**绝大多数稳定性 bug 的源头**。

## TL;DR

- **Harness 决定 agent 怎么活、怎么死**。模型本身是 stateless 的函数,所有"会话、暂停、恢复、终止、清理"都是 harness 在管;不显式建模生命周期,生产环境一定会出资源泄漏与僵尸进程。
- **子 agent 不是"调一下"那么简单**。`spawn` 出去的那一刻,资源边界(进程 / 线程 / 协程)、隔离强度、终止语义就被锁死了;选错了后面再补成本极高。
- **不要让单个 HTTP 请求持有整个 agent 生命周期**。长时任务(分钟 / 小时 / 天)的 session 必须外置到 store(Redis / Postgres / 对象存储),请求只是"触发器 + 收件箱";否则网络抖动 / 反向代理超时 / 客户端断网 = 任务失踪。
- **协议层(MCP / A2A)被 harness 承载,不被 agent 承载**。MCP 的 host/client 是 harness 进程;A2A 的 task state machine 是 harness 在维护;agent 只是"被包在里面的"决策逻辑。
- **Graceful shutdown 是显式工程**。SIGTERM 到达时:跑完当前 turn → checkpoint → 终止子进程 → 释放文件锁 / 临时文件;不写这套,服务每次重启都会留下孤儿与泄漏。

## 1. 单 agent session 的生命周期

> Session ≠ Conversation。Session 是**资源拥有者**(内存、文件、连接、token 配额);Conversation 只是其中的消息流。两者解耦是 harness 设计的第一原则。

一个 session 至少要经过这几个状态:

```text
start ─> [running] ─pause─> [paused] ─resume─> [running]
              │                          │
              │                          └─> [expired] (TTL / 配额)
              │
              ├─> [draining] ─> [checkpointed] ─> [terminated]
              │                  (收到 shutdown, 跑完当前 turn)
              │
              └─> [terminated] ─> cleanup
```

- **start**: 分配 session ID、加载 state、绑定 token 配额、记录 trace ID
- **running**: 进入主循环(Observe → Think → Act → Check,见 [`../agent.md`](../agent.md))
- **paused**: 用户中断 / 等外部事件 / 配额冻结,state 落盘后释放运行时;可被 `resume` 唤醒
- **draining**: 收到 SIGTERM / scale-in 事件,**不接新 turn**,跑完当前 turn 后落 checkpoint
- **terminated**: 显式终止或超时回收,执行 cleanup
- **cleanup**: 释放子进程 / 文件锁 / 临时目录 / 网络连接(见 §7)

> **反直觉点**:很多团队把"paused"和"terminated"混为一谈——一个 session 暂停 30 分钟后再恢复,应不应该保留中间 LLM 的 context、临时文件、token 配额?答案**取决于业务**,但必须显式选;模糊处理会导致"用户重连时 GPU 显存爆"或"恢复后配额已经超"。

## 2. 子 agent 的隔离:进程级 vs 线程级 vs 协程级

> **隔离强度 = 终止难度**。隔离越强越安全,但 spawn / terminate 的成本也越高;生产系统经常需要"**混用**"。

| 隔离粒度 | 资源边界                                | 终止语义                     | 典型场景                           | 失败模式                                |
| :------- | :-------------------------------------- | :--------------------------- | :--------------------------------- | :-------------------------------------- |
| **进程** | 文件系统 / 网络 / 内存 / 文件描述符独立 | SIGTERM + 强 kill(可强制)    | 不信任的子 agent(Code Interpreter) | 僵尸进程、文件锁 / socket 泄漏          |
| **线程** | 共享进程地址空间,独立栈                 | `pthread_cancel`(语义不友好) | 几乎不推荐;LLM 调用本质是 IO       | 共享状态污染;一个崩全崩                 |
| **协程** | 共享堆,独立栈,语言 runtime 调度         | `context.Context` 取消传播   | Agent-as-Tool 的内部子任务         | 协作式取消——子 agent 不响应就永远不退出 |

> **经验法则**:不可信 / 可能执行用户代码 / 需要强沙箱的子 agent(代码执行、浏览器、shell)→ **进程**;同框架内、共享 state、可信的工具型子 agent → **协程 + context 取消**;线程级几乎不该用。

### 进程级 spawn 的标准范式

```python
# Go: 子 agent 进程 + context 取消 + 资源回收
ctx, cancel := context.WithTimeout(parent, 30*time.Minute)
defer cancel()                       // L1: 取消传播
defer os.RemoveAll(tmpDir)           // L2: 临时文件回收

cmd := exec.CommandContext(ctx, "child-agent", "--session", sid)
cmd.Stdout, cmd.Stderr = logWriter, errWriter // 必接,否则 PIPE 满会死锁
if err := cmd.Start(); err != nil { return err }
go func() {                          // L3: wait + 防止僵尸
    _ = cmd.Wait()
    // ... 标记 task 状态
}()

// 收到 SIGTERM 时的处理见 §6
```

`exec.CommandContext` 是 Go 里"取消会自动 SIGTERM 然后 SIGKILL"的便捷封装;`cmd.Wait()` 必须在另一 goroutine 跑,否则 SIGTERM 之后父进程没法 join。

## 3. IPC: harness 怎么承载 MCP 与 A2A

> **协议本身是数据格式;承载协议的是 harness 进程**。MCP 的 host / client 是 harness 起的;A2A 的服务端进程是 harness 跑的;agent 代码只看到 `call_tool(...)` / `send_task(...)` 的客户端 API。

- **MCP(agent ↔ tool / resource)**:harness 启动 MCP client,与 tool server 走 stdio(JSON-RPC)或 HTTP+SSE;`tools/list` / `resources/read` 由 harness 缓存并翻译给模型。详见 [`../tools.md`](../tools.md) 与 [`../multi_agent.md` §7.1](../multi_agent.md)。
- **A2A(agent ↔ agent)**:每个 agent 对外暴露 A2A 端点(也是 harness 起的 HTTP server),用 Agent Card(`/.well-known/agent.json`)做发现,task state machine 做生命周期。详见 [`../multi_agent.md` §7.2](../multi_agent.md)。
- **AG-UI(agent ↔ UI)**:harness 暴露 SSE / WebSocket,前端按约 16 种事件类型订阅;transport 无关。

> **反直觉点**:MCP / A2A 的"服务端"通常**不是 agent 本身**,而是 harness 起的网关进程——它负责鉴权、限流、转换、流式回包。agent 只是被调用时算一下答案。这种"agent + harness 进程"的分层,是把 agent 从"玩具"变成"服务"的关键。

## 4. 长时任务:不要让 HTTP 请求持有 session

> 这是 harness 设计里**最容易踩**的坑:用户开一个 web UI,后端一个 POST `chat(...)` 同步等 2 小时——网络抖一下就 task 失踪。

正确的分层是"**触发器 / session-store / worker**"三段式:

```text
POST /sessions   ─>  API  ─>  store.create()  ─>  enqueue(task_id)
                                                 ─>  202 + session_id

worker[g]        ─>  consume ─> store.load ─> agent.run(ctx, state)
                                ─> checkpoint(每 N 步) ─> store.save

GET /sessions/id ─>  API  ─>  store.read  ─>  SSE / WS stream progress
```

关键点:

- **HTTP 请求寿命 ≤ 几秒**;agent 寿命 ≤ 几小时甚至几天;两者彻底解耦
- **session state 在 store**(Redis / Postgres / 对象存储);agent 是 stateless worker
- **进度 push**(SSE / WS / webhook)而不是 poll
- **取消在 store 标记** + worker 周期性检查 `ctx.Done()`;不是"客户端断网就杀"

详见 [`./io.md`](./io.md)(长时任务的进度与中断)和 [`./state.md`](./state.md)(lifecycle 事件与 checkpoint)。

## 5. 调度器视角:把 agent 装进调度系统

> 调度系统看 agent 跟看 cron job 一样:有入队、有 worker、有并发限制、有 retry、有 dead letter。

- **队列层**:任务进入 Redis / RabbitMQ / Kafka / Postgres `LISTEN/NOTIFY`;按优先级、租户、模型分桶
- **worker pool**:固定 / 弹性 worker 数;每个 worker 起一个 agent 进程(或租一个 sandbox)
- **cron 入口**:周期性任务(每天汇总、每小时监控)走调度器(cron / Temporal / Airflow / Dagster),不直接起 agent
- **限流**:并发上限(防 OOM)+ 租户配额(防单租户打满)+ 模型 TPM(防超 API 配额)
- **dead letter**:连续失败 N 次的任务进 DLQ,人工介入;不要让坏任务"无限重试占着 worker"

> **经验法则**:**agent 不是一个特殊的东西**,它就是 worker pool 里的一个 task type。先把调度器搭好,再让 agent 跑在 worker 上,会比"先写 agent 再补调度"省 10 倍力。

## 6. Graceful Shutdown

> 收到 SIGTERM 时,**绝对不能**直接 `os.Exit()`——那会丢当前 turn 的中间状态、留子进程、留文件锁。K8s 默认给 30 秒 grace period,要充分利用。

```text
SIGTERM 到达
  │
  ▼
1. 标记 draining=true,拒绝新 session,新 turn
  │
  ▼
2. 等待当前 turn 跑完(超时则强制打断并落 checkpoint)
  │
  ▼
3. 遍历所有子进程:
     - 发 SIGTERM(child)
     - 等待 grace(默认 5s)
     - 强 SIGKILL
     - Wait() 回收,防 zombie
  │
  ▼
4. 落最终 checkpoint 到 store
  │
  ▼
5. 释放资源(见 §7):
     - 临时目录、文件锁、网络连接
  │
  ▼
6. 关闭 server: graceful http.Server.Shutdown(ctx)
  │
  ▼
7. os.Exit(0)
```

> **关键反直觉**:`defer` 不会在 `SIGTERM` 时执行——Go 进程默认 `os.Exit()` 不会跑 `defer`。必须**显式**在 signal handler 里走完上面 1-7 步;最常见的 bug 就是"重启服务后磁盘上多了一堆 `.lock` 文件 / 临时目录"。

## 7. 资源回收:临时文件 / 子进程 / 网络连接 / 文件锁

| 资源         | 常见泄漏                     | 正确做法                                                       |
| :----------- | :--------------------------- | :------------------------------------------------------------- |
| **临时文件** | `/tmp` 越攒越多、磁盘满      | `TempDir` + `defer RemoveAll`,或 `mkstemp` 走 `Close+Unlink`   |
| **子进程**   | 父退 child 还在跑(zombie)    | 父必须 `Wait()`;**进程组** + `Setpgid: true` 防止 fork 炸弹    |
| **网络连接** | `keep-alive` 池未关、池污染  | `http.Server.Shutdown(ctx)` 优雅关;idle pool 设上限            |
| **文件锁**   | 进程崩了 `flock` 不释放      | 用 `fcntl(F_OFD_SETLK)`(内核级 close-on-exec)而非进程 advisory |
| **GPU 显存** | 多 session 共用一模型,显存爆 | 按 session 隔离 `CUDA_VISIBLE_DEVICES`;用 vLLM / TGI 池化      |

> **子进程泄漏是 harness 第一杀手**。Go 的 `os/exec` 默认不 `Setpgid`——父崩了子进程会被 init 接管,继续跑、继续占文件、继续扣费。**生产环境的子 agent 必须进程组化 + 双 signal + Wait 回收**。

## 8. 跨进程 agent 协议(A2A)的生命周期

A2A 协议把"跨进程的 agent 任务"标准化成一套状态机。**这套状态机由 harness 维护,不在 agent 代码里**——agent 只暴露"接 task / 返回进度 / 取消"。

```text
[submitted] ─> [working] ─> [completed]
                  │
                  ├─> [input-required] ─> [working]   (HITL)
                  ├─> [auth-required]  ─> [working]   (OAuth)
                  └─> [failed] / [canceled]
```

关键点:

- **Agent Card**(`/.well-known/agent.json`):描述能力 / 端点 / 认证 / streaming——是**发现**机制,不是"我有什么 prompt"
- **Task ID 跨进程**:harness 给每个 task 派全局 ID,worker / 状态机 / UI / 审计日志全靠它对账
- **Streaming(SSE / WS)**:长 turn 进度 push,不是"客户端轮询 task"
- **Opacity**:agent 内部状态对调用方不可见——跨厂商协作的基础

详见 [`../multi_agent.md` §7.2](../multi_agent.md)。

## 9. 反直觉点 / Common pitfalls

> 这一节全是"我见过 / 听过的生产 bug"。

- **子 agent 不是"调一下"那么简单**。spawn 出去就要想清楚:谁拥有它的 state、谁 kill、谁回收文件、谁记日志。**不先想清楚就调,等同于在主流程里开了一扇不可控的后门**。
- **HTTP 长连接 ≠ session 长**。客户端断网 ≠ 任务取消;反向代理 60s 超时 ≠ 服务端停算。**取消语义要在 store 里,不在网络层**。
- **defer 不会在 SIGTERM 时跑**。Go / Python 都一样;必须在 signal handler 里手动走完 shutdown 流程。
- **进程级 child 默认继承父的 fd**——子 agent 能写父进程在用的 socket / 文件。**显式 `Setpgid` + `CloseExtraFiles`**。
- **Checkpoint ≠ log**。log 是事件流;checkpoint 是**可恢复的 state**(已发副作用 + 幂等键)。log 当 checkpoint 用,resume 时副作用会双跑。
- **Grace period 不能省**。K8s 默认 30s,生产调到 60-90s;低于 30s 几乎一定丢 checkpoint。
- **文件锁用 advisory `flock` 进程崩了不释放**——要 `OFD`(open file description)锁,跟着 fd 走,`close` 一定释放。

## 10. 最小可用的 harness 设计清单(MVP)

- [ ] Session state 在外部 store(Redis / Postgres / 对象存储),agent 是 stateless worker
- [ ] HTTP API 只做 trigger + 进度查询,不持有 session 寿命
- [ ] 子 agent 进程组化 + 双 signal + Wait 回收
- [ ] SIGTERM 走完整 shutdown 1-7 步(显式,不靠 defer)
- [ ] 临时文件、文件锁、网络连接有显式 `Close` / `RemoveAll`
- [ ] 关键 turn 落 checkpoint,resume 路径演练过
- [ ] 调度器有并发上限、租户配额、dead letter
- [ ] A2A / MCP 端点由 harness 暴露,agent 不直接监听网络

## 相关链接

- [`../multi_agent.md`](../multi_agent.md):协议层(MCP / A2A / AG-UI);4 大编排模式;Agent-as-Tool
- [`../tools.md`](../tools.md):MCP 与 tool / resource 协议的细节
- [`./sandbox.md`](./sandbox.md):进程级隔离的资源限制(cgroup / seccomp / namespace)
- [`./state.md`](./state.md):lifecycle 事件与 checkpoint 的关系,state 落盘与回放
- [`./io.md`](./io.md):长时任务的进度推送与中断恢复
