# Option / Result 的日常写法

> `Option` 和 `Result` 好用的地方不在类型本身，而在**组合子与 `?` 让"错误分支"变成一条线**。
> 计数取自 flowcraft 的 `rust/`：`let … else` 101 处、`.ok_or_else(..)` 43 处、`.and_then(..)` 43 处、`.map_or(..)` 10 处。

## TL;DR

- **`?` 不只是"提前返回"，它还做一次 `From::from` 转换**：这是"底层错误自动升格为分类错误"的机制，也是"错误类型不匹配"的常见原因。
- **`Option` 表示"可能没有"，`Result` 表示"可能失败"**。两者别混：`Result<Option<T>, E>` 才是"查询失败"与"查不到"的正解。
- **`let … else` 适合"拿不到就早退"**，比 `match` + `return` 少一层缩进，比 `unwrap` 明确。
- **组合子优于嵌套 `match`**：`ok_or_else` / `and_then` / `map_or` / `filter` 把分支摊平成链。
- **库代码该用具体错误类型（`thiserror`），应用入口才用 `anyhow`**——反过来的话，调用方就失去了分类能力（参见 [`../engineering/errors.md`](../engineering/errors.md)）。

## 1. `?` 到底做了什么

```rust
fn read_port(path: &Path) -> Result<u16> {
    let text = std::fs::read_to_string(path)?;   // io::Error → 本函数的错误类型（From）
    let port = text.trim().parse::<u16>()?;      // ParseIntError → 同上
    Ok(port)
}
```

等价于：

```rust
let text = match std::fs::read_to_string(path) {
    Ok(v) => v,
    Err(e) => return Err(From::from(e)),         // ← 这一句是关键
};
```

- 所以"为什么这里编译不过"通常是因为**缺少那个 `From` 实现**（在自己错误类型上加 `#[from]`，或手写一个映射）。
- 想加"上下文"时用 `.map_err(...)` 或 `map_err(|e| Error::validation(...))`，别在 `Display` 里拼句子。

## 2. 五种常见形状

```rust
// 1) 拿不到就早退（最常用，比 match 干净）
let Some(v) = map.get(key) else { return Err(Error::not_found("key")) };

// 2) 把 Option 转成 Result：ok_or 适合常量，ok_or_else 适合要构造错误的情况
let v = map.get(key).ok_or_else(|| Error::not_found(key))?;

// 3) 链式取值
let port = env::var("PORT").ok().and_then(|s| s.parse().ok());

// 4) 二选一取值，带默认
let n = maybe.unwrap_or(0);
let n = maybe.map_or(0, |v| v + 1);          // 默认值 + 变换一步到位

// 5) "查不到"不是错误，但"查询失败"是
fn find(&self, k: &str) -> Result<Option<Entry>>;
```

- **`ok_or_else` 而不是 `ok_or`**：错误构造往往要分配（拼字符串、map 里取字段），`ok_or_else` 只在 `None` 时才执行。
- **`Result<Option<T>, E>`** 比 `Option<Result<T, E>>` 可读：外层失败是流程问题，内层 `None` 是语义问题。

## 3. 组合子 vs `match`

| 场景 | 写法 |
| :--- | :--- |
| 只关心成功值 | `.map(...)` |
| 需要又一次可能失败的步骤 | `.and_then(...)` |
| 把 `Option` 变 `Result` | `.ok_or_else(...)` |
| 出错时换类型/加分类 | `.map_err(...)` |
| 有默认值 | `.unwrap_or(default)` / `.map_or(default, f)` |
| 校验 | `.filter(谓词)` / `.filter_map(...)` |

> 但**超过三层嵌套就该回到 `match`/`let else`**：组合子的可读性在第三层开始崩，尤其是混着 `and_then` 与 `map` 的时候。

## 4. 错误类型的边界

- **库 / capability 内部**：具体枚举（`thiserror` 派生），每类带分类，跨进程可传（见 [`../engineering/errors.md`](../engineering/errors.md)）。
- **二进制入口 / CLI**：`anyhow::Result` 可以，但要**在边界上转换一次**（`anyhow!(err)`），不要把它渗进 capability 签名。
- **测试**：`unwrap` / `expect` 合理，且测试 crate 显式 `allow(clippy::unwrap_used)`。

## 5. `must_use` 与"静默丢错误"

- `Result` 是 `#[must_use]`，忽略会有警告；但 `let _ = foo();` 能压掉它——**这正是泄漏错误的标准姿势**，review 时要盯。
- 确实要忽略时写明意图：`let _ = tx.send(evt); // 读者可能已退出`。
- 反过来：自己写的 `fn` 返回 `Result` 时加 `#[must_use]`，别让调用方能悄悄丢。

## 6. 常见坑

- 用 `.unwrap()` 处理"配置缺失"：库代码里等于把用户输入错误变成进程崩溃。
- `if let Some(x) = ... { ... } else { return }` 早就可以写成 `let ... else`。
- `.and_then(|x| Ok(y))`（该用 `.map`）以及 `.map(|x| Ok(y))`（该用 `.and_then`）——两者混淆会让类型多嵌套一层。
- 把 `Option<T>` 一路传到 UI 层，再用 `unwrap_or_default()` 兜底：语义丢了（"没有"与"空值"不是一回事）。
- 在 `Display` 里塞调试信息：`Display` 给用户看，`Debug` 给开发者看。

## 7. 清单

- [ ] `?` 的每一跳都有明确的 `From` 映射，不靠字符串
- [ ] "查不到"用 `Option`，"失败"用 `Err`，两者别互相冒充
- [ ] 组合子不超过三层，超过就换 `match` / `let else`
- [ ] 忽略错误的地方写了理由
- [ ] capability 签名里没有 `anyhow::Error`

## 相关链接

- [`../engineering/errors.md`](../engineering/errors.md)（错误分类、跨进程传输、与 Go 侧的对齐）
- [`ownership.md`](ownership.md)（`?` 早退与 `Drop` 回滚是同一套"提前退出也要保持状态一致"的思路）
- [`traits.md`](traits.md)（`From` / `Into` 是 trait 系统的一部分）
- [`../engineering/async_blocking.md`](../engineering/async_blocking.md)（异步函数里的错误传播与阻塞的关系）
