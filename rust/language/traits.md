# trait / 泛型 / 动态分发

> Rust 的抽象只有两条路：**单态化（泛型）** 和 **动态分发（`dyn`）**。选错不会编译失败，只会让二进制膨胀或让热路径变慢。
> 用法统计取自 flowcraft 的 `rust/`（六万行量级）。

## TL;DR

- **`impl Trait` 参数 = 泛型 = 单态化**：每个具体类型生成一份代码，快但会让编译时间与体积涨。
- **`Box<dyn Trait>` / `Arc<dyn Trait>` = 虚表调用**：一份代码、可存不同类型，代价是一次间接跳转 + 不能内联。
- **能力边界用 `dyn`，内部算法用泛型**。这个仓库的实际情况：`Arc` 出现 366 次、`Box<dyn …>` 12 次、`Rc` **0 次**。
- **关联类型 vs 泛型参数**：一个实现只有一个关联类型（`Iterator::Item`），可以有多个泛型实例。
- **泛型不会让运行时变慢，但会让编译时间和二进制变大**；反过来，`dyn` 的慢只在"被调几百万次的小函数"上才测得出。

## 1. 三条抽象路径

```rust
fn parse(s: &str) -> impl Iterator<Item = &str> { s.split(',') }   // 返回位置 impl Trait（单态化）
fn log(sink: &dyn Sink, line: &str) { sink.write(line) }           // 参数 dyn（虚表）
trait Store { fn get(&self, k: &str) -> Option<&str>; }            // trait 定义
```

| 写法 | 分发 | 何时用 |
| :--- | :--- | :--- |
| `fn f(x: impl Trait)` | 静态（单态化） | 内部工具函数，调用点类型有限 |
| `fn f<T: Trait>(x: T)` | 静态 | 同上，需要给关联类型/约束起名时 |
| `&dyn Trait` / `Box<dyn Trait>` | 动态（虚表） | 需要存异构实现、跨模块边界、插件/处理器 |
| `-> impl Trait` | 静态 | 隐藏具体类型（迭代器、Future 最常见） |

## 2. 真实用法分布（能说明取舍）

```text
Arc<…>             366     共享所有权的主力
Arc<dyn Trait>      多处   能力边界：CraftHandler / ViolationSink / EndpointProvider
Box<dyn …>          12     一次性装箱的异构对象
Rc<…>                0     多线程 crate 里根本不该出现
Weak<…>              2     只在需要打断循环引用的地方
```

- **`Arc<dyn Trait>` 是"跨线程 + 异构实现"的标准答案**：能力注册表里存的就是它。
- `Rc` 一次都没出现，是**没得选**而不是证明了不需要：这里每个共享结构都要跨线程，`Rc: !Send` 一被拿进去就编译失败。
- `Weak` 只在两处出现：避免自引用环，以及"缓存不阻止回收"。

## 3. 关联类型 vs 泛型参数

```rust
trait Memory {
    type Snapshot;                       // 一个实现只有一个快照类型
    fn snapshot(&self) -> Self::Snapshot;
}

trait Aggregate<T> { fn add(&mut self, v: T); }   // 可以有多份实现
```

- 用关联类型表达"由实现决定的唯一类型"（`Iterator::Item`）。
- 用泛型参数表达"实现可以对多种类型成立"（`From<T>`、`AsRef<T>`）。
- 选错的症状：调用点要写 `<_ as Trait<_>>` 这类标注，或者必须 `collect::<Vec<_>>()` 才能消歧。

## 4. `dyn` 的两个实际约束

- **对象安全（object safety）**：方法不能有泛型参数、不能返回 `Self`。`async fn` in trait 从 1.75 起可以直接写（AFIT），但这样的 trait **不能 `dyn`**；要 `Box<dyn Trait>` 时才落成返回 `BoxFuture`——所以本仓统一用 `#[async_trait]`，代价是每个调用都过一次装箱。
- **`dyn` 的 trait 默认要求 `'static`**（`Box<dyn Trait>` 里），要带借用就写 `Box<dyn Trait + '_>`。

## 5. 孤儿规则与 blanket impl

- 只能在**本 crate 的类型**上实现**外部 trait**，或在**本 crate 的 trait** 上实现外部类型——否则一堆 crate 会为同一个类型写冲突的实现。
- 想给外部类型加方法：包一层 newtype（`struct Wrapper(ExternalType)`），或者在自己的 trait 上做 blanket impl（`impl<T: Display> MyTrait for T`）。
- blanket impl 的代价：它对**所有** `T` 生效，后来想给某个具体类型特化时会冲突。加之前先想清楚"是不是以后要例外"。

## 6. 清单

- [ ] 跨模块边界 / 需要异构集合 → `Arc<dyn Trait>`，别泛型化到爆炸
- [ ] 内部工具函数 → `impl Trait`，别提前擦除类型
- [ ] trait 里需要 `async fn` → 明确知道它被改写成了 boxed future
- [ ] 关联类型用于"唯一"，泛型参数用于"多实例"
- [ ] blanket impl 前先问"以后要不要给某个具体类型例外"
- [ ] 多线程 crate 里出现 `Rc` 就要停下来看

## 相关链接

- [所有权 / 借用 / 生命周期](ownership.md)（`&mut` 的排他性在 trait 方法上的表现）
- [内部可变性：Cell / RefCell / OnceLock / Mutex 家族](interior_mutability.md)（`Arc` + `Mutex` 的组合为什么是主流）
- [Rust workspace 的形状](../architecture/workspace.md)（crate/feature 划分与"边界用 dyn"的关系）
- [async / blocking 边界](../engineering/async_blocking.md)（`#[async_trait]` 与阻塞调用为什么会打架）
