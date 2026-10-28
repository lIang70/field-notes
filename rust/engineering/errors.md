# 错误分类（跨语言、跨进程）

> 一套系统同时有 Go 和 Rust 两棵树时，错误分类不是风格问题：**分类错了，重试策略就错了**。
> 案例来自 flowcraft：Rust 侧 `flowcraft::error`，Go 侧 `core/errdefs`。

## TL;DR

- **分类要对齐，不要求同**：Rust 侧 14 类里有 13 类镜像 Go 的 `errdefs`，只多一个 `Closed`（Go 侧用裸 `FailedPrecondition` 表达）。
- **`Display` 只渲染 message**，不渲染"kind: message"——这样两棵树对同一个错误的 diff 才能逐字比对。
- **gRPC status code 是有损的**：`BudgetExceeded` 与 `RateLimit` 都映射到 `ResourceExhausted`，`PolicyDenied` 与 `Forbidden` 都映射到 `PermissionDenied`。只读 code 的 peer 会把**永久的预算失败当成可重试限流**、把**策略拒绝当成权限问题**。
- 所以跨进程要传两样：**分类 + 可选的结构化 detail**，分类放在自己的 wire 类型里（`flowcraft.error.v1`），status code 退化成给通用中间件的粗提示。
- **枚举要 `#[non_exhaustive]`**。加一类是契约变更，下游的 `match` 必须被迫承认，而不是默默走 `_ =>`。

## 1. 分类

```rust
#[non_exhaustive]
pub enum ErrorKind {
    Validation, NotFound, Unauthorized, Forbidden, Conflict,
    // …共 14 类
}
```

- **必须是 `enum` 而不是字符串**：字符串分类永远会漂（`"rate_limit"` / `"RateLimit"` / `"rate-limit"`）。
- **`non_exhaustive`**：新增类别是破坏性变更，强制下游重新编译并处理。
- **每个类别带一个"可重试性"**，别让调用方猜：`RateLimit` 可重试、`BudgetExceeded` 不可重试——两者在 HTTP 上是同一个 429/402 附近的模糊地带。

## 2. 跨进程传输

```text
同进程：  Error { kind, message, detail: Vec<Detail> }
跨进程：  flowcraft.error.v1 { kind, message, detail }
          gRPC status code = 粗提示（给中间件 / 通用重试器）
```

- **detail 是结构化的、可选的**：字段名、期望值、位置这类信息放进去，而不是塞进 message 里让人 `regex`。
- **不要用 message 做逻辑**：两棵树的文案本来就不一样；需要判断的只有 kind 和 detail。
- 保留 status code 是为了让 gRPC 生态里的通用组件（重试拦截器、负载均衡）还能工作，但它不再承载语义。

## 3. `Display` 的约定

`Error` 的 `Display` 只输出 message：

- 好处是"同一个错误在 Go 与 Rust 侧的文案可以逐字 diff"，在双树联调时非常省事。
- 代价是调试时看不出分类——所以 `Debug` 里带 kind，日志里显式打 `kind=` 字段。

## 4. 与 Go 侧的映射表要机器可查

- 14 类的名单在一个地方（Rust 的 `taxonomy.rs` / Go 的 `errdefs`），两边各有测试断言"类目集合一致、映射关系不重不漏"。
- 分类改动的 review checklist：**是否两边都改、wire 是否兼容、映射测试是否更新**。
- 双树的其它约定见 [Go 工程约定](../../go/engineering/conventions.md) §6。

## 5. 反模式

- 用 `anyhow::Error` 穿过 capability 边界：上层拿不到分类，只能当"未知错误"处理。
- 把 `Whatever(String)` 加进分类：等于退回字符串。
- 在 wire 上只传 status code（这就是被修掉的那个 bug）。
- 用 `?` 把错误一路 `into()` 到顶层，丢掉原始分类——每一跳都要能还原"原来是什么"。

## 6. 清单

- [ ] 分类是 `enum` + `non_exhaustive`，每个类别写明可重试性
- [ ] `Display` 只给 message，`Debug` 带 kind
- [ ] 跨进程传 kind + detail，status code 仅作提示
- [ ] 与另一棵树的映射有测试对表
- [ ] capability 边界上不出现 `anyhow::Error`

## 相关链接

- [Rust workspace 的形状](../architecture/workspace.md)（模块图里 `error` 在最底层，谁都依赖它）
- [unsafe 的边界：一个 crate 专门收着](../architecture/unsafe_boundary.md)（系统调用错误怎么进分类）
- [有界容器](bounds.md)（拒绝类错误与资源上限的关系）
- [Go 工程约定](../../go/engineering/conventions.md)（Go 侧的 `errdefs`、双树同修）
