# Go Concurrency Patterns (并发模式)

Go 的 CSP 模型鼓励 "Do not communicate by sharing memory; instead, share memory by communicating."。以下是几种经典的并发设计模式。

> 经验法则：**任何启动 goroutine 的地方，都要明确它的退出条件**（`context`/`done`、输入 channel 关闭、计数完成等），否则非常容易 goroutine 泄漏。

## 1. Generator (生成器)

函数返回一个只读 Channel，并在内部启动 Goroutine 持续生产数据。务必支持取消，并在退出时 `close(out)`。

```go
// 需要 import：context
func generator(ctx context.Context) <-chan int {
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

## 2. Fan-In (扇入 / 多路复用)

将多个输入 Channel 合并到一个输出 Channel。

```go
// 需要 import：context, sync
func fanIn[T any](ctx context.Context, inputs ...<-chan T) <-chan T {
    out := make(chan T)
    var wg sync.WaitGroup
    wg.Add(len(inputs))

    forward := func(ch <-chan T) {
        defer wg.Done()
        for {
            select {
            case v, ok := <-ch:
                if !ok {
                    return
                }
                select {
                case out <- v:
                case <-ctx.Done():
                    return
                }
            case <-ctx.Done():
                return
            }
        }
    }

    go func() {
        defer close(out)
        for _, ch := range inputs {
            go forward(ch)
        }
        wg.Wait()
    }()

    return out
}
```

## 3. Pipeline (流水线)

将处理逻辑拆分为多个阶段，每个阶段由一组 Goroutine 和 Channel 组成。
`Source -> Stage1 -> Stage2 -> Sink`

```go
// 需要 import：context
// Stage 1: 产生数字（可取消）
func gen(ctx context.Context, nums ...int) <-chan int {
    out := make(chan int)
    go func() {
        defer close(out)
        for _, n := range nums {
            select {
            case out <- n:
            case <-ctx.Done():
                return
            }
        }
    }()
    return out
}

// Stage 2: 平方（可取消）
func sq(ctx context.Context, in <-chan int) <-chan int {
    out := make(chan int)
    go func() {
        defer close(out)
        for n := range in {
            select {
            case out <- n * n:
            case <-ctx.Done():
                return
            }
        }
    }()
    return out
}

// Main
ctx, cancel := context.WithCancel(context.Background())
defer cancel()

c := gen(ctx, 2, 3)
out := sq(ctx, c)
_ = out
```

## 4. Fan-Out / Worker Pool (扇出 / 工作池)

将任务分发给多个 worker 并发处理（**限制并发数量**，避免资源耗尽）。

```go
// 需要 import：context, sync
func worker(ctx context.Context, id int, jobs <-chan int, results chan<- int) {
    for j := range jobs {
        // 处理逻辑里也应该可取消
        select {
        case results <- j * 2:
        case <-ctx.Done():
            return
        }
    }
}

