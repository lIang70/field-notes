# async / blocking 边界（trait 异步、实现同步）

> tokio 上最容易出的事故不是死锁，是"在 async 上下文里做了阻塞 I/O"——单次看起来只慢几毫秒，压到并发上就是整池 worker 被占死。
> 案例来自 flowcraft 的 `rust/`：一部分已经按这个规则写了，规则本身还在设计稿里（下面会标清楚哪些是已落地、哪些是计划）。

## TL;DR

- **规则一句话：trait 边界异步，实现内部同步，跨边界用 `spawn_blocking` 包一层**。公共 API 保持 `async fn`，实现里该阻塞就阻塞。
- **修复形态是"搬"，不是"改写"**：现有同步实现本身是对的、测试也覆盖了，只在 `async fn` 外壳里包一层。风险接近零，review 成本低。
- **约定必须有门禁**：靠 review 记住"不要写 `std::fs::read`"是不现实的；可行的是 `clippy.toml` 的 `disallowed-methods`，再对 `blocking` 模块与 `#[cfg(test)]` 局部豁免。
- **不要用 `tokio::fs` 当默认**：它背后同样是 `spawn_blocking`，但没有统一的错误映射和预算控制，等于把一个可以集中的问题散到各处。
- **允许同步 I/O 的位置有三个**（同步构造 `new/from_*`、测试、已经在 `spawn_blocking` 闭包里的代码）——漏掉任何一条，规则都会变成"处处例外"。

## 1. 已落地的两个站点

```rust
// caps/workspace/local.rs：cap-std 句柄共享在 Arc 上，读写整体搬到阻塞线程
let dir = Arc::clone(&self.dir);
tokio::task::spawn_blocking(move || operation(&dir)).await?
```

```rust
// caps/sandbox/session/terminal.rs：等待子进程、resize、reap 都是阻塞调用
let status = tokio::task::spawn_blocking(move || child.wait()).await?;
```

两处共同的形状：**同步操作整体挪进去**（不是"在闭包里拼一个小 await"），返回值再映射成分类型错误。终端的注释里写了代价："每次 run 都要在阻塞线程上 reap——这是 pty 的代价"。

## 2. 规则（设计稿，`docs/plans/`，未写码）

```text
1. `async_trait` 的 `async fn` 里不得出现：
     std::fs::*  /  std::net::ToSocketAddrs  /  std::thread::sleep
2. 跨边界统一走 blocking::run(closure)：
     spawn_blocking + 统一错误映射 + tracing 字段（flowcraft.blocking）
3. CPU 密集同理：spawn_blocking，不要在 worker 线程上算
4. 允许同步 I/O 的三处：BuildCtx 的同步构造、#[cfg(test)]、已在阻塞闭包内
5. 门禁：clippy.toml 的 disallowed-methods 禁掉上面这些符号
```

门禁大致长这样（生效后就是"第二年也不会退化"的那层保险）：

```toml
# clippy.toml
disallowed-methods = [
  { path = "std::fs::read", reason = "use blocking::run" },
  { path = "std::fs::write", reason = "use blocking::run" },
  { path = "std::fs::read_to_string", reason = "use blocking::run" },
  { path = "std::fs::symlink_metadata", reason = "use blocking::run" },
  { path = "std::fs::canonicalize", reason = "use blocking::run" },
  { path = "std::thread::sleep", reason = "use tokio::time::sleep" },
]
```

外加一个已经开着的 lint：`future_not_send = "warn"`——把非 `Send` 的东西带进 `async fn` 时能提示（抓不到全部，但抓得住常见那类）。

## 3. 怎么判断"这段代码真的在 async 上"

扫描方式比人眼靠谱：`grep -rl "async fn"` 再对每个文件数 `std::fs::|ToSocketAddrs|thread::sleep`，然后**逐个看调用上下文**。三类结果：

| 位置 | 判定 |
| :--- | :--- |
| `async_trait` 实现里、`tokio::spawn` 的 future 里、`.await` 链上 | 真缺陷，要搬 |
| 同步构造（`new` / `from_*` / `BuildCtx`） | 允许，但要在注释里写明"这是构造期" |
| 已经在 `spawn_blocking` 闭包里 | 允许，而且**不要**再嵌套一层 |

## 4. 不做的事

- **不把 trait 改成同步**：那会把阻塞传染给所有调用方，包括本来就是异步的那些。
- **不引入 `tokio::fs` 作为默认**：它的错误映射与预算控制不统一；要么全用 `blocking::run`，要么全不用。
- **不在 `spawn_blocking` 里写长轮询**：阻塞线程池默认上限 512，长轮询会把池子占满；该用 `watch` / channel 等事件。

## 5. 反模式

- `async fn` 里直接 `std::fs::read`：单测全绿，压测直接把吞吐砍半。
- `std::thread::sleep` 当退避：worker 线程被睡死，定时器全乱。
- 在 `async fn` 里做大 JSON 序列化（CPU 密集）：同样要 `spawn_blocking`。
- 用 `block_in_place` 当万能开关：它只在多线程 runtime 上可用，且会阻塞当前 worker，不是 `spawn_blocking` 的替代。
- 把规则写在 README 里，没有 lint：一年后没人记得。

## 6. 清单

- [ ] `async fn` 体内没有 `std::fs` / `std::thread::sleep` / 同步 DNS
- [ ] 跨边界的阻塞调用都走同一个 helper（有统一错误映射与 tracing）
- [ ] 允许的三处例外在注释里写明理由
- [ ] `clippy.toml` 的 `disallowed-methods` 已开，且有豁免列表
- [ ] `future_not_send` 之类的目标 lint 在 workspace 级开启
- [ ] 阻塞线程池的压力测试：并发下 worker 不被占满

## 相关链接

- [`bounds.md`](bounds.md)（超时与上限是同一类"有界"）
- [`workspace.md`](../architecture/workspace.md)（这些 capability 的模块位置与 feature 边界）
- [`../../go/engineering/optimization.md`](../../go/engineering/optimization.md)（Go 侧对应的"async/阻塞"与分配治理）
- [`../../os/concurrency.md`](../../os/concurrency.md)（线程池、阻塞与调度的一般原理）
