# 调度器对比 (Scheduler Comparison)

这一篇**不深入任何单一调度器**，而是把不同生态里常见的调度器放在同一张表上对比：调度什么、怎么抢、怎么权衡吞吐与延迟。  
Linux CFS 内部细节看 [`process.md`](process.md) 的 8.2 节；Go GMP 内部细节看 [`go/runtime/scheduler.md`](../go/runtime/scheduler.md)；线程/协程模型看 [`concurrency.md`](concurrency.md) 的第 4 节。

## TL;DR

- **调度对象的差异**远比“算法差异”重要：调度 OS 线程、用户态协程、轻量纤程，对应的“上下文”成本和能利用的并行度完全不同。
- **抢占性**决定了“单个跑题任务能不能拖垮整体”：内核态抢占是硬实时/低延迟的基础，但代价是切换开销与缓存破坏。
- **优先级/权重**是“软公平”机制；**实时性**是“硬截止时间”机制——两者通常**不能同框**。
- 现代调度器在“公平 / 实时 / 节能 / 拓扑感知 / 容器配额”这几个目标间做权衡：每个调度器默认偏向不同。
- Go GMP 与 tokio runtime 不是“OS 调度器”，但它们在**应用层重新发明了调度**，理解 OS 调度器能直接帮上选型与调优。

## 1. 调度对象：先决定在调度什么

> 调度器的“单位”决定了它所有的设计取舍：切换成本、可达数量、隔离方式。

| 调度对象                                     | 典型栈大小    | 切换成本                  | 数量级      | 例子                       |
| :------------------------------------------- | :------------ | :------------------------ | :---------- | :------------------------- |
| **OS 线程**                                  | 1~8 MB        | 一次系统调用 + 上下文切换 | 数百~数千   | Linux task、Windows thread |
| **Goroutine**                                | 2 KB 起，动态 | 用户态切换，runtime 调度  | 数十万~百万 | Go GMP                     |
| **Tokio task**                               | 几乎为 0      | 用户态调度                | 数十万~百万 | Rust tokio runtime         |
| **UMS fiber (Windows)**                      | 12 KB 起      | 用户态切换                | 数千~数万   | Windows UMS                |
| **Linux 协程 (io_uring task / userfaultfd)** | 自管          | 用户态                    | 视实现      | io_uring / 自实现          |

> 真正选型时，先选“**调度对象**”，再选“**调度器**”：协程框架会限制你能用的系统调用、阻塞行为、栈深度。

## 2. 五个核心调度器速览

### 2.1 Linux CFS (Completely Fair Scheduler)

- **调度对象**：内核 task（线程/进程，统一 `task_struct`）。
- **核心机制**：
  - 每 CPU runqueue + **红黑树**，按 `vruntime` 排序。
  - 每次挑 `vruntime` 最小的 task；运行后按 `nice/weight` 折算更新其 `vruntime`。
- **优先级/权重**：`nice` 值通过权重表换算；权重越高，`vruntime` 增长越慢 → 实际占用 CPU 比例越大。
- **组调度 (cgroup)**：在 cgroup 视角里，“整组”当作一个 task 参与公平；这与容器 CPU 配额直接对接。
- **实时性**：CFS 不提供硬实时；实时任务由 `SCHED_FIFO / SCHED_RR / SCHED_DEADLINE` 单独调度（脱离 CFS 路径）。
- **亲和性 / NUMA**：`sched_setaffinity` + `numactl`；CFS 也会在 idle 平衡中考虑 NUMA 距离。

> 设计目标：**通用系统的“公平”**——对长任务、短任务、I/O 密集、CPU 密集都尽量过得去；不偏袒任何一种负载。

### 2.2 Windows UMS (User-Mode Scheduling)

- **调度对象**：**UMS fiber**（应用创建的纤程）。
- **设计动机**：
  - Windows 的内核态上下文切换相对昂贵。
  - 把“调度”从内核拿出来，**让应用程序自己决定**什么时候切到哪个 fiber。
  - 与 UMS 配套的还有 **Fibers**（更老的、纯用户态纤程，没有 UMS 那种“主动让出给 OS 线程”的机制）。
