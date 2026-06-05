# Escape Analysis 案例与性能优化

> 这篇是 [`escape_analysis.md`](escape_analysis.md) 的"实战补充"：用最小可复现的片段 + `go build -gcflags` 输出，解释常见逃逸场景，并给出诊断工具清单。基础概念（栈/堆、指针逃逸、`interface{}` 装箱）见主文档。

## TL;DR

- **逃逸分析 = 编译器决定"变量在栈还是堆"**：逃逸到堆才会被 GC 跟踪，分配和回收都更贵。
- **典型 case**：循环变量闭包、切片/map 扩容、`interface{}` 装箱、大结构体传值 vs 传指针、`for range` 变量（Go 1.22 之前）。
- **诊断工具**：`go build -gcflags="-m"` 看逃逸；`go build -gcflags="-m -m"` 看更细的；`go test -bench` + `benchstat` 对比；`pprof` 抓 heap/allocs。
- **核心心法**：少一次堆分配 = 少一次 GC 压力；但不要为了"逃逸对"牺牲代码可读性。

## 案例 1：循环变量被闭包捕获（Go 1.22 之前）

Go 1.22 之前，`for` 循环里的 `v` 在每次迭代中是**同一个变量**（地址相同），所以闭包捕获的是同一个 `v`：

```go
package main

import "fmt"

func main() {
    var fs []func()
    for i := 0; i < 3; i++ {
        fs = append(fs, func() { fmt.Println(i) })
    }
    for _, f := range fs {
        f()
    }
}
```

- Go 1.21 及之前：打印 `3 3 3`（典型 bug）。
- Go 1.22+：每次迭代 `i` 重新声明，打印 `0 1 2`（修复）。

**逃逸分析视角**：

```bash
$ go build -gcflags="-m" main.go
./main.go:6:6: moved to heap: i
```

`i` 被闭包捕获、且闭包可能比 `for` 活得久，编译器把 `i` 放到堆上（无论 Go 1.21 还是 1.22+，闭包外引用都会逃逸）。Go 1.22+ 的修复是**语义层面**的，不是把 `i` 留在栈上。

> **修法**：Go 1.22 之前的标准做法是循环里加一行 `v := i`（或 Go 1.22+ 直接升级即可）。

## 案例 2：切片/map 扩容导致指针逃逸

```go
package main

func consume(s []int) { _ = s }

func f() {
    var buf []int
    for i := 0; i < 1000; i++ {
        buf = append(buf, i) // 多次扩容
        consume(buf)
    }
}
```

```bash
$ go build -gcflags="-m -l" main.go
./main.go:6:11: buf escapes to heap:
./main.go:6:11:    flow: ~r0 = &buf:
./main.go:6:11:    from: return buf (interface-conversion) at ./main.go:6:11
```

> **关键观察**：`[]int` 本身是值类型，但 `append` 在扩容时会**重新分配底层数组**，而扩容时编译器无法证明新底层数组不会被外部引用（因为 `consume` 接收的是 `[]int` 形参，可能持有引用），所以 `buf` 逃逸。

**修法**：

- 在 `f()` 内部把 `consume` 直接消化掉，别传出切片。
- 或者预分配容量：`buf := make([]int, 0, 1000)`，让 `append` 一次到位（但 `buf` 是否逃逸还取决于是否被外部引用）。

## 案例 3：`interface{}` 装箱

```go
package main

type I interface{ Foo() }

type T struct{}

func (T) Foo() {}

func call(v I) { v.Foo() }

func main() {
    var t T
    call(t) // 装箱：T -> I
}
```

```bash
$ go build -gcflags="-m" main.go
./main.go:9:13: t escapes to heap
```

> **关键观察**：接口装箱时，runtime 需要一个"指向具体值的指针"塞进 `eface.data`；如果原值是值类型（不在堆上），就必须先把它放到堆上再取址。这就是 `interface{}` 参数容易触发逃逸的根因。

**修法**：

- 热点路径尽量用具名接口（`type Reader interface{...}`）或泛型（Go 1.18+）代替 `interface{}`。
- 不要把"值类型塞进 `fmt.Println`"当性能问题——`fmt` 没办法，序列化/格式化场景用泛型/具体类型自己实现。

