# 测试布局（平台层 / 跨进程 / 跨平台）

> 平台层的测试重点和业务代码不一样：**协议一致性、结构约束、平台差异**，而不是"函数返回对不对"。
> 案例来自 flowcraft 的 `rust/`（MIT）：一致性套件可被任意实现复用，结构测试扫源码，平台差异用 `cfg` 显式排除而不是留空。

## TL;DR

- **一致性套件要写成"可被别人指向的断言函数"**（`assert_craft_abi`），配一个参考实现（stub）。这样任何新的 craft 实现都能一行接入，而不是各家抄一份测试。
- **`#![cfg(unix)]` 比"留空不测"诚实**：整个文件在 Windows 上直接不编译，注释写明"unix-only rather than untested"。
- **结构约束也进测试**：`tests/layering.rs` 扫 `crate::` 路径来守模块单向图（Cargo 不再替你守时，这条只能靠测试）。
- **时间语义用注入时钟测，不用 `sleep`**：`Clock = Arc<dyn Fn() -> SystemTime>` 让过期逻辑可测且并发安全。
- **测试 crate 要显式 `allow(clippy::unwrap_used)`**——workspace 级 `deny` 是给库代码的，测试里 `unwrap` 就该炸。

## 1. 目录形状

```text
flowcraft/src/**           单元测试内联在 #[cfg(test)] mod
flowcraft/tests/layering.rs          结构测试（模块单向图）
flowcraft-platformd/tests/
  conformance.rs           可复用的一致性套件 + 参考实现
  craft.rs  craft_stdio.rs  ABI 的两种传输
  reload.rs                 世代切换与失败语义
  hosting.rs  secrets.rs  watcher.rs  admin.rs  cli.rs
  support/                  fixture 与临时目录
```

- **单元测试不追求覆盖率，追求"这条约定有没有被钉住"**：契约、错误分类、边界常量更值得写。
- **集成测试在 `tests/` 下按"能力"分文件**，一个文件一个主题，失败时一眼知道是哪条契约破了。
- `support/` 放 fixture 构造（平台文档、临时目录、签名密钥），避免每个测试文件各写一份 `tempdir` + 清理。

## 2. 一致性套件：断言函数，不是测试集合

```rust
// conformance.rs：拿到一条指向被测实现的 client，就能跑完整套
async fn assert_craft_abi(client: &CraftClient) { /* Describe/Start/Run/Steer/Renew/Cancel/Close */ }
```

- 被测实现通过 `CraftClient` 接进来（unix socket 或 `dns://`），所以同一套断言既能测 `craft-stub`，也能测任意真实实现。
- 参考实现是 `craft-stub`：既做测试夹具，也做"契约长什么样"的活文档。
- 断言覆盖的是**生命周期语义**而不是字段值：`Run` 必须以恰好一个 `done` 或 `error` 结束、`Cancel` 幂等、`Close` 之后 `Run` 必须失败、`Renew` 整组替换 grant。
- 这类套件的价值在于：**平台侧的契约变了，所有实现一起红**，而不是等某个实现悄悄漂了三个月。

## 3. 结构测试：扫描源码

`tests/layering.rs` 读每个模块的 `crate::` 路径，与一张 `LAYERS` 表比对：

- **只读代码路径，不读注释**：`//!` 里出现的 `crate::kernel::…` 是文档，扫描前剥掉。
- **承认近似**：宏展开、`cfg` 分支、`use` 粒度都扫不全。测试文件里直接写明"这是近似，而且近似本身就是重点"。
- 同一个文件还断言"恰好两个包"和"只有 daemon 依赖库"——这两条无法用表表达。

> 可迁移的经验：**凡是"以前由 Cargo/编译器免费提供"的约束，合并 crate 之后都要补一条测试**。否则它只是从"被强制"降级成"被希望"。

## 4. 平台差异：显式排除

```rust
//! These drive unix-domain sockets and a POSIX shell; a Windows host has
//! neither in this crate yet, so the file is unix-only rather than untested.
#![cfg(unix)]
```

- 比在函数里 `if cfg!(windows) { return }` 好在：**Windows 上根本不会编译它**，也不会出现"跑过了但什么都没测"的假绿。
- Windows 侧的对应能力由 `rust-windows` 这条 CI lane 覆盖（见 [`../../go/engineering/testing.md`](../../go/engineering/testing.md) §2 的聚合门）。
- 平台差异一旦有实现，就把 `cfg` 从整文件缩到具体用例——`cfg` 放得越宽，漏测的面越大。

## 5. 时间与并发

- **注入时钟**：`Clock = Arc<dyn Fn() -> SystemTime + Send + Sync>`，过期/剪枝逻辑直接测边界秒。
- 不用 `sleep` 等"大概到期"：那既慢又不稳，还会在并发下互相污染。
- async 测试用 `#[tokio::test]`（仓库里有 200+ 个），但**不依赖 wall clock**：等待用通道/`watch` 的信号，而不是固定时长。
- 超时类逻辑测"超时之后的行为"（错误分类、资源回收），不测"是不是刚好在 N 毫秒触发"。

## 6. lint 与测试

- workspace 级 `unwrap_used = "deny"`、`expect_used = "warn"`，测试 crate 里显式 `#![allow(clippy::unwrap_used, clippy::expect_used)]`——**测试该炸就炸**，库代码不行。
- `undocumented_unsafe_blocks = "deny"` 对测试同样生效：测试里写 unsafe 也要写 SAFETY。
- `make rust-test` / `rust-fmt-check` / `rust-clippy` 与 Go 侧同一个入口，避免"两套语言两套习惯"。

## 7. 这份树的缺口（诚实清单）

- **没有 fuzz 目标**：解析器类的模糊测试目前只在 Go 侧（配置、patch、execd 帧）。Rust 侧的 config / proto 解析还没有。
- **没有 proptest / quickcheck**：区间合并、路径规范化这类纯函数适合属性测试，现在靠固定用例。
- **没有基准**：性能判断目前靠"有界常量 + 观测"，没有 criterion bench。

> 这三条都是"知道缺、暂时接受"，写在这里比留在脑子里好——下次有人问"这棵树测到位了吗"，答案在这。

## 相关链接

- [`../architecture/workspace.md`](../architecture/workspace.md)（`layering.rs` 守的那张模块图）
- [`../runtime/tokio.md`](../runtime/tokio.md)（世代切换、`watch` 通知怎么测）
- [`../../go/engineering/testing.md`](../../go/engineering/testing.md)（Go 侧的四层、平台矩阵、fuzz/soak、性质测试）
- [`../engineering/bounds.md`](../engineering/bounds.md)（"有界"这一类约定的测试形态）