- **与 Fibers 的差异**：
  - **Fibers**：纯用户态，由程序自己 `SwitchToFiber`；如果 fiber 阻塞在系统调用上，整个 OS 线程都阻塞。
  - **UMS**：允许 fiber **陷入内核**后，把 OS 线程还回调度器（去执行别的 fiber），等阻塞完成再由内核补回上下文；既能用户态快速切换，又不会被一个 fiber 的同步调用“焊死”线程。
- **抢占性**：UMS fiber 是**协作**的（程序主动让出）；OS 线程层面是抢占。
- **典型场景**：早期 SQL Server、IIS 那种“成千上万工作项但 OS 线程不能开太多”的场景。

> 设计目标：**让应用层拿到调度权**，同时利用 OS 线程池处理阻塞调用。

### 2.3 FreeBSD ULE

- **调度对象**：内核 thread（与 Linux 类似）。
- **核心机制**：
  - 维护 **runqueue** 与 **idle queue**。
  - **交互式负载优先**：唤醒任务会得到“交互性加成”，更快被选中。
  - **CPU 拓扑感知**：在多核/NUMA 上做“就近”选择。
  - **时间片动态**：交互任务短片、CPU 任务长片（与 CFS 的 vruntime 思路不同）。
- **与 CFS 的差异直觉**：
  - CFS 用“全局 vruntime”近似公平；ULE 用“交互性”显式判断。
  - CFS 调度器迁移更激进；ULE 更注重 cache 局部性。
- **实时性**：`RT priority`（`pri`）支持硬实时；与 CFS 类似，硬实时脱离通用路径。

> 设计目标：**桌面/交互式负载优先**（FreeBSD 的传统强项），同时不放弃多核扩展。

### 2.4 Go GMP

> GMP 详细内部细节看 [`go/runtime/scheduler.md`](../go/runtime/scheduler.md)，这里只从“调度器对比”视角看取舍。

- **调度对象**：**goroutine (G)**，初始栈 2 KB，按需扩缩。
- **核心机制**：
  - **G** (goroutine) — 用户级执行单元。
  - **M** (machine) — OS 线程。
  - **P** (processor) — 调度上下文，逻辑 CPU，持有 local run queue (LRQ)。
  - **Work stealing**：P 之间相互偷取 LRQ 的一半；全局队列饥饿时优先注入。
- **抢占性**：
  - Go 1.14 之前：协作式（函数边界检查）。
  - Go 1.14+：`sysmon` 用 `SIGURG` 抢占真·长循环的 G。
- **阻塞系统调用**：M 阻塞时释放 P，P 找新 M，**不让阻塞点拖垮其他 G**。
- **与 OS 调度的关系**：
  - 一句直白：**Go runtime 是个用户态 M:N 调度器**，M 是 OS 线程，G 是协程。
  - 它不替代 OS 调度器，但**对 OS 调度器是“友好”**（通常把 `GOMAXPROCS` 设为可见核数，避免与 OS 抢调度）。
- **亲和性 / NUMA**：runtime 不直接感知 NUMA；可以靠 `GOMAXPROCS` + 容器绑核间接控制。

> 设计目标：**海量 goroutine 上的低延迟调度**——把 OS 线程当“CPU”使用，goroutine 当“程序”使用。

### 2.5 Rust tokio runtime

> tokio 不是语言，是**一个用户态调度器/运行时**。它面向 async/await 的 task 调度。

- **调度对象**：**async task**（`Future` 驱动的状态机），初始栈几乎为 0（task 是堆上对象）。
- **两种 runtime**：
  - **单线程 (`current_thread`)**：所有 task 在一个 OS 线程上跑；调度器是个 work-stealing + 单线程队列。
    - 优点：极低开销、cache 友好、**无锁**。
    - 缺点：不能用多核；任何一个阻塞操作都会卡住所有 task。
  - **多线程 (`multi_thread`)**：每 worker 线程一个 local queue + 全局队列 + work-stealing；**与 Go GMP 思路高度类似**。
