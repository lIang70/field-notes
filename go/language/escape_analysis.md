# Escape Analysis (逃逸分析)

逃逸分析是 Go 编译器在编译阶段进行的一项分析，用于决定变量应该分配在 **栈 (Stack)** 上还是 **堆 (Heap)** 上。

## 核心原则

- **栈 (Stack)**: 分配和回收极快（指针移动），不需要 GC 参与。函数返回时自动销毁。
- **堆 (Heap)**: 需要 GC 扫描和回收，开销较大。

**编译器的目标**: 尽可能将变量分配在栈上，只有当变量在函数返回后仍被引用（“逃逸”出函数作用域），才分配到堆上。

## 常见逃逸场景

### 1. 指针逃逸

函数返回局部变量的指针。

```go
func create() *User {
    u := User{Name: "test"}
    return &u // u 逃逸到堆上，因为返回值在外部被引用
}
```

### 2. 接口类型逃逸 (Boxing)

接口值本身是 16 字节的 `(type, data)`；逃逸的根因不是"动态派发"，而是**装箱**：具体值类型要放进 `data` 就得有可寻址的存储，值被外传时只能上堆。

```go
func print(v interface{}) {
    fmt.Println(v) // v 已经是接口值，这里不再"装箱"；它本身逃逸是因为被传到别的函数
}

func box(t T) { var i interface{} = t; sink(i) } // 这里才是一次真正的装箱
```

> 判据看调用点：能内联/去虚拟化时 `-m` 会报 `does not escape`；跨包、存进容器或全局时才会看到 `escapes to heap`。

### 3. 栈变量上限与"外传"

goroutine 初始栈是 2KB，但栈会长；**决定逃逸的不是 2KB**，而是两件事：变量是否被外传（返回、存进全局/接口/闭包），以及是否超过编译器的栈变量上限（`maxStackVarSize` 10MB、`maxImplicitStackVarSize` 64KB）。

```go
func huge() {
    s := make([]int, 10000) // 80KB：s 不外传就仍可能在栈上
    _ = s
}

func leak() []int {
    s := make([]int, 10000); return s // 外传 → 堆
}
```

### 4. 闭包引用

闭包中引用的外部变量，如果闭包生命周期超过了函数，变量会逃逸。

### 5. 变量大小不确定

```go
func makeSlice(n int) {
    s := make([]int, n) // 长度 n 不确定，逃逸到堆
}
```

## 分析工具

使用编译参数查看逃逸分析结果：

```bash
# -m: 打印优化决策 (包括逃逸分析)
# -l: 禁用内联 (可选，为了看清特定函数的逃逸情况)
go build -gcflags="-m -l" main.go
```

**输出示例**:

```text
./main.go:5:6: moved to heap: u
./main.go:9:13: ... argument does not escape
```

## 优化建议

1. **多传值，少传指针**: 对于小对象（如基本类型、小结构体），传值复制的开销往往小于 GC 的开销。
2. **预分配**: `make` 切片或 Map 时，如果长度确定，尽量使用常量，避免动态扩容和大小不确定导致的逃逸。
3. **小心 interface{}**: 热点代码路径尽量避免使用 `interface{}`。
