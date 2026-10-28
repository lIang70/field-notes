# 分配器与分配策略（GlobalAlloc / Layout / arena）

> 内存问题有**三层杠杆**：全局分配器（最粗）、容器与缓冲的形状（最常用）、分配模式（arena / 池 / SoA）。
> 这篇讲清三层的边界和代价。单次分配的成本与 `size_of` 表在 [内存布局与分配](memory.md)。

## TL;DR

- **换分配器是最后一招**，不是第一招。先看"分配次数能不能减"和"缓冲形状对不对"——这两层改动的收益通常大一个量级，风险小得多。
- **`Layout` 是分配契约**：`alloc` 与 `dealloc` 必须用**同一个** `Layout`（size 与 align 都一致），否则是 UB 而不是"可能泄漏"。
- **全局分配器只能有一个**（`#[global_allocator]`），换它会影响整个进程，包括所有依赖——必须 A/B 压测，而不是"换了感觉更快"。
- **Arena/bump 不跑 `Drop`**：放进去的对象在 arena 重置时被整体丢弃，析构函数不会执行。要么只放 POD，要么自己记账手动 drop。
- **"对象池"经常比直接分配慢**——池的查找、并发争用、生命周期错配，往往吃掉它省下的那点 malloc 开销。最便宜的池是"复用同一个 `Vec`，`clear()` 后接着用"。

## 1. 三层杠杆

| 层 | 手段 | 收益 | 风险 |
| :--- | :--- | :--- | :--- |
| 分配模式 | 复用缓冲、批量、SoA、arena | 大（去掉每次调用的分配） | 中（生命周期要重新设计） |
| 容器形状 | `with_capacity`、环形缓冲、`Arc<[u8]>`、`Bytes` | 中大 | 低 |
| 全局分配器 | mimalloc / jemalloc / snmalloc | 中（多线程碎片与尾延迟） | 高（整进程、平台、静态链接） |

> 顺序建议：先测分配次数 → 改模式（\(=\ \)减次数）→ 改容器（\(=\ \)减峰值）→ 最后才评估换分配器。

## 2. `Layout`：分配的契约

```rust
let layout = Layout::new::<u64>();                 // size 8, align 8
let arr = Layout::array::<u32>(1024)?;             // size 4096, align 4
let padded = arr.pad_to_align();                   // 补齐到 align 的整数倍
let raw = unsafe { std::alloc::alloc(layout) };    // 必须判空并处理 OOM
```

- **对齐是硬要求**：`alloc` 返回的指针至少满足 `layout.align()`；用 `Vec<u8>` 当缓冲再 `cast::<T>()` 时，若 `T` 要求 8 字节对齐而缓冲没保证，就是 UB（`#[repr(align(N))]` 或 `Layout` 里显式要）。
- **`dealloc` 必须拿到同一个 `Layout`**：`size` 也对，不只是 `align`。所以"分配时记 size"是自定义分配器里必须做的事。
- `Layout::from_size_align` 失败（size 溢出/align 非 2 的幂）要**返回错误**，不要 `unwrap`。

## 3. `GlobalAlloc` 的铁律

```rust
unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, l: Layout) -> *mut u8 { /* 记录 → 转发 */ }
    unsafe fn dealloc(&self, p: *mut u8, l: Layout) { /* 转发 */ }
}
```

- `unsafe` 实现里**不得 panic、不得分配**（递归进自己），错误只能返回空指针。
- 返回空指针表示失败：调用方（`Vec` 等）会走 OOM 处理（默认 abort），不会静默。
- `realloc` 可以不实现（默认用 alloc+copy+dealloc）；实现时要保持一致的对齐与 size 语义。
- **一个进程只能有一个 `#[global_allocator]`**，且它会影响所有 crate（包括依赖里的 `Vec`）。
- 在 `main` 之前、`static` 初始化期间就会调用：分配器实现不能依赖其它全局状态。
- 计数型分配器（测试里数分配次数）的写法在 [内存布局与分配](memory.md) §7。

## 4. 换全局分配器：什么时候值

**值得试的信号**（先确认这些，再谈换）：

- 长驻服务、多线程、分配/释放**跨线程**频繁 → 系统 malloc 的 arena 竞争或碎片明显。
- 尾延迟（p99/p999）远高于 p50，且 profile 指向分配器内部。
- RSS 随时间单调上涨但活跃对象数稳定（碎片，不是泄漏）。

**代价**（三条都要评估）：

- **二进制体积与启动开销**：jemalloc/mimalloc 都是几 MB 级的静态代码。
- **平台与静态链接**：musl 静态链接、Windows 的 CRT 替换、macOS 的 zone 交互都各有坑。
- **`fork` 与信号安全**：多线程进程 `fork` 后子进程只能调 async-signal-safe 函数，分配器行为要确认。

**怎么评判**：三条曲线一起看——p99 延迟、RSS 峰值、`dhat`/massif 的分配量；只看到"平均更快"不算。

