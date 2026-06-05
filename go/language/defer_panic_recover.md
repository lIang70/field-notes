# Defer, Panic, Recover

## Defer

Go 的 `defer` 语句用于延迟函数调用，常用于资源释放（解锁、关闭文件）。

### 演进历史

1. **Go 1.12 及之前 (堆分配)**:
   - `defer` 结构体分配在堆上。
   - 性能较差，循环中使用会导致频繁分配。
2. **Go 1.13 (栈分配优化)**:
   - 编译器在栈上预留空间给 `defer`。
   - 减少了堆分配（`mallocgc`）的开销，性能提升 30%。
3. **Go 1.14+ (Open Coded Defer)**:
   - **内联优化**: 对于简单的、不位于循环中的 `defer`，编译器直接将 deferred 函数调用插入到函数返回点。
   - **零开销**: 运行时几乎不需要创建 `_defer` 结构体，性能与直接调用函数无异。
   - 依赖位图 (Bitmap) 记录哪些 defer 需要执行（处理条件分支）。

### 执行顺序

- **后进先出 (LIFO)**: 栈式执行。
- **参数预计算**: `defer fmt.Println(time.Now())` 中的 `time.Now()` 会在 defer **声明时**计算，而不是执行时。

## Panic & Recover

### Panic

- `panic()` 会停止当前函数的正常执行。
- 也就是逐层向上执行当前 Goroutine 调用栈中已注册的 `defer` 函数。
- 直到遇到 `recover` 或者 Goroutine 栈顶（导致程序崩溃）。

### Recover

- 必须在 `defer` 函数中调用。
- 如果在 `panic` 流程中调用了 `recover`：
  - 停止 panic 扩散。
  - 获取 panic 传入的错误值。
  - 函数正常返回（从执行 `recover` 的那个 `defer` 所属的函数返回，**不会**回到 panic 发生点继续执行）。

### 跨协程问题

- `panic` **只能** 触发当前 Goroutine 的 defer。
- 一个 Goroutine 的 panic **不能** 被另一个 Goroutine 的 recover 捕获。
- 如果子协程 panic 且未 recover，**整个进程** 会崩溃。
- **最佳实践**: 在启动 Goroutine 时使用 `defer` + `recover` 兜底。

```go
go func() {
    defer func() {
        if r := recover(); r != nil {
            fmt.Println("Recovered in goroutine:", r)
        }
    }()
    // 业务逻辑
}()
```
