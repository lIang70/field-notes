# 内存布局与分配（Vec / Box / Arc 的真实成本）

> 讲"值放在哪、复制了什么、什么时候分配"。所有权与借用的语义在 [所有权 / 借用 / 生命周期](../language/ownership.md)，这里只看内存。
> 用法与数字取自 flowcraft 的 `rust/`（六万行、多线程、`Rc` 出现 0 次）。

## TL;DR

- **move 只是搬指针**：`Vec<T>` / `String` 的 move 搬 24 字节，堆上的缓冲原地不动；真正的复制只有你显式 `clone` 的那一次。
- **尺寸不由字段和决定**：`repr(Rust)` 允许编译器重排字段，`size_of` 经常比"把字段加起来"小；要稳定布局（FFI、mmap、wire）就得上 `#[repr(C)]`。
- **`Vec` 的增长是"按倍数 + 整体搬迁"**：均摊 O(1)；需要搬移时瞬时峰值 ≈ 3 倍旧容量（= 1.5 倍新容量），原地扩展则没有。容量**不会自动回落**。
- **`Arc::clone` 是一次原子加**，多线程高频 clone 同一个 `Arc` 会争同一缓存行；共享结构适合 `Arc`，逐元素复制不适合。
- **`Drop(Arc)` 只自减 strong**：strong 归零才 drop `T`、再自减 weak，最后一个 weak 才释放控制块——所以析构不是"一次原子减"。
- **Niche 能省一半内存**：`Option<Box<T>>` 与 `Box<T>` 同宽（用空指针表示 `None`），`Option<u64>` 却要 16 字节。
- **分配次数比分配大小更能预测性能**——`Vec` 少一次 realloc 往往比每个元素少 8 字节更值钱。

## 1. 一个值的成本

| 类型                                | `size_of` | 说明                                              |
| :---------------------------------- | :-------- | :------------------------------------------------ |
| `u64` / `f64`                       | 8         | 对齐 8                                            |
| `bool`                              | 1         | 但 `Option<bool>` 是 2（没有 niche）              |
| `&T` / `Box<T>`                     | 8         | 裸指针                                            |
| `&[T]` / `&str` / `Box<dyn Trait>`  | 16        | **胖指针**：数据指针 + (长度 \| 虚表指针)         |
| `Vec<T>` / `String`                 | 24        | `(ptr, len, cap)`；`String` 是 `Vec<u8>` 的薄包装 |
| `Arc<T>` / `Rc<T>`                  | 8         | 指向"控制块 + T"的一次分配                        |
| `Option<Box<T>>`                    | 8         | niche：`None` = 空指针                            |
| `Option<NonNull<T>>` / `Option<&T>` | 8         | 同上                                              |
| `Option<u64>`                       | 16        | 没有 niche，得加一个 tag 字节再对齐               |

```rust
// 断言尺寸比读文档可靠——仓库里就有这种测试
assert!(std::mem::size_of::<Error>() <= 64);
assert!(std::mem::size_of::<Result<(), Error>>() <= 64);
```

> 这类"类型大小预算"的断言很值：`Error` 从 64 涨到 72 字节时，所有 `Result<T>` 的返回值宽度都会变，而没人会去 review 这个。

## 2. Vec 与 String 的增长

```text
push → 容量不足 → 分配 cap*2（至少满足本次）→ memcpy 旧元素 → 释放旧缓冲
```

- 均摊 O(1)，但**峰值内存 = 旧缓冲 + 新缓冲**，在大容器上就是 1.5–2 倍的瞬时占用。
- 容量**不会自动回落**：`drain`、`clear`、`truncate` 都保留 `cap`；要还就 `shrink_to_fit()` 或 `into_boxed_slice()`。**什么时候该还**：长期持有且不再增长的容器（缓存、连接池）；热路径上反复复用的缓冲恰恰要保留容量（见 [分配器与分配策略](allocator.md) §7）。
- 已知长度就 `with_capacity`——这不是微优化，而是"读文件/解析分片"这类路径上的标准动作（仓库里十几处 `Vec::with_capacity(src.len())` 都是这个形状）。
- 反向的坑：给大量小 `Vec` 都拍一个 `with_capacity(4096)`，峰值内存直接乘上去。

