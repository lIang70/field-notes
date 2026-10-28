# unsafe 的边界：一个 crate 专门收着

> 平台层绕不开 Win32：Job Object、安全令牌、完整性标签、WFP 过滤引擎全是裸系统调用。问题不是"要不要 unsafe"，而是"让它能出现在哪"。
> 案例来自 flowcraft 的 `rust/`：`flowcraft` 库 `forbid(unsafe_code)`，`flowcraft-win32` 是唯一例外。

## TL;DR

- **`forbid` 不能被内部的 `allow` 放宽**——这正是它比 `deny` 有用的地方：一旦选了 forbid，任何一处需要 unsafe 的代码都**必须**去另一个 crate。
- 所以"单 crate 收 unsafe"不是洁癖，是 `forbid` 逼出来的结构：`flowcraft-win32` 对外是 safe API，对内每个 `unsafe` 块配 `// SAFETY:` 说明，并有 lint `undocumented_unsafe_blocks = "deny"` 兜底。
- **这个 crate 在任何平台都能编译，非 Windows 上是空的**——workspace 构建不需要 Windows 主机，CI 也不需要"跳过整个包"。
- **把 unsafe 收进一个 crate，会让"不安全的面"变成可审计的对象**。评审只需要读一个 crate 的公开签名，而不是在 6 万行里找 `unsafe`。

## 1. 策略

```toml
# 根 Cargo.toml
[workspace.lints.rust]
unsafe_code = "forbid"
missing_docs = "warn"

[workspace.lints.clippy]
unwrap_used = "deny"
expect_used = "warn"
future_not_send = "warn"
```

```toml
# flowcraft-win32/Cargo.toml —— 唯一例外，且更严
[lints.rust]
unsafe_code = "allow"
missing_docs = "warn"

[lints.clippy]
unwrap_used = "deny"
expect_used = "deny"
undocumented_unsafe_blocks = "deny"
```

- `unwrap_used = deny` 在库侧是真需求：库里的 `unwrap` 等于把"输入不合法"变成"进程崩"。
- `future_not_send = warn` 会在你把非 `Send` 的东西带进 `async fn` 时提示——它抓不到全部，但能抓住最常见的一类。
- 例外 crate 反而更严：**放开 unsafe 的同时把 `undocumented_unsafe_blocks` 从 warn 提到 deny**。

## 2. 这个 crate 里装什么

- **`job`**：进程树边界——带 `KILL_ON_JOB_CLOSE` 的 job object、内核内存与 cpu-time 限制、以及"这个会话为什么结束"的记账。
- **`process`**：管道会话的 spawn：三根管子、子进程端在句柄列表里继承、以及和终端一样的 token + job。
- **token / 完整性标签**：受限令牌、低完整性级别、AppContainer profile。
- **WFP（规划中，尚未落地）**：计划按包 SID 在 bind 层装过滤器。目前这个 crate 只有 job / process / pty / token / acl 五个模块，网络策略还在 Go 侧。

机制本身（哪个隔离层在哪一步生效）写在 [`../../os/sandbox.md`](../../os/sandbox.md)；这里只关心 Rust 侧的组织方式。

## 3. 写在块上的约定

每个 `unsafe` 块至少要回答三个问题，写不成句子就说明这段不该在这里：

```rust
// SAFETY: `job` is a live handle we own (created above), the struct outlives
// the call, and `SetInformationJobObject` only reads the struct for the
// duration of the call.
unsafe { SetInformationJobObject(job, info_class, ptr.cast(), size) }?;
```

- **是谁的资源、谁的负责释放**；
- **调用期间什么必须成立**（句柄有效、结构体布局、生命周期）；
- **失败会怎样**（返回码、错误映射）。

## 4. 跨平台的空实现

- crate 顶层在非 Windows 下几乎没有内容，但**签名与类型仍然存在**（feature / `cfg` 只裁掉实现）。
- 这样上游可以无条件 `use flowcraft_win32::job::Job;`，由调用点决定"这个平台能不能用"。
- 也可以反过来：非 Windows 下这些类型直接编译失败，逼调用方显式处理——两种都行，**关键是要一致**，不能一半可用一半 panic。

## 5. 代码评审时看什么

- 新增 `unsafe` 是否都在同一个 crate 里（否则 `forbid` 会先替你拦下）。
- 有没有"安全的封装 + 不安全的前提"错位：safe 函数签名不变，但内部多了一条未文档化的前提。
- 句柄 / 资源的所有权是否唯一：`Drop` 里关一次，别在两处关。
- 错误路径：系统调用的返回码是否映射到分类错误，而不是 `unwrap`。

## 相关链接

- [`workspace.md`](workspace.md)（crate 划分与 lint 继承）
- [`../../os/sandbox.md`](../../os/sandbox.md)（Job Object / AppContainer / WFP 的机制与坑）
- [`../engineering/errors.md`](../engineering/errors.md)（系统调用错误怎么进分类体系）
- [`../../go/engineering/conventions.md`](../../go/engineering/conventions.md)（Go 侧对应的"扇出边界 recover"一类约定）
