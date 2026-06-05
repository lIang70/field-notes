# Go 并发场景与模板速查（Scenarios → Templates）

这份文档按“你在业务里会遇到的并发场景”组织，给出**最合适的模板组合**（`context` / `channel` / `errgroup` / worker pool / semaphore / rate limit 等）。

> 建议阅读顺序：先看 `channels.md`（语义与坑），再看 `concurrency_patterns.md`（组合模式），最后用本页当“选型与模板索引”。

## 0. 总原则（先记住这 4 条）

- **生命周期**：任何 goroutine 都要能因 `ctx.Done()` / 输入关闭 / 计数完成而退出。
- **取消传播**：整体超时/取消优先用 `context.WithTimeout/WithCancel`，并 `defer cancel()`。
- **并发上限**：默认要“限并发”（worker pool 或 semaphore），不要无限起 goroutine。
- **关闭规则**：只由**发送方**关闭 channel；接收方不 close。

---

## 1) 批量请求/批量 IO：并发执行 + 任一失败取消全部

**适用**：并发调用 HTTP/RPC/DB；任何一个失败就整体失败；需要超时。

**推荐模板**：`errgroup.WithContext` +（可选）semaphore 限并发。

```go
// import: context, golang.org/x/sync/errgroup
g, ctx := errgroup.WithContext(context.Background())

// 可选：限并发（见场景 2）
for _, task := range tasks {
    task := task
    g.Go(func() error {
        return task.Run(ctx)
    })
}
if err := g.Wait(); err != nil {
    // 任一失败：ctx 已取消，其它任务应尽快退出
}
```

---

## 2) 不固定 worker：只想限制“同时进行的任务数”

**适用**：遍历 items 并处理；希望“最多 N 个同时跑”；每个任务独立。

**推荐模板**：semaphore（带缓冲的 `chan struct{}`）+ `WaitGroup`。

```go
// import: sync
sem := make(chan struct{}, 20) // 最大并发 20
var wg sync.WaitGroup

for _, item := range items {
    item := item
    wg.Add(1)
    sem <- struct{}{} // acquire
    go func() {
        defer wg.Done()
        defer func() { <-sem }() // release
        // do work(item)
    }()
}
wg.Wait()
```

**注意**：如果任务需要可取消，把 `ctx` 带进去并在任务内部监听 `ctx.Done()`。

---

## 3) Producer-Consumer：生产者产出任务，多个消费者处理

**适用**：任务流不断到来；希望固定 worker 数；天然背压。

**推荐模板**：worker pool + “owner-close” 关闭 results。

```go
// import: context, sync
func worker(ctx context.Context, jobs <-chan Job, results chan<- Result) {
    for j := range jobs {
        select {
        case results <- handle(ctx, j):
        case <-ctx.Done():
            return
        }
    }
}

func run(ctx context.Context, workers int, jobs <-chan Job) <-chan Result {
    results := make(chan Result)
    var wg sync.WaitGroup
    wg.Add(workers)
    for i := 0; i < workers; i++ {
        go func() { defer wg.Done(); worker(ctx, jobs, results) }()
    }
    go func() {
        defer close(results) // 由发送方关闭
        wg.Wait()
    }()
    return results
}
```

---

## 4) 多阶段流水线：Source → Stage1 → Stage2 → Sink

**适用**：数据需要分阶段处理；每阶段可并行；要可取消、防泄漏。

**推荐模板**：pipeline + 每阶段监听 `ctx.Done()`，退出时 `close(out)`。

```go
// import: context
func stage(ctx context.Context, in <-chan T) <-chan U {
    out := make(chan U)
    go func() {
        defer close(out)
        for v := range in {
            select {
            case out <- transform(v):
            case <-ctx.Done():
                return
            }
        }
    }()
    return out
}
```

---

## 5) 多路输入合并：Fan-In（N→1 汇总）

**适用**：多个 worker/多个来源汇聚到单个下游；下游顺序不重要。

**推荐模板**：Fan-In + `WaitGroup` 收口 + `close(out)`。

> 代码见 `concurrency_patterns.md` 的 `fanIn` 泛型版本。

---

## 6) 广播通知：一次事件唤醒所有 goroutine

**适用**：停止信号、配置热更通知、关闭事件。

**推荐模板**：`close(done)` 广播（比往 channel 发送一个值更稳）。

```go
done := make(chan struct{})
go func() {
    // ... 某个时刻触发 ...
    close(done) // 广播：所有 <-done 都会立刻返回
}()

select {
case <-done:
    // stop
case <-ctx.Done():
}
```

---

## 7) 限流：控制 QPS / 速率

**适用**：外部接口限速；后台任务节流。

**推荐模板**：`time.Ticker`（简单）或 `x/time/rate`（更完整）。