## 案例 4：大结构体传值 vs 传指针

```go
package main

type Big struct {
    a, b, c [1024]byte
}

func byVal(s Big) Big  { return s } // 3072B 拷贝
func byPtr(s *Big) *Big { return s } // 8B（指针）
```

直觉上"传指针更高效"——但要看场景：

- **小结构体（≤3 个 word）**：传值更快，避免一次堆分配和 GC 跟踪。
- **大结构体（> 几 KB）**：传指针更快，避免栈拷贝（虽然栈拷贝本身不贵，但栈会扩）。
- **逃逸视角**：
  - `byVal` 通常**不逃逸**（`s` 在栈上，函数返回时自然销毁）。
  - `byPtr` **逃逸**（`s` 在堆上，因为指针可能比函数活得久）。

```bash
$ go build -gcflags="-m" main.go
./main.go:6:18: s does not escape        # byVal
./main.go:7:19: s escapes to heap        # byPtr（参数被外层引用）
```

> **反直觉点**："少一次堆分配"并不意味着传值一定更好。要看结构体大小 + 是否会被外部引用。

## 案例 5：`for range` 变量语义（Go 1.22 之前/之后）

见 **案例 1**。Go 1.22 之前 `for _, v := range xs`，`v` 是同一个变量，闭包捕获会出错；Go 1.22+ 每次迭代新变量。

**与逃逸的关系**：

- Go 1.22 之前，**如果 `v` 被闭包捕获且闭包外传**，`v` 会逃逸到堆。
- Go 1.22+ 同样会逃逸（语义修了，逃逸不变）。

```bash
# Go 1.21：v 逃逸
./main.go:3:14: moved to heap: v
```

## 案例 6：性能优化——少一次堆分配的收益

一个真实场景：HTTP 框架里复用大结构体，传入传出都按值。

```go
// 优化前：每次 request 一个新对象，逃逸到堆
func handle(req *Request) *Response {
    r := &Response{} // 逃逸
    r.Status = 200
    return r
}

// 优化后：sync.Pool 复用
var pool = sync.Pool{
    New: func() any { return &Response{} },
}

func handle(req *Request) *Response {
    r := pool.Get().(*Response)
    r.Reset()
    r.Status = 200
    return r
}
```

`sync.Pool` 把"频繁创建/销毁的临时对象"复用掉，对**高 QPS 服务**的 GC 压力降低非常明显（参考 Go 标准库 `fmt` 包用 `pp` 池化 buffer 的做法）。

> **副作用警告**：
>
> 1. `sync.Pool` 里的对象可能被 GC 清空（**无内存保证**），不能当缓存用。
> 2. 还回去之前必须 `Reset`，否则可能泄漏上一轮的引用导致数据竞争。
> 3. 不要为了"压 GC"过度使用 `Pool`——大多数应用 GC 根本不是瓶颈。

## 诊断工具与命令

### 1. 逃逸分析输出

```bash
# 基本：打印逃逸决策
go build -gcflags="-m" main.go

# 更详细：-m -m
go build -gcflags="-m -m" main.go

# 关闭内联，让"某个函数到底逃不逃"清晰可见
go build -gcflags="-m -l" main.go

# 更细的调试模式（一般用不上，输出很啰嗦）
go build -gcflags="-d=escape" main.go
```

**典型输出**：

```text
./main.go:6:11: moved to heap: u          # 变量逃逸到堆
./main.go:9:13: ... argument does not escape  # 实参没逃逸（不影响调用者）
./main.go:11:2: (... escapes)             # 实参逃逸
```

读 `flow:` 那几行尤其有用：它告诉你**为什么**编译器认为变量逃逸（通过哪个赋值、通过哪个 return 传出）。

### 2. 基准测试 + benchstat

```bash
# 写 benchmark
func BenchmarkXxx(b *testing.B) {
    for i := 0; i < b.N; i++ {
        _ = target()
    }
}

# 跑 benchmark（带内存分配统计）
go test -bench=BenchmarkXxx -benchmem ./...

# 对比两次修改
go test -bench=. -count=10 -benchmem > old.txt
# (改代码)
go test -bench=. -count=10 -benchmem > new.txt
benchstat old.txt new.txt
```