> 这份代码没换：`mimalloc`/`jemalloc`/`bumpalo` 都没引入，用的是系统分配器。它的内存问题是通过**缓冲形状**解决的（见 §6）。

## 5. Arena / bump：适合"请求内短命对象"

```rust
let bump = bumpalo::Bump::new();
let s: &mut str = bump.alloc_str("hello");      // 从大块里切一段，几乎零成本
// …这个请求里解析出的所有节点都从同一块里切…
bump.reset();                                    // 整块回收，O(1)
```

- 适合：一次解析/一次请求产生的**大量短命、同生命周期**对象（AST、临时 DTO、渲染列表）。
- 不适合：跨请求共享、生命周期不一、需要各自 `Drop` 的对象。
- **关键坑**：`Bump::alloc` 返回的 `&mut T` **不会跑 `Drop`**，放了 `String`/`Vec` 就等于泄漏那块堆缓冲。`bumpalo::boxed::Box`、`bumpalo::collections::Vec/String` 这类会注册析构的类型是例外——它们在被 drop 时正常跑析构，所以必须在 `reset` 之前 drop。
- 引用穿透：arena 里的对象拿到的是 `&mut` 借用的切片，生命周期受 arena 约束，跨 async 边界时几乎一定借不过去。

## 6. 这份代码怎么做的（不换分配器，改形状）

- **环形缓冲 + 字节预算**：单块预算（`0` 表示不限），溢出丢最老的整块并**报 gap**，绝不静默留洞；"单块大于预算时仍保留"是有意为之的例外。
- **`Arc<[u8]>`**：一条输出/事件被多个读者共享时，一次精确分配 + 引用计数，克隆不再复制字节。
- **`bytes::Bytes`**：gRPC 客户端侧用它的切片语义（零拷贝切分），只在需要跨层共享字节时引入。
- **`Vec::with_capacity(src.len())`**：从源头长度决定容量，消除增长期的双份峰值。

这四条加起来，把"分配次数"和"峰值内存"都压住了，**不需要换分配器**。

## 7. 固定池 / 对象池：多数时候别做

- 池省的是 `malloc/free`，付出的是：池的元数据、并发同步、生命周期错配（归还时还有引用）、以及"预热不够时的额外分配"。
- 只有在"对象构造成本远大于分配成本"（比如带缓冲区的大结构）时才可能划算。
- 最便宜的池：**复用同一份容器**——`vec.clear()` 保留容量，`String::clear()` 同理；这比重造一个池子稳得多（何时反过来说 `shrink_to_fit`，见 [内存布局与分配](memory.md) §2）。

## 8. 怎么测

| 想确认 | 手段 |
| :--- | :--- |
| 分配次数 / 峰值 | 计数分配器（`GlobalAlloc` + 原子计数）、`dhat` |
| 堆增长时间线 | `valgrind --tool=massif`、进程 RSS 曲线 |
| 碎片 | 活跃对象数稳定但 RSS 涨；对比 `dhat` 的 inuse vs alloc |
| 换分配器收益 | A/B 压测，看 p99 + RSS + 启动时间，不看均值 |
| Arena 泄漏 | 确认 arena 里没有持有堆缓冲的类型（`String`/`Vec`/`Box`） |

## 9. 反模式

- 因为"听说 jemalloc 快"就全局替换，没有 A/B，也没有 p99 数据。
- 在 `GlobalAlloc` 实现里 `println!`（递归）或 `unwrap`（panic 穿越 UB 边界）。
- 用 `Vec<u8>` 当对齐缓冲，直接 `cast` 成 `u64` 切片。
- arena 里塞 `String`，reset 后以为内存还回去了。
- 自建对象池却没有"归还即失效"的约束，导致 use-after-return。
- 把分配器当性能银弹：真正的热点往往是"每轮都重建一遍配置"这种调用次数问题。

## 10. 清单

- [ ] 分配次数已经在 profile/计数里量化过（不是猜）
- [ ] 缓冲能复用（`clear` 保留容量），已知长度就 `with_capacity`
- [ ] 跨层共享字节用 `Arc<[u8]>` / `Bytes`，不深拷贝
- [ ] 自定义 `Layout` 的分配，size 与 align 都被记录
- [ ] 全局分配器若替换：有 A/B 数据、有平台验证、有回滚开关
- [ ] arena 里没有会漏 `Drop` 的类型，或已显式 drop
- [ ] 对象池之前，先试过"复用容器"

## 相关链接

- [内存布局与分配](memory.md)（`size_of` 表、Vec 增长、Arc 计数成本、计数分配器写法）
- [所有权 / 借用 / 生命周期](../language/ownership.md)（`Drop` 与生命周期——arena 跳过的正是它）
- [有界容器](../engineering/bounds.md)（字节预算与"单块例外"这类策略）
- [tokio 实战：watch / broadcast / 世代切换 / 锁的选择](tokio.md)（环形日志与 `watch` 修订号的配合）
- [Memory Management](../../os/memory.md)（页、分配器、碎片的一般原理）
