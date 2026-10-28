# tokio 实战：watch / broadcast / 世代切换 / 锁的选择

> 平台层用 tokio 时，真正需要设计的不是"用不用 async"，而是**信令怎么传、旧资源怎么退役、锁持有多久**。
> 案例来自 flowcraft 的 `rust/`（MIT）：`watch` 3 处以上、`broadcast` 1 处、一次完整的世代切换。

## TL;DR

- **`watch` 传"最新状态 + 版本号"**，`broadcast` 传"每条事件"——选错的代价是反的：状态用 broadcast 会丢事件顺序语义，事件用 watch 会丢中间过程。
- **`broadcast` 的 `Lagged` 必须显式处理**：消费者跟不上时，不能静默丢，要发一条"缺口事件"告诉它从哪续读。
- **世代切换的顺序是 parse → build → 起新 → 换入 → 排空旧**。换入在排空之前，所以新版在旧版退役期间已经在服务。
- **排空失败/超时不是 reload 失败**：换入已经发生，报 `drained: false` 并把旧世代留到关闭时重试，而不是回滚一个已经生效的切换。
- **`std::sync::Mutex` 在 async 代码里是对的**——只要守卫不跨 `.await`；需要跨 `await` 持有才换 `tokio::sync::Mutex`。

## 1. watch：状态 + 版本

三处真实用法，形状一致：

| 用途 | 载荷 | 读法 |
| :--- | :--- | :--- |
| 会话注册表的 settle 通知 | `watch::Sender<u64>`（代际） | 等待者比对自己那一代 |
| 会话日志的修订号 | `watch::Sender<u64>` | 变了就重读增量 |
| 子进程退出状态 | `watch::Receiver<Option<ExitStatus>>` | `None` → `Some` 即"已退出" |

- `watch` 天然适合"只关心最新值"的场景：慢消费者会**跳过中间值**，这正是我们想要的。
- 用 `u64` 当载荷是一种便宜的"版本号"：通知不是说"发生了什么"，而是"你手上的东西可能过期了"。
- `Option<T>` 用一个通道表达"还没发生 / 已发生"，避免多开一个 flag 通道。

## 2. broadcast：事件流 + 显式缺口

会话日志对外提供事件流，内部是 `broadcast::Sender<Event>`（容量 `WATCH_FANOUT`）。关键在慢消费者分支：

```rust
Err(broadcast::error::RecvError::Lagged(_)) => {
    // 丢了哪些事件是未知的，所以只告诉读者"从哪个序号续读"
    let _ = sender.send(Event::Lag { next_seq: cursor }).await;
    return;
}
Err(broadcast::error::RecvError::Closed) => return,
```

- 这里的 `sender` 是**给该订阅者的 `mpsc::Sender`**（`broadcast` 那端只负责 `subscribe` / `recv`，它的 `send` 是同步的）。
- **`Lagged` 不能 `continue`**：调用方会以为没丢过数据。
- 发一条 `Lag { next_seq }` 把"缺口"变成协议的一部分，读者可以去存储补读——这正是"日志是权威，广播只是通知"的分工（存储侧的增量读见 [桌面 agent 的性能治理](../../go/engineering/optimization.md) 的 `LoadAfter`）。
- 快照边界要一起处理：订阅时先拿一个 `snapshot` 序号，之后丢弃 `seq < snapshot` 的事件，避免重放与实时流重叠。

## 3. 世代切换（reload）

```text
parse → build/plan → 起新世代 → swap（新世代开始服务）
                    → drain 旧世代（有界 grace，默认 10s）
                    → 排空失败：报 drained=false，旧世代留到 shutdown 重试
```