- **阻塞检测**：`spawn_blocking` 把“会阻塞”的任务扔到独立线程池，**保护 worker 线程不被拖垮**。
- **与 OS 调度的关系**：tokio 默认**完全协作**——`.await` 点就是让出点，**不抢占**。
- **与 async-std / smol 对比**：同为 Rust 用户态调度器；tokio 多线程版是最像 Go GMP 的。

> 设计目标：**在少量 OS 线程上跑大量 I/O 密集型 task**，把每个 await 都当作“可能切走”的机会。

## 3. 调度器对比维度（核心对比表）

> 这一节是“轴”——把不同调度器按同一组维度摆出来，方便按需取用。

| 维度          | Linux CFS                                     | Windows UMS           | FreeBSD ULE           | Go GMP                          | tokio multi-thread      |
| :------------ | :-------------------------------------------- | :-------------------- | :-------------------- | :------------------------------ | :---------------------- |
| 调度对象      | 内核 task                                     | UMS fiber             | 内核 thread           | goroutine                       | async task              |
| 调度位置      | 内核态                                        | 用户态 (调度权在 app) | 内核态                | 用户态 (runtime)                | 用户态 (runtime)        |
| 调度模型      | 抢占                                          | 协作 (fiber 层)       | 抢占                  | 抢占 (1.14+ 信号)               | 协作 (`.await` 让出)    |
| 优先级/权重   | nice + 权重表                                 | 无 (应用自定义)       | `pri` + 交互性加成    | 无全局优先级 (优先级队列在内部) | 无 (调度器视角)         |
| 公平性        | CFS 名称由来：完全公平                        | 由应用决定            | 偏向交互 + cache 局部 | 抢占 + work stealing            | 抢占 + work stealing    |
| 组调度 / 配额 | cgroup v1/v2 (CPU、memory、pids)              | 进程/JOB 对象         | jail + cpuset         | 无 (GOMAXPROCS 总数)            | 无                      |
| 亲和性 / NUMA | 完整支持 (sched_setaffinity / NUMA balancing) | 由 app + 线程池       | 显式 NUMA 感知        | 弱 (需手动控制)                 | 弱 (需手动控制)         |
| 实时性        | `SCHED_FIFO/RR/DEADLINE` (旁路 CFS)           | 不主打实时            | `RT priority`         | 不主打实时                      | 不主打实时              |
| 阻塞处理      | 内核态阻塞可睡眠                              | 主动让出 OS 线程      | 内核态阻塞可睡眠      | P 转让给其他 M                  | `spawn_blocking` 线程池 |
| 切换成本      | 高 (内核态切换)                               | 低 (用户态)           | 高                    | 极低 (用户态)                   | 极低 (用户态)           |
| 典型量级      | 数千~万级 task                                | 数千~万级 fiber       | 数千~万级 thread      | 数十万~百万级 G                 | 数十万~百万级 task      |

### 3.1 几个“反直觉”的轴

- **抢占性不是越多越好**：抢占带来切换开销与 cache 破坏；纯 I/O 密集型应用上，抢占反而降低吞吐。
- **优先级与公平性经常冲突**：CFS 用 nice 表达权重但保持 vruntime 公平，ULE 用交互性表达偏好；Go/tokio 干脆放弃应用层可见的“优先级”。
- **组调度是“配额”不是“公平”**：cgroup 的 CPU 份额是 cgroup 间的公平，与 CFS 在 task 间做的公平是两层叠加。
- **实时 ≠ 公平**：硬实时调度（`SCHED_DEADLINE`、`pri` RT）一般不参与通用公平路径，写错了会**整系统卡死**。
- **用户态调度器不会让 OS 调度器消失**：M:N 模型里 M 仍然是 OS 线程，被 OS 调度；**别把“用户态调度器”当万能解**。

## 4. 设计目标与典型适用场景

> 这一节把“调度器的偏向”直接翻译成“什么时候用它”。