```rust
// 已知长度：一次分配
let mut out = Vec::with_capacity(input.len() + 16);
// 已经不再变化：把多余容量还掉（`Box<[T]>` 顺便变成瘦指针）
let boxed: Box<[u8]> = out.into_boxed_slice();
```

`String` 与 `Vec<u8>` 同构（UTF-8 不变式由类型保证），所以 `push_str` 同样会 realloc；拼长字符串用 `String::with_capacity` 或 `write!` 到预分配缓冲。

## 3. Arc / Rc 的真实成本

```text
Arc::new(T) → 一次分配：[ strong | weak | T ]（控制块与 T 连续）
Arc::clone   → 原子自增 strong
Drop(Arc)    → 自减 strong；归零时 drop T、再自减 weak；最后一个 weak 才释放
```

- **计数在同一块分配里**：所以 `Arc<T>` 本身只是 8 字节指针，但每个 `Arc::clone` 都要动同一缓存行——多线程高频 clone 就是伪共享式的争用。
- **适合 Arc 的**：handler、注册表、配置、`Arc<dyn Trait>` 能力边界（这个仓库 366 处基本都是这个用途）。
- **不适合的**：循环里 `clone` 再 `push`——那是在给每个元素买一次原子操作。
- **COW 出口**：`Arc::make_mut` / `Arc::get_mut`（以及 `Cow<'_, T>`）可以在"只有我一个人持有"时退化成独占修改，省掉一次拷贝。
- **循环引用会泄漏**：`Weak` 用来打断环，或者让"缓存不阻止对象回收"（仓库里 `Weak` 只出现两处，都是这个理由）。

## 4. 共享只读负载：`Arc<[u8]>`

```rust
struct Record {
    data: Arc<[u8]>,   // 定长、不可变、clone 只加一次引用
}
```

- 从 `Vec<u8>` 转 `Arc<[u8]>` 会**精确拷贝一次并缩到实际长度**，之后克隆只加计数——适合"一条输出/一个事件被多个读者共享"。
- 代价是**写入必须重建**（不可变）；需要增量写就用 `Vec<u8>` / `VecDeque<u8>` 自管缓冲，读完再冻结成 `Arc<[u8]>`。
- 这是"读多写少"的通用形状：环形日志、事件流、快照。

## 5. Drop 的顺序与"部分移动"

```rust
struct Session { log: Log, child: Child, journal: Journal }
// 字段按声明顺序 drop：log → child → journal；局部变量按声明逆序
```

- 需要"先收尾 A 再释放 B"就把它写成字段顺序或 `Drop` 实现，**不要依赖作用域恰好在那之前结束**。
- 有 `impl Drop` 的类型**禁止部分移动**（不能把字段逐个移走）——这层限制反而是保护。
- 想"从 `&mut self` 里取走一个字段"，正解是 `std::mem::take` / `mem::replace`（留一个默认值），而不是 `unsafe`。
- 需要延后/跳过 drop：`ManuallyDrop`，但要把"谁负责最终释放"写在注释里。
- **Rust 没有 async drop**：要异步收尾的东西必须显式 `close().await`，不能指望 `Drop`。

## 6. 布局控制：什么时候必须 `repr`

- `repr(Rust)`（默认）：编译器**可以重排字段**降 padding，所以 `size_of` 常小于字段之和；跨版本不保证稳定。
- `#[repr(C)]`：字段顺序与 C 一致。用途：FFI、`mmap`/wire 结构、`unsafe` 里按字节解释的内存。
- `#[repr(align(N))]`：抬高对齐（比如按缓存行隔离热字段），代价是尺寸变大。
- **ZST（零大小类型）**：`Vec<()>` 不分配元素内存（`cap == usize::MAX`），适合"只记长度"的场景。

padding 与伪共享的两个具体形状：

```rust
struct A { a: u8, b: u64 }        // size_of = 16（b 前有 7 字节 padding）
struct B { b: u64, a: u8 }        // size_of = 16，同样 7 字节，只是跑到尾部
struct C { a: u8, b: u32, c: u8 } // 编译器重排后 8 字节；写成 #[repr(C)] 就是 12

// 两个高频原子挤在同一缓存行：一个线程自增 a，另一个自增 b，互相打掉对方的行
struct Counters { a: AtomicU64, b: AtomicU64 }        // 同一行，伪共享
#[repr(align(64))] struct Padded(AtomicU64);          // 各占一行，代价是内存变大
```