func main() {
    ctx, cancel := context.WithCancel(context.Background())
    defer cancel()

    jobs := make(chan int, 100)
    results := make(chan int, 100)

    // 启动 3 个 worker
    var wg sync.WaitGroup
    for w := 1; w <= 3; w++ {
        wg.Add(1)
        go func(id int) {
            defer wg.Done()
            worker(ctx, id, jobs, results)
        }(w)
    }

    // 发送任务
    for j := 1; j <= 5; j++ {
        jobs <- j
    }
    close(jobs)

    // 所有 worker 完成后关闭 results（由生产者侧关闭）
    go func() {
        wg.Wait()
        close(results)
    }()

    // 获取结果
    for r := range results {
        _ = r
    }
}
```

## 5. Context (上下文与取消)

控制 Goroutine 树的生命周期，处理超时和取消。

- `context.WithCancel`: 手动取消。
- `context.WithTimeout`: 超时自动取消。
- `context.WithValue`: 请求链路传值。

**原则**:

- Context 应该是函数的第一个参数。
- 只有在这一层需要取消子操作时，才创建新的 Context。
- 监听 `ctx.Done()` 来优雅退出。

```go
func operation(ctx context.Context) {
    select {
    case <-time.After(1 * time.Second):
        fmt.Println("done")
    case <-ctx.Done():
        fmt.Println("cancelled:", ctx.Err())
    }
}
```

## 6. Or-Done（避免 goroutine 泄漏的“保险丝”）

当你在消费一个输入 channel，但又需要支持取消时，可以用 `orDone` 包一层，让 `range`/读取在 `ctx.Done()` 时立刻退出。

```go
// 需要 import：context
func orDone[T any](ctx context.Context, in <-chan T) <-chan T {
    out := make(chan T)
    go func() {
        defer close(out)
        for {
            select {
            case <-ctx.Done():
                return
            case v, ok := <-in:
                if !ok {
                    return
                }
                select {
                case out <- v:
                case <-ctx.Done():
                    return
                }
            }
        }
    }()
    return out
}
```

## 7. Tee（复制一个数据流到多个下游）

把同一份输入同时发送到两个输出。常用于日志/监控旁路。

```go
// 需要 import：context
func tee[T any](ctx context.Context, in <-chan T) (<-chan T, <-chan T) {
    out1 := make(chan T)
    out2 := make(chan T)
    go func() {
        defer close(out1)
        defer close(out2)
        for v := range orDone(ctx, in) {
            o1, o2 := out1, out2
            for i := 0; i < 2; i++ {
                select {
                case o1 <- v:
                    o1 = nil
                case o2 <- v:
                    o2 = nil
                case <-ctx.Done():
                    return
                }
            }
        }
    }()
    return out1, out2
}
```

## 8. 并发上限（Semaphore / Bounded Concurrency）

不建固定 worker 池，而是对“同时进行的任务数”加上限（最常见的限并发模式）。

```go
// 需要 import：sync
// items / Item 为占位符：替换成你的业务类型即可
sem := make(chan struct{}, 20) // 最大并发 20
var wg sync.WaitGroup
for _, item := range items {
    wg.Add(1)
    sem <- struct{}{} // acquire
    go func(it Item) {
        defer wg.Done()
        defer func() { <-sem }() // release
        // do work...
    }(item)
}
wg.Wait()
```

## 9. errgroup（带取消的并发任务编排）

当你要并发做 N 件事，并且**任意一个失败就取消其他**，用 `golang.org/x/sync/errgroup` 很省心。

```go
// 需要 import：context, golang.org/x/sync/errgroup
// tasks / t.Run 为占位符：替换成你的业务逻辑即可
g, ctx := errgroup.WithContext(context.Background())
for _, t := range tasks {
    t := t
    g.Go(func() error {
        return t.Run(ctx)
    })
}
if err := g.Wait(); err != nil {
    // 第一个 error 会返回，同时 ctx 会被取消
}
```

## 10. 限流（Rate Limiting）

用 `time.Ticker` 做简单 QPS 限制；更复杂的令牌桶可用 `golang.org/x/time/rate`。

```go
// 需要 import：time
// requests 为占位符：替换成你的请求/任务流即可
ticker := time.NewTicker(100 * time.Millisecond) // 每 100ms 放行 1 次
defer ticker.Stop()
for req := range requests {
    <-ticker.C
    _ = req
}
```

## 11. 优雅退出（Graceful Shutdown）

服务型程序常见：监听 OS 信号，取消 ctx，等待 goroutine 收尾，再退出。

```go
// 需要 import：context, os, os/signal, sync, syscall
ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
defer stop()

var wg sync.WaitGroup
wg.Add(1)
go func() {
    defer wg.Done()
    <-ctx.Done()
    // cleanup...
}()

wg.Wait()
```

## 12. 常见坑与检查清单（很重要）

- **谁来 close channel**：只由**发送方**关闭；接收方关闭会引发数据竞争/恐慌。
- **读取要处理 ok**：`v, ok := <-ch`，`ok=false` 表示对方关闭，别再发送/转发。
- **避免 goroutine 泄漏**：每个 goroutine 都要能因 `ctx.Done()` 或输入关闭而退出；必要时用 `orDone`。
- **select + nil channel**：把某个 channel 置 `nil` 可临时禁用对应 case（例如 Tee 的写两次技巧）。
- **close 两次会 panic**：需要确保只 close 一次（用 `sync.Once` 或由唯一拥有者关闭）。
