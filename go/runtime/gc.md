# GC

Go 使用并发, 三色标记清除的垃圾回收器.

- 主要目标
  - 低延迟: STW (Stop-The-World) 时间短
  - 高吞吐量: 不影响程序执行效率
  - 并发执行: 与用户代码同时运行

- 算法类型
  - 并发标记清除 (Concurrent Mark-Sweep)
  - 三色标记法 (Tri-color Marking)
  - 非分代 (Non-generational)
  - 非压缩 (Non-compacting)

## 三色标记法

三色标记法将对象分为三种类型: 白色 (尚未访问的对象), 灰色 (已访问但引用的对象需要检查), 黑色 (已访问且引用对象已经检查).

- GC 开始的时候所有对象都是白色
- 从根对象 (全局变量, 栈对象) 开始检查, 并且标记为灰色
- 然后递归标记所有引用

### 完整的周期

- 清扫终止 (Sweep Termination) - STW
- 标记准备 (Mark Setup) - STW：开写屏障、扫描栈根
- 并发标记 (Concurrent Marking)
- 标记终止 (Mark Termination) - STW
- 并发清除 (Concurrent Sweeping)

### 写屏障

用于在并发标记过程中, 确保新创建的引用不会丢失, 防止存活的对象被回收

```go
// 伪代码：混合写屏障（Go 1.8+）——先着色，再赋值
func writePointer(slot **Object, ptr *Object) {
    if gcPhase == marking {
        shade(*slot) // 槽位里的旧值：可能是删边，必须保活
        shade(ptr)   // 新值：可能是插入边，必须保活
    }
    *slot = ptr
}
```

> 三个容易写反的地方：判据是**槽位所属对象为黑且被引用对象为白**（不是拿新值当黑）；shade 的除了新值还有**旧值**（删边保护）；屏障必须在**赋值之前**执行。

## GC 触发机制

1. 堆大小触发
2. 定时触发
3. 手动触发

### 参数调优

```bash
# 环境变量控制
export GOGC=100        # 默认值，增长100%后触发GC
export GOGC=off        # 完全关闭GC
export GODEBUG=gctrace=1  # 输出GC跟踪信息
```
