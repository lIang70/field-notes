# netpoll (Go 网络 I/O 模型)

Go 网络 I/O 的核心思路：**用户态看起来是同步阻塞**（`conn.Read()` 像普通函数调用），**底层由 runtime/netpoll 做非阻塞 + 多路复用**。这一层是 Go 能在"一个 goroutine 处理一个连接"的语义下做到高并发的关键。

## TL;DR

- **同步语义 + 非阻塞实现**：业务代码写 `Read`/`Write` 像是阻塞调用；运行时把它们改写成"非阻塞 + 等待 netpoll 事件"的组合。
- **抽象层**：`runtime/netpoll`（源码 `src/runtime/netpoll.go`）屏蔽了 epoll / kqueue / IOCP 的差异。
- **平台后端**：Linux 用 epoll（LT 模式），BSD/macOS 用 kqueue，Windows 用 IOCP（完成端口，思路不同）。
- **与调度器协同**：G 阻塞在 I/O 上时会被 `gopark`；事件就绪后由 `netpoll` 调用 `goready` 重新入队。
- **代价**：每个连接一个 goroutine 并不"零成本"——栈、fd、调度都有开销，需要限并发。

## 总览：goroutine-per-connection

> **Go 选择了"看似阻塞"的同步 API**：用户写 `net.Conn.Read(buf)`，调用栈上是同步语义；运行时把它转成"非阻塞 + 等待事件"。

对比两种典型并发网络模型：

- **Reactor 模型**（典型如早期 Node.js、Netty）：单/少线程 + 回调/事件循环；业务逻辑必须以非阻塞方式写，否则一阻塞就拖垮整个 loop。
- **goroutine-per-connection**（Go 风格）：每条连接一个 goroutine，阻塞 API 随便用；运行时负责把"goroutine 阻塞"映射为"系统级非阻塞 + 事件唤醒"。

Go 走的是后者：API 像 Java 的 `InputStream.read`，但底层像 epoll 的 Reactor。两者结合的关键就在 `runtime/netpoll` 抽象层。

## 演进线索

> 这里给的是"思路"，不是逐版本的 changelog。具体函数签名/数据结构会随版本变化，但分层思想没变。

### 1. 早期：基于 `sched` 调度器的非阻塞 fd

- Go 1.0 之前的 `sched` 调度器在 `select` 语句上做调度，G 的阻塞通过 `select` 内部实现。
- **缺点**：调度器自身是单线程，全局锁竞争严重；fd 模型与调度耦合得也很紧。
- 详见 [`scheduler.md`](scheduler.md) 中关于旧调度器退场的部分。

### 2. 引入 `runtime/netpoll` 抽象层

- Go 1.0 起，net 包开始使用一个独立的"网络轮询器"抽象（`src/runtime/netpoll.go`）。
- 这一层职责清晰：把不同 OS 的多路复用 API（epoll / kqueue / IOCP）统一成几个函数：`netpollinit` / `netpollopen` / `netpollclose` / `netpoll`。
- 业务包（`net`、`net/http`、`crypto/tls`）只跟这一抽象层打交道，不再直接调 `epoll_*`。

### 3. Go 1.5+ GMP + 非阻塞 I/O 整合

- GMP 重构（详见 [`scheduler.md`](scheduler.md)）后，G 的阻塞语义被收敛到 `_Gwaiting` 状态。
- I/O 阻塞与 channel 阻塞、锁等待的"park/unpark"机制统一了：
  - 入口：`gopark` 把 G 从 `_Grunning` 切到 `_Gwaiting`，释放当前 M/P。
  - 出口：`goready` 把 G 放回 run queue，从 `_Gwaiting` 切回 `_Grunnable`。
- 也就是说"等 I/O"和"等 channel"在调度器视角下是一样的——这是 Go 同步语义能够"一视同仁"的根因。

### 4. 平台后端

netpoll 在每个 OS 下有不同的实现（`netpoll_epoll.go` / `netpoll_kqueue.go` / `netpoll_windows.go` 等）：

