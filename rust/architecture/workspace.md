# Rust workspace 的形状（crate / 模块图 / feature）

> 一个"平台层"用 Rust 写的时候，crate 边界和模块边界不是审美问题，而是**编译期能替你守什么**的问题。
> 案例来自 flowcraft 的 `rust/`（MIT）：两个包、四个 member、六万行量级。

## TL;DR

- **crate 是发布单位，模块是组织单位**——从十几个 layer crate 塌成 1 个库 + 1 个守护进程之后，单向依赖不再由 Cargo 强制，那就用**一个扫描源码的测试**去守（`tests/layering.rs`）。
- **feature 要沿着与模块相同的单向边排**：`caps` → `platform` → `telemetry` → `error`，谁需要谁就开，别做"全开"和"全关"两档。
- **生成代码要提交进仓库**：消费者不应该为了编译一个库而装 `protoc`；生成器本身 `publish = false`，因为它是仓库工具而不是库的一部分。
- **`rust-version` 是承诺，不是备注**：写进去的版本 cargo 会强制，所以要写"真的能编译通过的地板"，而不是"我想要的版本"。
- **把生成器做成 example target 会把程序塞进发布的包里**——cargo 会打包 target 需要的文件。做成独立 member 才能真正排除。

## 1. 两个包，两种受众

```text
flowcraft            库：契约、装配、控制面、能力（消费方只需要它）
flowcraft-platformd  守护进程 flowcraftd：唯一交付的二进制 + supervisor/admin 面
flowcraft-win32      唯一允许 unsafe 的 crate：Win32 机制的安全封装
tools/proto-gen      仓库工具：从 proto/ 生成 src/proto/generated/（publish = false）
```

- 库与守护进程分离，**"依赖库的只能是守护进程"这条也由测试检查**。
- `flowcraft-win32` 在任何平台都能编译，非 Windows 上是空的——这样 workspace 构建不需要 Windows 主机（细节见 [`unsafe_boundary.md`](unsafe_boundary.md)）。

## 2. 模块图：从 Cargo 强制变成测试强制

```text
error, proto                      叶子
telemetry  → error
message    → error, proto
resource   → error, logerr         （不依赖 telemetry）
kernel     → error, telemetry, resource, logerr
client     → error, telemetry, proto, kernel, craft
caps::*    → error, resource, kernel, client, proto
```

以前十几个 crate 的时代，这些单向边由 Cargo 免费保证；装进一个 crate 之后就没有了，于是 `tests/layering.rs` 改成**扫源码**：每个模块里出现的 `crate::` 路径与一张表（`LAYERS`）比对，越界即失败。

这个测试有两处值得抄：

- **只读代码路径，不读注释**：`//!` 里写 `crate::kernel::identity` 是文档，不是依赖；扫描前先把注释剥掉。
- **承认它是近似**：`use` 粒度、宏展开、`cfg` 分支都扫不到。测试文件里明写了"这是近似，而且近似本身就是重点"——**一个能跑的近似远胜一张没人守的架构图**。

## 3. feature 与模块同向

```text
error        （永远编译）
telemetry    → error
proto        （生成代码）
resource     → telemetry
kernel       → resource
client       → kernel
caps::*      → client
```

- 每个 capability 单独一个 feature（`workspace` / `secret` / `sandbox` / `tool`），**单独开就能用**；`wire` 才额外拉进 endpoint server、远端 client 和 grant scope 校验。
- 好处是消费方可以只引"我要的那个能力"，而不是为了一个文件读写 API 拉进整个控制面。
- 代价：feature 组合是笛卡尔积，必须有一条 CI 只跑"最小组合"（否则默认 feature 掩盖了缺失的 `cfg`）。

## 4. 生成代码与 `rust-version`

- `proto/` 在仓库根，与 Go 侧共享；Rust 绑定**提交进仓库**，`make rust-proto-gen` 重生成。消费者编译、发布都不需要 `protoc` 和 build script。
- 生成器最早是个 example target，结果 cargo 把 example 需要的文件一起打进了 crate——改成独立 member + `publish = false` 才真正排除。
- `rust-version = "1.90"` 的注释写的是**理由**（依赖树里 rmcp 要 1.88、process-wrap 要 1.87），不是愿望；写低了不是"兼容更多人"，是"发布即报错"。

## 5. 写 workspace 时的一串决定

| 决定 | 理由 |
| :--- | :--- |
| `[workspace.lints]` 统一策略，crate 内用 `workspace = true` 继承 | 一处改策略，三个 crate 同时生效 |
| 单一 crate 允许例外 | 只有 `flowcraft-win32` 放开 `unsafe_code`，且额外收紧 `undocumented_unsafe_blocks` |
| 生成的 proto 用 `#[non_exhaustive]` 之外的错误枚举手写 | 协议的扩展性由 proto 管，错误分类由 Rust 的 `enum` 管 |
| 版本号只出现在 workspace 级 | 多个 crate 各自写版本，迟早对不上 |

## 相关链接

- [`unsafe_boundary.md`](unsafe_boundary.md)（为什么必须有一个"只放 unsafe"的 crate）
- [`../engineering/errors.md`](../engineering/errors.md)（错误分类怎么与 Go 侧对齐）
- [`../engineering/bounds.md`](../engineering/bounds.md)（这些模块里的容器为什么都带上限）
- [`../../go/architecture/layering.md`](../../go/architecture/layering.md)（Go 侧同一套分层的形状）
- [`../../go/architecture/craft_abi.md`](../../go/architecture/craft_abi.md)（守护进程对外暴露的就是这份 ABI）