> `benchstat` 是 `golang.org/x/perf/cmd/benchstat` 工具，专门做带统计显著性的对比。**只看一次 -bench 没有意义**——用 `-count=10` + `benchstat` 才能确认不是噪声。

### 3. pprof：堆与分配

```bash
# 启动时开启 pprof
import _ "net/http/pprof"
go func() { log.Println(http.ListenAndServe("localhost:6060", nil)) }()
```

```bash
# 抓 heap（当前在堆里还活着的对象）
go tool pprof http://localhost:6060/debug/pprof/heap

# 抓 allocs（自启动以来所有分配，包括已 GC 的）
go tool pprof http://localhost:6060/debug/pprof/allocs

# 在 pprof 里：
#   top          - 列出分配最大的几个调用栈
#   list Func    - 看某个函数逐行的分配
#   web          - 可视化调用图
```

**反例场景**：你看到 `allocs` 里某个函数在每次调用都分配 1KB——很可能就是逃逸。先用 `-gcflags="-m"` 验证"的确逃逸了"，再去改。

### 4. 实验性：手动打开 / 关闭内联

逃逸分析本身没法"关掉"，但可以用编译指令控制函数的内联/栈行为，从而定位**逃逸边界**：

```go
//go:noinline
func hotPath() *T { return &T{} } // 关闭内联看是否逃逸
```

把函数标 `//go:noinline` 之后再看 `-m` 输出，能更清楚"这个变量到底是在哪一步逃逸的"（内联有时会把逃逸源头"吞掉"，分不清是谁的锅）。

### 5. 速查清单（诊断 → 行动）

| 看到什么                           | 可能原因                   | 行动                                     |
| :--------------------------------- | :------------------------- | :--------------------------------------- |
| `moved to heap: v`                 | 闭包 / 接口装箱 / 切片扩容 | 看具体行；判断是否可改值传递             |
| `escape: param`                    | 形参通过返回值/接口外传    | 看调用点是否能改为内部消化               |
| `does not escape`                  | 栈分配                     | 不需要管                                 |
| `moved to heap: i` 在循环里        | 循环变量被闭包捕获         | Go 1.22 升级 / 加 `v := v`               |
| pprof 看到大块重复分配             | 热点路径在堆上             | 考虑 sync.Pool / 复用 / 改值传递         |
| pprof 显示 goroutine 大量阻塞在 FD | 慢连接堆积                 | 检查 ReadTimeout / WriteTimeout / 限并发 |

## 心法：什么时候值得"为逃逸而优化"

> **不要过早优化**。很多 Go 程序的瓶颈在锁、I/O、序列化，**不在** GC。

值得做的场景：

- **热点路径**：profile 显示某个函数每秒调用上百万次，每次分配 16 字节——这种"小而频繁"的堆分配是 GC 主要压力源。
- **大量短命对象**：每秒分配 1GB、立刻 GC 掉的临时对象。
- **接口装箱出现在热路径**：能用泛型/具名接口替掉就替。

不值得做的场景：

- **非热点路径**：每秒 100 次的分配，根本不在 GC 视野里。
- **为了"少一次分配"破坏代码可读性**。
- **没有 profile 数据的"凭感觉优化"**。

## 相关链接

- [`escape_analysis.md`](escape_analysis.md)：基础概念（栈/堆、指针逃逸、`interface{}` 装箱等）。
- [`../runtime/gc.md`](../runtime/gc.md)：分配的对象由 GC 跟踪；少一次堆分配 = 少给 GC 添负担。
- [`../runtime/memory_allocator.md`](../runtime/memory_allocator.md)：小对象走 mcache，大对象直接走 mheap。
- [`../runtime/scheduler.md`](../runtime/scheduler.md)：goroutine 栈的扩缩机制（逃逸发生在栈上放不下的时候）。
- [`../runtime/netpoll.md`](../runtime/netpoll.md)：网络 I/O 中 `FD` / `pollDesc` 也是堆对象，复用思路同此。