| 平台          | 机制     | 思路                                                     |
| :------------ | :------- | :------------------------------------------------------- |
| **Linux**     | `epoll`  | 事件就绪通知（reactor 风格），LT 模式                    |
| **BSD/macOS** | `kqueue` | 通用事件接口，与 epoll 思路接近                          |
| **Windows**   | IOCP     | **完成端口**：发起 overlapped I/O 后由内核在"完成"时通知 |

> **Windows 与 epoll 的方向差**：epoll 是"FD 状态变化时告诉你"（ready 通知）；IOCP 是"你发起 I/O，做完了内核再叫你"（completion 通知）。Go 在 Windows 下用 `netpoll_windows.go` 把 IOCP 适配成与 epoll 相同的"事件就绪"语义，业务代码完全感知不到。

## 关键数据结构与机制

> 下面的"伪代码"是按主流 Go 1.20+ 形态给出的概念示意；具体字段名会随版本微调（建议对照 `src/internal/poll/fd_poll_runtime.go`、`src/runtime/netpoll.go` 读源码）。

### 1. `internal/poll.FD`（net 包底层）

每个网络连接（TCP/UDP 套接字）在 Go 里都被包成 `FD`：

```go
// 简化：internal/poll.FD（示意）
type FD struct {
    Sysfd int // 真正的 OS 文件描述符

    pd *pollDesc // 关联到这个 fd 的"轮询描述符"，非阻塞 I/O 的核心

    // 一些状态与 I/O 方法
    IsStream      bool // TCP=true
    ZeroReadIsEOF bool
    // ...
}
```

业务层 `net.Conn` / `net.Listener` 最终都委托给 `FD` 调 I/O。`Sysfd` 在创建时就被 `SetNonblock` 设为非阻塞——这是 netpoll 能工作的前提。

### 2. `pollDesc`（runtime ↔ netpoll 桥接）

`pollDesc` 是 runtime 视角的"我关心这个 fd 上的可读/可写事件"：

```go
// 简化：internal/poll.pollDesc（示意）
type pollDesc struct {
    fd uintptr // 关联的 fd

    cs atomic.Uint32 // 保护后两位的状态位

    rg, wg uint32 // waitRead / waitWrite 序列号（防 ABA）
    // ... 与 runtime 共享的字段（runtime 通过 linkname 访问）
}
```

- 它**不属于** runtime 的 g/m/p，而是 `internal/poll` 包里的普通结构。
- 但 runtime 持有指向它的指针（通过 linkname 访问），因此能在事件就绪时直接找到它并 `goready` 等待中的 G。

### 3. `pollCache`（`pollDesc` 复用）

- 频繁创建/销毁连接 = 频繁创建/销毁 `pollDesc`。
- `internal/poll` 用一个 per-P 的 `pollCache` 做无锁复用，避免每次 `accept` 都走 `newpollDesc` → `mallocgc`。
- 这是"一连接一 goroutine"模型下少被提到但很重要的优化点。

### 4. `netpollblock` / `goready`（park / ready 协同）

阻塞路径伪代码（**简化**）：

```text
// netpollblock 示意：等待事件，直到 rg/wg 序列号变化
func netpollblock(pd *pollDesc, mode int) {
    // 1. 把自己（G）挂到 pollDesc 的等待队列上
    // 2. gopark：G 切到 _Gwaiting，释放 M/P
    // 3. 内核事件就绪后，netpoll 调用 netpollunblock
    // 4. netpollunblock 再调 goready → G 回 run queue
}
```

`gopark` 与 `goready` 是 runtime 里"让一个 G 睡觉/叫醒"的标准动作；channel、`sync.Mutex`、`netpoll` 都用同一对原语——这就是 Go 同步语义统一的体现。

## 典型 Read/Write 路径

下面以 `conn.Read(buf)` 为例（写路径对称，路径里 `WaitRead` 换 `WaitWrite` 即可）：

```text
用户代码
  conn.Read(buf)                       // 同步语义
    └─ net.Conn.Read
        └─ net.fd.Read
            └─ internal/poll.FD.Read
                ├─ 尝试非阻塞 Read（fd 已置 O_NONBLOCK）
                │   ├─ 成功：返回
                │   └─ EAGAIN：进入等待
                └─ FD.pd.WaitRead()
                    └─ pollDesc.waitRead()
                        └─ netpollblock(pd, 'r')
                            ├─ gopark(G)            ──► G 切到 _Gwaiting
                            └─ （netpoll 在另一处/被 sysmon 拉起）
                                └─ epoll_wait 返回该 fd
                                    └─ netpoll 遍历就绪列表
                                        └─ 对每个就绪 fd 调 netpollunblock
                                            └─ goready(G)            ──► G 回 _Grunnable
                └─ 调度器挑中 G 继续执行
                    └─ 重新尝试非阻塞 Read → 成功
```