- 规则：**默认布局下别用"字段相加"推尺寸**；要稳定（FFI / mmap / wire）才上 `repr(C)`，要知道确切尺寸就断言。
- 生成代码里的枚举通常是 `#[repr(i32)]`（proto 要求）；业务类型不要随手加 `repr`，加了就是给自己上约束。

## 7. 怎么测

| 想确认的事       | 测法                                                   |
| :--------------- | :----------------------------------------------------- |
| 类型尺寸没膨胀   | `assert!(size_of::<T>() <= N)`                         |
| 没有意外 realloc | `assert_eq!(v.capacity(), expected)` 或读 `capacity()` |
| 这条路径零分配   | 自定义 global allocator 计数，比计时稳                 |
| 峰值内存         | 进程 RSS / `dhat` / `valgrind --tool=massif`           |
| 吞吐             | criterion 基准，**别用 wall clock 断言**               |

可抄的计数分配器（测试里读 `COUNT` 的前后差值）：

```rust
struct Counting;
static COUNT: AtomicUsize = AtomicUsize::new(0);

unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, l: Layout) -> *mut u8 {
        COUNT.fetch_add(1, Ordering::Relaxed);
        unsafe { System.alloc(l) }
    }
    unsafe fn dealloc(&self, p: *mut u8, l: Layout) { unsafe { System.dealloc(p, l) } }
}
#[global_allocator]
static A: Counting = Counting;
```

> 同 `go/engineering/optimization.md` 的一条经验：**能省内存的检查自己不能分配内存**。Rust 里最容易犯的版本是"在零分配检查里写一句 `format!("{}", n)`"。

## 8. 峰值内存的四个常见来源

1. **容量不回落**：容器清空后仍占着历史峰值容量。
2. **增长瞬间的双份**：realloc 时旧缓冲 + 新缓冲同时存在。
3. **`Arc` 环 / 后台任务未收**：引用计数到不了 0，或 `JoinHandle` 没人持有。
4. **环形缓冲的预算与单块例外**：字节预算管总量，但"单块大于预算时仍然保留"——这条例外必须写进文档，否则排查时无法解释。

## 9. 常见坑

- `Vec::remove(0)`：O(n) 搬元素；要队列就用 `VecDeque`。
- 热路径里 `format!` / `to_string()`：每次都是一次分配。
- 每个请求都 `clone` 一份配置：把配置装进 `Arc` 传引用。
- `HashMap` 默认用 SipHash（抗哈希洪泛但慢）；确信输入可信且热点时再换 `ahash` / `rustc-hash`，换之前先测。
- `iter().cloned().collect::<Vec<_>>()` 只为再 `iter()` 一遍：中间那个 `Vec` 是纯开销。
- 忘了 `shrink_to_fit` 却把容器长期持有（缓存、连接池）。

## 10. 清单

- [ ] 关键类型的 `size_of` 有断言（尤其会进 `Result`/`Option` 的错误类型）
- [ ] 已知长度的容器都 `with_capacity`
- [ ] 长期持有的容器在想清楚后 `shrink_to_fit` / `into_boxed_slice`
- [ ] `Arc` 只在"共享结构"上用，不在循环里 clone
- [ ] 共享只读负载用 `Arc<[u8]>` / `Bytes`，需要写就先用 `Vec`
- [ ] drop 顺序是写出来的，异步收尾走显式 `close()`
- [ ] 零分配与容量用断言测，不用计时
- [ ] 峰值内存的四个来源都过了一遍

## 相关链接

- [所有权 / 借用 / 生命周期](../language/ownership.md)（move / 借用 / `Drop` 的语义层）
- [内部可变性：Cell / RefCell / OnceLock / Mutex 家族](../language/interior_mutability.md)（`Arc<Mutex<T>>` 的组合成本）
- [trait / 泛型 / 动态分发](../language/traits.md)（单态化 vs `dyn` 的体积与速度）
- [tokio 实战：watch / broadcast / 世代切换 / 锁的选择](tokio.md)（`watch`/`broadcast` 的缓冲与环形日志的配合）
- [有界容器](../engineering/bounds.md)（上限、回收、观测三件套）
- [Go Memory Allocator](../../go/runtime/memory_allocator.md)（另一门语言的分配器视角）