- **parse/build 在创建任何东西之前**：坏文档只得到 `Validation` 错误，运行中的世代一动不动（测试里断言 `generation()` 仍是旧值、旧文件还在）。
- **swap 在 drain 之前**：旧世代退役期间新版已经在服务，吞吐不掉。
- **配置分层要写清楚**：`platform.yaml`（endpoint、TLS、grant 策略）**不可热重载**，只能重启；只有部署文档能换世代。把"什么能热更"当成契约写进文档，比让用户试出来强。
- **失败语义**：`drained: false` 是"报告"而非"失败"——切换已经发生，回滚反而制造不一致。旧世代被保留并在关闭时重试。

## 4. 锁：按"守卫是否跨 await"选

```rust
// 能力内部：短临界区，std Mutex 就够（永远不在 await 上持有）
let mut entries = self.entries.lock().map_err(|_| poisoned())?;
entries.insert(id, entry);
drop(entries);
// …这里才 await

// 世代切换：守卫要跨 await（drain 期间要保持排他），用 tokio 的
let mut current = self.current.lock().await;
```

- **`std::sync::Mutex`**：临界区只有几行、不含 `.await`。好处是便宜，且能在非 async 上下文里用。
- **`tokio::sync::Mutex` / `RwLock`**：守卫必须跨 `.await` 存活时。代价是更慢，且只能在 async 上下文中用。
- **毒化（poisoning）也要处理**：`lock()` 返回 `Err` 时映射成分类型错误，不要 `unwrap`（workspace 级 lint 本来就 `unwrap_used = "deny"`）。
- 读多写少的开关类状态优先 `AtomicU64` / `RwLock`，别用 `Mutex` 当门闩。

## 5. 超时与取消

- 每个"等外部"的地方都有界：`timeout(CLOSE_UNWIND, session.terminate())`、`timeout(budget, session.close())`、`TERMINATE_GRACE` 等 `await_exit()`。
- **超时后的动作要写清**：是"终止并算失败"，还是"放弃等待但让它在后台继续"？两棵树的实现差异最容易在这里露出来。
- 没有 `select!` 的地方不要硬塞：多个信号源合并时优先想"能不能用一个 `watch` 表达状态"，而不是把五个 future 塞进一个 `select!`。
- 测试里**不靠暂停时钟**：需要时间语义的地方注入 `Clock`（`Arc<dyn Fn() -> SystemTime>`），比 `tokio::time::pause()` 更稳、更容易在并发测试里用。

## 6. 反模式

- 用 `broadcast` 传"当前状态"：慢消费者会看到过期状态，且顺序不可假设。
- 用 `watch` 传事件：中间过程直接消失。
- 在 `std::sync::Mutex` 守卫还活着时 `.await`：需要 `Send` 的 future 会直接编译失败（`MutexGuard: !Send`）；但 `current_thread` / `block_on` 这类不要求 `Send` 的上下文编得过，然后在运行时死锁。
- reload 时先 drain 再 swap：切换窗口内没有可用世代。
- 把"排空超时"当致命错误：已经生效的切换被回滚，比不排空更糟。
- 用 `spawn` 起的任务没人持有 `JoinHandle`，关闭时静默泄漏。

## 7. 清单

- [ ] 状态用 `watch`、事件用 `broadcast`，且 `Lagged` 有显式缺口事件
- [ ] 订阅先取快照序号，丢弃快照前的事件
- [ ] reload 顺序：parse/build → 起新 → swap → 有界 drain
- [ ] 排空失败是"报告 + 延后重试"，不是回滚
- [ ] 锁按"守卫是否跨 await"选型，毒化有映射
- [ ] 所有跨进程/跨设备的等待都有超时，且超时后语义明确
- [ ] 时间相关测试注入时钟，不用真实 sleep

## 相关链接

- [async / blocking 边界](../engineering/async_blocking.md)（阻塞调用怎么搬出 async 上下文）
- [有界容器](../engineering/bounds.md)（超时、队列容量、排空 grace 都属于"有界"）
- [Rust workspace 的形状](../architecture/workspace.md)（这些通道与锁分布在哪些模块）
- [可观测性](../../go/engineering/observability.md)（换世代这件事怎么归因到日志）