| 调度器      | 偏向                | 典型场景                         | 不适合                          |
| :---------- | :------------------ | :------------------------------- | :------------------------------ |
| Linux CFS   | 通用公平、容器友好  | 服务器、容器、桌面               | 硬实时                          |
| Windows UMS | 应用自定义调度      | 高并发数据库服务器、IIS          | 需要 fork-style 多进程隔离      |
| FreeBSD ULE | 交互式 + cache 友好 | 桌面、网关                       | 极端 NUMA 服务器 (历史偏见)     |
| Go GMP      | 海量 G + 低延迟     | 微服务、网络服务、CNCF 工具      | 长时间 CPU bound (C 扩展更划算) |
| tokio       | I/O 密集型 + 可预测 | 反向代理 (linkerd/tonic/reqwest) | CPU bound 业务逻辑 (用 rayon)   |

## 5. 选择调度器时的几个工程抓手

- **任务类型**：CPU 密集（少而长）vs I/O 密集（多而短）。前者选 1:1 线程模型更省心；后者选用户态调度器更划算。
- **延迟量级**：p99 < 1ms 级别要避开 OS 线程切换；考虑 io_uring / tokio / Go。
- **NUMA 拓扑**：多 socket 服务器上要主动 `numactl` 绑核；否则一次跨节点内存访问就吃掉微秒级延迟。
- **容器/CPU 限额**：cgroup CPU quota 实际是“周期 + 配额”，与 CFS 调度相互影响；不要让 quota 太小导致任务经常被强制睡眠。
- **优先级 / 实时性**：能不用硬实时就不用；用了就要预留出 RT 带宽给关键任务。
- **可观测性**：
  - Linux：`perf sched` / `bpftrace` / `/proc/sched_debug` / `pidstat -w`。
  - Go：`GODEBUG=schedtrace=1` / `runtime/trace` / `pprof`。
  - tokio：`tokio-console` / `tracing`。

## 6. 现代调度器的几个趋势（点到为止）

- **用户态调度器接管**：Go / Rust / Java 21 virtual thread 都在把“调度”下放到 runtime。
- **eBPF 用于调度观测**：`bpftool` / `sched_ext` (Linux 6.12+) 允许用 BPF 程序**替换/扩展** CFS 调度策略——调度器可编程化。
- **节能与拓扑感知**：手机/笔记本调度器（EAS）把“能耗/性能”放进了调度目标；服务器调度器把“cache locality / NUMA”放进去。
- **AI 工作负载的反向冲击**：GPU 调度与 CPU 调度的协同（GPU 上下文切换成本远高于 CPU）正在重塑“任务”的定义。
- **可抢占协程**：Rust 的 `async` 默认非抢占；社区在讨论显式抢占点（`select_biased!`、loom 等），但**刻意**不学 Go 的全局信号抢占。

## 常见误区

- “**用 Go 写 CPU 密集型也能开百万 goroutine**”：能开不代表能跑得快；GOMAXPROCS 是 CPU 上限，过多 G 会让调度器自身成为瓶颈。
- “**tokio 多线程 = Go GMP**”：思路相近但 tokio 默认**不抢占**，CPU bound 的 future 会卡死 worker 线程；务必用 `spawn_blocking` 隔离阻塞。
- “**Windows UMS 是 Windows 版 goroutine**”：错。UMS 是 OS 提供的**机制**（让应用接管调度），而 goroutine 是**自带调度器的语言特性**。Go 在 Windows 上也跑 M:N，但 UMS 不参与。
- “**调度器越公平越好**”：对延迟敏感任务，“公平”意味着把时间片给不相干的任务；需要主动分组/绑核。
- “**CFS 是 Linux 唯一的调度器**”：除 CFS 外还有 `SCHED_FIFO/RR/DEADLINE`、`EEVDF`（6.6 之后）、`sched_ext`（6.12+）等多种路径。

## 相关阅读

- [`process.md`](process.md)：CFS 内部细节、上下文切换与观测
- [`concurrency.md`](concurrency.md)：M:N / 1:1 / N:1 线程模型
- [`io.md`](io.md)：I/O 多路复用（epoll/io_uring）——用户态调度器的“喂养”机制
- [`virtualization.md`](virtualization.md)：vCPU 调度与 KVM
- [`go/runtime/scheduler.md`](../go/runtime/scheduler.md)：Go GMP 详解
