# 所有权 / 借用 / 生命周期（Go 视角）

> 假设你已经会一门带 GC 的语言，这里只记"换了心智模型之后真正会踩的地方"。
> 例子取自 flowcraft 的 `rust/`，都是真实代码里出现过的形状。

## TL;DR

- **move 就是 memcpy + 把源标记为不可用**。没有隐藏的深拷贝，也没有隐藏的引用计数；`Rc`/`Arc` 是显式选择的。
- **`&mut` 的核心语义是"排他"，不是"可变"**。同一时刻只能有一个活着的大 `&mut`。但从一个 `&mut self` 里同时借**不同字段**是允许的（字段投影不相交）；不允许的是借用区间重叠。
- **生命周期标注几乎都用于"返回值借用参数"**。函数内部不需要标注；`&self` 的方法默认返回借用 `self`。
- **`Drop` 是 RAII 的落点**：未提交的状态在 drop 时自动回滚，比"记得 catch 里清理"可靠得多。
- **`Copy` 不是性能优化，是语义声明**。给类型加 `Copy` 等于承诺"复制它永远正确"，大结构体照抄反而更慢。

## 1. 三种"传递"语义

| 形式 | 语义 | 典型代价 |
| :--- | :--- | :--- |
| `T` | move（所有权转移） | 一次 memcpy，源失效 |
| `&T` | 共享借用（可同时多个） | 零成本 |
| `&mut T` | 排他借用（同时只有一个） | 零成本 |

```rust
fn take(s: String) -> usize { s.len() }      // 拿走所有权，调用后 s 不能用

let s = String::from("hi");
let n = take(s);                              // move
// println!("{s}");                           // 编译错误：value borrowed after move
```

- 借用检查器在**编译期**保证：要么多个 `&T`，要么一个 `&mut T`。
- 因此"先拿 `&mut` 再在里面读同一个值"这类代码需要通过**重新借用（reborrow）**或改变数据流来写，而不是加锁。

## 2. 拆借：为什么"借两个字段"是合法的

```rust
struct Pair { a: Vec<u8>, b: Vec<u8> }

fn split(p: &mut Pair) -> (&mut Vec<u8>, &mut Vec<u8>) {
    (&mut p.a, &mut p.b)        // 合法：字段是互不重叠的借用
}

// 不行的只有借用区间重叠，或者同一个 &mut 上两次调用只返回一个字段的视图：
// fn first(&mut self) -> &mut Vec<u8>; fn second(&mut self) -> &mut Vec<u8>;
// let (a, b) = (p.first(), p.second());   // 两次 &mut self 同时活着 → 拒绝
```

实践结论：**借用冲突往往在提示你拆结构或拆函数**，而不是提示你上 `RefCell`。

## 3. 生命周期：只在三个地方要写

- 返回引用时：`fn name(&self) -> &str`（省略规则自动绑定到 `&self`）。
- 多参数且返回借用其中一个：必须显式 `<'a>`。
- 结构体里存引用：必须显式（这也是为什么库代码里更多用拥有型字段）。

> 常见误解："生命周期是让编译器知道值活多久"。反了——**生命周期是你在描述"这些引用之间的关系"**，值的存活与否由所有权决定。

## 4. Drop：把"清理"变成类型的一部分

```rust
impl Drop for Reservation<'_> {
    fn drop(&mut self) {
        if !self.armed { return; }               // 已提交，不回滚
        if let Ok(mut entries) = self.registry.entries.lock()
            && entries.get(&self.id).is_some_and(|e| e.generation() == self.generation)
        {
            entries.remove(&self.id);            // 只回收"自己那一代"
        }
        self.registry.settle();
    }
}
```

这是"占位 → 提交 → 自动回滚"的标准形状：

- 创建时 `armed = true`（占位生效）；
- 正常路径显式 `disarm()`（状态已交给别的路径）；
- 任何提前返回、`?` 失败、panic 展开，都会走 `Drop`，把占位撤掉。

> 两个细节：**drop 里不 panic**（panic 中再 panic 会 abort）；drop 里做 I/O 要么容错，要么在注释里写明为什么允许。

## 5. 常见坑

- `Copy` 加在大结构体上：每次传参都在拷贝，热点路径上比 `&T` 慢一个量级。
- 为了"少写 clone"而到处传 `&mut`：最后借不出来，改成"函数只返回结果"。
- 认为 `&str` 一定比 `String` 好：跨作用域返回就得 `String`，别为了省一次分配把签名搞复杂。
- 用 `mem::take` / `mem::replace` 取走字段值：在"需要把字段移出去但不能 move 整个 self"时是正解，但也容易掩盖设计问题。

## 6. 清单

- [ ] 参数优先 `&T`，需要"拿走"时才传 `T`
- [ ] 借用冲突先想拆结构 / 拆函数，而不是 `RefCell`
- [ ] 生命周期标注只出现在"返回值借用参数"或"结构体存引用"
- [ ] 需要回滚的状态用 `Drop` 表达，不做"记得清理"
- [ ] `Copy` 只给"位模式可复制的小类型"

## 相关链接

- [trait / 泛型 / 动态分发](traits.md)（泛型与动态分发；借用规则在 trait 上的表现）
- [内部可变性：Cell / RefCell / OnceLock / Mutex 家族](interior_mutability.md)（借用检查过不去时的正经出口）
- [内存布局与分配](../runtime/memory.md)（move、Box、Vec 在内存里到底发生了什么）
- [有界容器](../engineering/bounds.md)（`Reservation` 这类 RAII 守卫在有界容器里的用法）
