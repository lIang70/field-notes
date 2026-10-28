# 内部可变性：Cell / RefCell / OnceLock / Mutex 家族

> 借用检查器说"不能同时有 `&mut`"的时候，正解通常不是加锁，而是选对"可变性的位置"。
> 例子全部来自 flowcraft 的 `rust/`（六万行、多线程、`Rc` 出现 0 次）。

## TL;DR

- **先问"这份可变性要不要跨线程"**：不跨 → `Cell` / `RefCell`；跨 → `Mutex` / `RwLock` / 原子。选错的代价是编译错误（好消息）或运行时 panic（坏消息）。
- **`OnceLock` 解决"只初始化一次"**：全局注册表、惰性加载的工具定义都用它，比 `lazy_static!` 少一层宏、少一个依赖。
- **`RefCell` 的借用检查搬到运行时**：同时两个 `&mut` 不是编译错误，是 **panic**。在 async 里持有 `RefCell` 借用跨过 `.await` 是最常见的翻车点。
- **`Arc<Mutex<T>>` 不是默认答案**：先想"能不能 `Arc<T>` 只读共享 + 原子变量做增量"，锁只在必须复合更新时上。
- **`Cell`/`RefCell` 是 `!Sync` 的**——它们的存在本身就说明"这份状态只属于一个线程"。

## 1. 选型表

| 需求 | 选择 | 真实用法 |
| :--- | :--- | :--- |
| 单线程、可复制的值 | `Cell<T>` | 计数器、开关（`get`/`set` 不需要借用） |
| 单线程、需要引用 | `RefCell<T>` | 图结构、临时可变视图 |
| 多线程、复合更新 | `Mutex<T>` | 会话注册表 `Mutex<BTreeMap<String, Entry>>` |
| 多线程、读多写少 | `RwLock<T>` | 后端表、工具注册表 |
| 多线程、无锁单一值 | `AtomicU64` / `AtomicBool` | 世代号、启动标志 |
| 只初始化一次 | `OnceLock<T>` | 进程级后端缓存、惰性工具定义 |

## 2. `OnceLock`：一次初始化，之后只读

```rust
static BACKENDS: OnceLock<Mutex<BTreeMap<StoreKey, Arc<FileBackend>>>> = OnceLock::new();

let loaded: OnceLock<Arc<dyn Tool>>;   // 惰性加载：第一次读时填上，之后一直共享
```

- 适合"进程级、只写一次、之后并发只读"的东西：注册表、解析后的配置、编译期不变的表。
- 比 `lazy_static!` / `once_cell` 好的地方：标准库、无依赖、`get_or_init` 语义清晰。
- 注意**先 `OnceLock` 还是先 `Mutex`**：上面那行是"整张表只建一次，表内部仍要并发改"；如果你要的是"每个键只建一次"，那应该是 `Mutex<BTreeMap<K, OnceLock<V>>>`。

## 3. `RefCell` 的运行时借用

```rust
let cell = RefCell::new(vec![1, 2, 3]);
let a = cell.borrow();        // 共享借用
// let b = cell.borrow_mut(); // 运行时 panic：already borrowed
```

- 编译期错误变成运行时 panic，所以**只在你能证明"借用很短、不会重入"时用**。
- 在 async 代码里尤其危险：`let g = cell.borrow_mut();` 之后 `.await`，别的任务再借就 panic——而且 panic 位置离根因很远。
- 需要"跨 await 的可变访问"时，用 `tokio::sync::Mutex`（见 [tokio 实战：watch / broadcast / 世代切换 / 锁的选择](../runtime/tokio.md) §4），别用 `RefCell`。

## 4. `Mutex` / `RwLock` 的边界

- **锁的粒度按"一次复合更新"来定**：注册表要"检查 + 插入 + 通知"是原子语义，那就一把锁；只是读一个计数就用原子。
- **毒化（poisoning）要映射**：`lock()` 返回 `Err` 表示上个持锁者 panic 了；映射成分类型错误，别 `unwrap`（workspace 级 `unwrap_used = "deny"` 也不允许）。
- **不要为了"省一个 `Arc`"而在锁里做 I/O**：持锁期间做阻塞操作 = 把锁变成全局串行点。
- **注入时钟**这类回调存成 `Arc<dyn Fn() -> SystemTime + Send + Sync>`：共享 + 可替换 + 可测试，比全局静态变量干净。

## 5. `Send` / `Sync`：两条自动推导的约束

```text
T: Send   可以把 T 移到另一个线程
T: Sync   &T 可以跨线程共享
```

- `Rc`：`!Send` 且 `!Sync`；`Cell<T>` / `RefCell<T>`：**只是 `!Sync`**——`T: Send` 时整个值可以搬到别的线程，只是不能并发共享（所以 `Mutex<Rc<T>>` 不行，`Mutex<Cell<T>>` 可以）。这个 crate 里 `Rc` 出现 0 次，是编译期筛出来的结果。
- `Arc<T>: Send + Sync` 要求 `T: Send + Sync`；`Mutex<T>: Sync` 要求 `T: Send`（所以 `Mutex<Rc<_>>` 仍然不能跨线程）。
- `unsafe impl Send/Sync` 是承诺，不是开关：写之前先问"我能不能列出所有并发访问路径"。

## 6. 常见坑

- 用 `RefCell` 绕过借用检查，然后在另一个函数里重入 → 运行时 panic，且堆栈指向中间的 `borrow`，不指向设计错误。
- `Mutex<Vec<T>>` 里 `push` 一个可能失败的 future，持锁 await → 死锁或长尾延迟。
- 静态 `OnceLock` 里初始化时又依赖另一个静态（初始化顺序问题）——把依赖显式传参。
- 原子变量做"复合不变量"（两个计数器要保持比例）——那需要锁，不需要两个原子。

## 7. 清单

- [ ] 先判定"跨不跨线程"，再选 `Cell/RefCell` 还是 `Mutex/RwLock/原子`
- [ ] 只初始化一次的东西用 `OnceLock`
- [ ] `RefCell` 借用不跨 `.await`，也不跨可能重入的调用
- [ ] 锁的临界区里没有 I/O、没有 `.await`
- [ ] 毒化有映射，不 `unwrap`
- [ ] `unsafe impl Send/Sync` 需要有注释列出并发路径

## 相关链接

- [所有权 / 借用 / 生命周期](ownership.md)（借用检查为什么会把状态推向内部可变性）
- [tokio 实战：watch / broadcast / 世代切换 / 锁的选择](../runtime/tokio.md)（跨 `await` 的锁怎么选、`watch` 为什么常比锁好用）
- [trait / 泛型 / 动态分发](traits.md)（`Arc<dyn Trait>` 的共享形态）
- [有界容器](../engineering/bounds.md)（被锁保护的那些表为什么都要有上限）
