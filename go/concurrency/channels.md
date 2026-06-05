# Channels (通道) Implementation

Channel 是 Go 并发模型 (CSP) 的核心。它在 Runtime 中由 `hchan` 结构体表示。

## 核心数据结构: hchan

```go
type hchan struct {
    qcount   uint           // 队列中的元素总数
    dataqsiz uint           // 环形队列的大小 (缓冲容量)
    buf      unsafe.Pointer // 指向底层环形数组的指针
    elemsize uint16         // 元素大小
    closed   uint32         // 是否关闭
    elemtype *_type         // 元素类型
    sendx    uint           // 发送索引
    recvx    uint           // 接收索引
    recvq    waitq          // 等待接收的 Goroutine 队列
    sendq    waitq          // 等待发送的 Goroutine 队列

    lock mutex              // 互斥锁，保护 hchan 所有字段
}
```

## 关键机制

### 1. 锁 (Lock)

- Channel **不是无锁的**。
- 所有操作（发送、接收、关闭）都需要先获取 `hchan.lock`。
- 锁的粒度很小，仅涉及指针移动和队列操作，数据拷贝发生在锁外（部分情况）或锁内（视实现细节而定，通常拷贝数据较快）。

### 2. 环形缓冲区 (Circular Buffer)

- 用于 **Buffered Channel**。
- 使用 `buf` 数组配合 `sendx` 和 `recvx` 索引实现环形队列。
- 避免了频繁的内存分配和回收。

### 3. 等待队列 (Wait Queues)

- `recvq` 和 `sendq` 是双向链表。
- 存储的是 `sudog` 结构体（封装了 G 及其等待的数据元素）。
- 当 Channel 满（发送时）或空（接收时），当前 G 会被打包成 `sudog` 挂入对应队列，并进入 `_Gwaiting` 状态（Park）。

## 操作流程

### 发送 (Send) `ch <- val`

1. **直接发送**: 如果 `recvq` 不为空（有 G 在等待接收），直接将数据拷贝给等待的 G，并唤醒该 G。**绕过缓冲区**。
2. **缓冲发送**: 如果 `recvq` 为空，但缓冲区未满，将数据拷贝到 `buf[sendx]`，更新索引。
3. **阻塞发送**: 如果缓冲区满（或无缓冲），将当前 G 包装成 `sudog` 放入 `sendq`，当前 G 挂起 (gopark)。

### 接收 (Receive) `val := <- ch`

1. **直接接收**: 如果 `sendq` 不为空：
   - **无缓冲**: 直接从发送者 G 拷贝数据。
   - **有缓冲**: (此时缓冲区必满) 从缓冲区头部读数据，将发送者 G 的数据拷贝到缓冲区尾部，唤醒发送者。
2. **缓冲接收**: 如果 `sendq` 为空，但缓冲区有数据，从 `buf[recvx]` 读数据，更新索引。
3. **阻塞接收**: 如果缓冲区空，将 G 放入 `recvq`，挂起。

## Select 实现原理

- `select` 语句对应 `runtime.selectgo`。
- **乱序轮询**: 随机生成遍历顺序，防止饥饿。
- **流程**:
  1. 加锁所有涉及的 Channel (按地址排序防止死锁)。
  2. 遍历查看是否有就绪的 Channel。
  3. 如果有，执行对应 case，解锁返回。
  4. 如果无，将当前 G 挂入所有 Channel 的等待队列。
  5. 被唤醒时，从所有队列中移除自己。

---

# Channels（使用语义 / 实战要点）

上面偏 runtime 视角；下面补充**写业务代码时必须掌握的 channel 语义与坑点**。

## 1. 关闭（close）语义：只由发送方关闭

- **原则**：`close(ch)` 只由**发送方（owner）**执行；接收方不应该关闭（否则发送者可能 panic）。
- **发送到已关闭 channel**：必然 `panic`。
- **从已关闭 channel 接收**：立刻返回零值；并且 `ok=false`。

```go
v, ok := <-ch // ok=false 表示 channel 已关闭且缓冲已耗尽
```

- **`range ch`**：会一直读到 channel 关闭且缓冲耗尽才退出。
- **close 的“广播效应”**：多个接收者都会被唤醒并读到 `ok=false`（常用于通知退出）。
- **close 两次会 panic**：多生产者场景用 `sync.Once` 或“集中关闭者”模式保证只 close 一次。

## 2. Buffered vs Unbuffered：同步点 vs 队列

- **无缓冲 channel**：发送与接收必须“握手”同时发生，是天然的同步点。
- **有缓冲 channel**：缓冲未满时发送不会阻塞；缓冲为空时接收会阻塞。
- **背压（Backpressure）**：通过小缓冲或无缓冲，让上游在下游慢时自然阻塞，避免无限堆积。
- **缓冲大小不是越大越好**：过大可能掩盖慢消费问题，导致内存膨胀/延迟抖动。

## 3. `select` 的关键语义

- **就绪 case 的选择**：Go 会做伪随机选择（尽量公平，但不保证严格公平/确定性）。
- **`default` 分支**：如果加了 `default`，`select` 永不阻塞，很容易写出“忙等”（CPU 飙高）。

```go
for {
    select {
    case v := <-ch:
        _ = v
    default:
        // 这里如果什么都不做，就是 busy loop
        time.Sleep(1 * time.Millisecond) // 或者移除 default
    }
}
```

## 4. nil channel：可用于“动态开关 case”

- **对 nil channel 的发送/接收会永久阻塞**。
- **在 select 中**：`case <-nilCh:` 永远不会就绪，相当于禁用该 case（常用于状态机/tee 技巧）。

## 5. 方向（只读/只写）与 API 设计

- 返回给调用方时，尽量返回只读：`<-chan T`。
- 传入 worker 时，尽量把方向收窄：`jobs <-chan Job`、`results chan<- Result`。
- 好处：减少误用（比如外部 close/发送）。

## 6. “谁负责关闭”模式（Owner-close）

一种非常稳的结构：**创建 channel 的 goroutine 同时负责关闭它**。

```go
func produce(ctx context.Context) <-chan int {
    out := make(chan int)
    go func() {
        defer close(out)
        for i := 0; ; i++ {
            select {
            case out <- i:
            case <-ctx.Done():
                return
            }
        }
    }()
    return out
}
```

## 7. 泄漏（Leak）与退出：优先用 ctx / done

常见泄漏原因：发送方/接收方永远阻塞在 channel 操作上（没有退出条件）。

- **建议**：goroutine 内部的循环都要监听 `ctx.Done()`（或 done channel）。
- **消费输入时**：如果输入可能永不关闭，用“orDone”模式包一层，保证可取消退出（见 `concurrency_patterns.md`）。

## 8. `time.After` 的小坑（循环里慎用）

在热循环中频繁 `time.After(d)` 会不断分配定时器对象；更稳的是 `time.NewTimer` 复用，并注意 `Stop/Reset`。

## 9. 速查清单（写并发时对照）

- **只有发送方 close**，并且只 close 一次。
- **接收用 `v, ok := <-ch`** 或 `range`，别忽略关闭语义。
- **select 不要滥用 default**，避免 busy loop。
- **给 goroutine 明确退出条件**（`ctx.Done()`/输入关闭/计数完成）。
- **控制并发与背压**：worker pool / semaphore / 小缓冲。