```go
// import: time
t := time.NewTicker(100 * time.Millisecond)
defer t.Stop()
for req := range requests {
    <-t.C
    _ = req
}
```

---

## 8) 超时：整体超时 vs 局部等待

**适用**：请求 SLA / 单步等待 / 重试退避。

- **整体超时（推荐）**：`context.WithTimeout`，让超时能向下游传播。
- **局部等待**：`time.NewTimer`/`time.Sleep`（不需要影响下游时）。

```go
// import: context, time
ctx, cancel := context.WithTimeout(parent, 2*time.Second)
defer cancel()
err := do(ctx)
_ = err
```

---

## 9) 优雅退出：服务收到信号后停止接活并等待收尾

**适用**：HTTP 服务、消费者服务、定时任务服务。

**推荐模板**：`signal.NotifyContext` + `WaitGroup`。

```go
// import: context, os, os/signal, sync, syscall
ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
defer stop()

var wg sync.WaitGroup
// wg.Add(1); go ...
<-ctx.Done() // 收到退出信号
wg.Wait()
```

---

## 10) “只做一次”的并发去重：同一个 key 同时来只执行一次

**适用**：缓存击穿保护、并发刷新、同一资源同时加载。

**推荐模板**：`golang.org/x/sync/singleflight`（比自己用 map+锁+channel 更不易错）。

> 这里不展开代码；需要的话我可以补一段“单飞 + ctx + 过期缓存”组合模板。

---

## 11) 常见组合建议（选型小抄）

- **批量外部调用**：`errgroup.WithContext` +（必要时）semaphore +（必要时）rate limit
- **持续任务流**：worker pool + 背压（小缓冲 jobs）+ 广播 done/ctx 取消
- **多阶段计算**：pipeline +（可选）每阶段内部做限并发
- **汇总多来源**：fan-in + 下游单消费者（或再 fan-out）

---

## 12) 并发请求“竞速”：取最快成功结果，其余丢弃/取消（Hedged / Race）

**适用**：同一请求可以发往多个副本/多机房/多线路；取**第一个成功**的响应作为结果，其余请求取消。

**推荐模板**：`context.WithCancel` + `resultCh`（缓冲 1）+ `sync.Once`，并确保 goroutine 在发送时也监听 `ctx.Done()`。

```go
// import: context, errors, sync
// 语义：返回“第一个成功”的结果；如果全失败，返回最后一个 error（或你可以改成聚合 error）。
func firstSuccess[T any](parent context.Context, fns ...func(context.Context) (T, error)) (T, error) {
    var zero T
    if len(fns) == 0 {
        return zero, errors.New("no candidates")
    }

    ctx, cancel := context.WithCancel(parent)
    defer cancel()

    resultCh := make(chan T, 1)                 // 关键：缓冲 1，确保最快者不会因无人接收而阻塞
    errCh := make(chan error, len(fns))         // 关键：足够缓冲，失败者不会卡在 err 发送上
    var once sync.Once
    var wg sync.WaitGroup
    wg.Add(len(fns))

    for _, fn := range fns {
        fn := fn
        go func() {
            defer wg.Done()
            v, err := fn(ctx) // 必须把 ctx 传到下游（HTTP/RPC/DB 才能真正取消）
            if err != nil {
                select {
                case errCh <- err:
                case <-ctx.Done():
                }
                return
            }
            once.Do(func() {
                // 只有第一个成功者能进入这里：发送结果并取消其它竞速者
                select {
                case resultCh <- v:
                    cancel()
                case <-ctx.Done():
                }
            })
        }()
    }

    // 等待第一个成功，或所有都失败
    go func() { wg.Wait(); close(errCh) }()

    select {
    case v := <-resultCh:
        return v, nil
    case <-ctx.Done():
        // parent 取消/超时
        return zero, ctx.Err()
    case err := <-errCh:
        // 这里拿到的是“某一个失败”。继续把剩余的错误消费完，直到确认没有成功。
        last := err
        for e := range errCh {
            last = e
        }
        // 如果在 draining 期间有人成功，resultCh 会有值；这里再检查一次
        select {
        case v := <-resultCh:
            return v, nil
        default:
            return zero, last
        }
    }
}
```

**注意**：

- “最快回复”通常应理解为 **最快成功（2xx/无 error）**；否则最快的可能是错误/超时。
- 要让“取消真的生效”，下游调用必须使用可取消的 API（例如 `http.NewRequestWithContext`、DB/RPC 的 ctx 版本）。

---

## 参考

- `channels.md`：channel 语义与坑（close/缓冲/select/nil/泄漏等）
- `concurrency_patterns.md`：Generator/Fan-In/Pipeline/Worker Pool/orDone/tee/errgroup/semaphore 等模式代码