> **关键点**：`Read` 并不是"一次调用从用户态阻塞到内核态返回"，而是"短忙试 + 等待 + 再试"三段式。这正是 Go 同步 API 不会拖垮整个进程的根因。

写路径（`Write`）与之对称：当 `Write` 遇到 `EAGAIN`（写缓冲满），G 同样 `gopark`，等 fd 可写时被 `goready`。

## 与 goroutine 调度器的衔接

> **一句话：netpoll 是"另一个入口"把 G 放回 run queue**。

正常情况下 G 由 `go` 语句、channel 操作、锁等"应用层事件"调度。netpoll 提供了一个"系统级事件"入口：

- `sysmon` 后台线程会周期性 `netpoll(timeout)`（很短），主动把就绪事件拉起来（见 [`scheduler.md`](scheduler.md) 的 sysmon 小节）。
- GC 期间等场景也会调 `netpoll` 顺便处理就绪事件。
- 也就是说：**即便没有 G 主动让出，sysmon 也能把就绪 I/O 对应的 G 拉回 run queue**，这是 Go 调度器"无回调"的关键。

为什么 Go 能用"看似阻塞的代码"实现高并发，**总结**就是：

- **API 同步**：业务代码简单、可线性推理。
- **底层非阻塞**：不会因为一个慢连接卡死一条 M。
- **调度器统一**：channel、锁、I/O 都用 `gopark`/`goready`，G 自然被均匀分到 P 上跑。
- **栈按需扩缩**：2KB 起，热点 goroutine 才占大栈，"一连接一 goroutine"才不会爆内存。

## fd 耗尽 / 内存膨胀：goroutine 不是免费的

> **反直觉点**：Go 写并发代码"很容易"，但 1 万个慢连接 = 1 万个 goroutine + 1 万个 fd + 1 万份栈内存。不要以为 goroutine 轻到可以无限开。

实际工程里要关注：

- **fd 上限**：每个连接占一个 fd。受限于 `ulimit -n`、进程级 `RLIMIT_NOFILE`、以及 Linux 的 `fs.nr_open` / `fs.file-max`。业务量大时务必调高。
- **goroutine 数量**：本身不是问题，问题是每个 goroutine 持有的栈（虽然 2KB 起，逃逸/扩容后会涨）和它对应的 `pollDesc` / 等待队列开销。
- **慢连接/慢请求**：恶意或异常的客户端不读/不写，会让 goroutine 长期 `_Gwaiting` 堆积。`net/http.Transport` 有 `MaxIdleConns` / `ReadTimeout` / `WriteTimeout` 等参数防御。
- **限并发**：即便用 goroutine 也要"限并发"，见 [`../concurrency/concurrency_patterns.md`](../concurrency/concurrency_patterns.md) 的 semaphore / worker pool。

> 调优建议：用 `lsof -p <pid> | wc -l` 看 fd 数；用 `pprof` 的 goroutine profile 看实际堆积的 G。

## 相关链接

- [`scheduler.md`](scheduler.md)：GMP、`gopark`/`goready`、`sysmon`。
- [`gc.md`](gc.md)：G 阻塞在 I/O 上时不会影响 GC（GC 关心的是 goroutine 的栈与堆对象关系）。
- [`memory_allocator.md`](memory_allocator.md)：连接对象（`FD` / `pollDesc`）的分配走 mcache/mcentral。
- [`write_barrier.md`](write_barrier.md)：并发 GC 与 I/O 本身无关，但栈扫描会扫到"正在等 I/O"的 G。
- [`../os/io.md`](../../os/io.md)：底层 epoll/select/poll 的语义（Go 屏蔽了这些）。
- [`../concurrency/concurrency_patterns.md`](../concurrency/concurrency_patterns.md)：goroutine 限并发、退出、泄漏防护。
