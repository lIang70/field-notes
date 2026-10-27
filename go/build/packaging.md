# 构建与打包（dev / release / 跨平台 / 资产）

> 一个 Go 项目从"能 go build"到"能发版"，中间是 build tag、ldflags、embed、平台差异和一套打包脚本。
> 案例来自 flowcraft（多模块 + Rust 共存）与 opencraft（Wails v3 桌面应用，macOS / Windows / Linux 三平台）。

## TL;DR

- **dev 与 release 必须是两条构建路径**，差异写死在脚本里：dev 关内联（`-gcflags=all=-l`）便于调试，release 要 `-trimpath` + `-w -s` + 版本注入。
- **版本号只有一个来源**：`-ldflags -X pkg.ServiceVersion=...`，telemetry、UI、打包文件名都读它。
- **`-buildvcs=false`**：没有 `.git` 的构建环境（CI 缓存目录、源码包）不会因此失败。
- **embed 的代价是"资产改完要重编译"**：模板、SQL、内置技能都 embed 时，dev 迭代就要额外准备一条"从磁盘读"的路径。
- **`-gcflags=all=-l` 会让 profile 出现不存在的热点，也会让构建缓存几乎全废**——只给 dev 用，别在 release 里手滑带上。

## 1. 两个入口，各管一段

| 入口 | 管什么 | 例子 |
| :--- | :--- | :--- |
| `Makefile` | 跨语言门禁与常用动作，**不随平台变** | `fmt` / `lint` / `vet` / `test` / `check-boundaries` / `tidy` / `rust-test` / `rust-clippy` |
| `Taskfile`（或 `scripts/*.sh`） | 平台打包、产物命名、签名 | `build` / `package` / `run` / `dev`，按 OS 拆文件 |

> 反模式：把"打包 macOS dmg"塞进 Makefile 的 `all`。门禁要能在任何机器上秒级跑完；打包只在对应 OS 上跑。

## 2. dev vs release

```text
dev:      -buildvcs=false -gcflags=all="-l"          + 调试 tag（如 private_mac_apis）
release:  -tags production -trimpath -buildvcs=false
          -ldflags="-w -s -X <pkg>/version.ServiceVersion=v0.7.1"
          (windows 再加 -H windowsgui，避免弹控制台窗口)
```

各参数的真实理由：

- `-gcflags=all="-l"`：关内联，断点与栈才可读。**代价**是 profile 失真、构建缓存失效，所以只在 dev（失真到什么程度见 [`../runtime/pprof.md`](../runtime/pprof.md) §6）。
- `-trimpath`：去掉绝对路径，产物可复现、不泄露构建机目录。
- `-w -s`：去符号与调试信息，体积小一截；此时 `go tool pprof` 仍可用（需要同一份二进制）。
- `-X` 注入版本：**单一变量**被 telemetry 的 `service.version`、UI 显示、产物名共用。
- build tag 分环境：`production` 开关调试面板；平台私有 API 用单独 tag，而不是 `runtime.GOOS` 满天飞。

## 3. embed 的边界

```go
//go:embed templates/*.gotmpl
var templates embed.FS

//go:embed sql/workspace/*.sql
var workspaceSchema embed.FS
```

- **适合**：模板、schema、内置技能、默认配置——"和二进制同版本才有意义"的东西。
- **不适合**：用户会改的配置、需要热更的资产、体积大的模型文件。
- **dev 便利性**：embed 之后改模板必须重编译。要么加一个"目录存在就读磁盘"的开关，要么把模板改动频率高的部分挪出 embed。
- 嵌入的 schema 与迁移脚本要**同一份**：`go:embed sql/*.sql` + 启动时按版本跑，避免"代码里一份、迁移文件里一份"。

## 4. 跨平台

- **桌面应用要真机构建**：Wails / 平台 SDK 依赖本机工具链，交叉编译不现实；CI 上用三个 runner 各出一份产物。
- **Windows**：`-H windowsgui` 去控制台；路径、命名管道、ConPTY 都与 unix 分叉（见 [`../../os/sandbox.md`](../../os/sandbox.md) 这类实现差异）。
- **macOS**：私有 API 用 build tag 隔离，避免 App Store / 公证阶段才发现问题；`Info.plist`、entitlements 与 cask 一起版本化。
- **Linux**：区分发行版依赖（WebKitGTK 版本）与 AppImage/deb 产物。

## 5. 发布产物

- 产物命名固定（含平台、架构、版本），否则安装脚本和 cask 分不清"这次发的是哪个"。
- **签名在公证之前**；公证失败要能看到 notarytool 的原始输出（流式打印 + 诊断上下文），否则只看到一行 timeout。
- 生成的绑定 / 协议代码要 `-check` 变体（`gen-proto-check`、`gen-bindings` + diff），确保"提交的生成物 = 现在脚本会生成的"——这类检查属于 CI 门禁的一部分（[`../engineering/testing.md`](../engineering/testing.md) §2）。
- 生成器的参数也要有理由：比如给 TS 绑定加 `-names`，是为了让 mock 能按 service/method 路由，而不是按数字 id。

## 6. 构建缓存与可复现

- 缓存杀手：`-gcflags` 变更、embed 目录里改文件、`-trimpath` 是否一致、`GOFLAGS` 里加 `-race`。
- **可复现**：锁定 Go 版本（`go.mod` 的 toolchain 行 + CI 镜像）、`-trimpath`、`-buildvcs=false`、依赖走 vendor 或校验和代理。
- **多模块**：`go.work` 不进版本库（见 [`../engineering/conventions.md`](../engineering/conventions.md)），CI 用 `go work` 临时生成或用 module 矩阵逐个构建。

## 7. 清单

- [ ] dev / release 两条路径写死在脚本里，不靠人记参数
- [ ] 版本号单一来源，注入后能被 telemetry 与 UI 读到
- [ ] `-trimpath` / `-buildvcs=false` 在 release 里默认开启
- [ ] embed 的内容有 dev 替代路径，或明确接受"改资产要重编译"
- [ ] 生成物有 `-check` 变体进 CI
- [ ] 跨平台产物：真机构建 + 固定命名 + 签名先于公证

## 相关链接

- [`../engineering/release.md`](../engineering/release.md)（发布流程、changelog、required check）
- [`../engineering/conventions.md`](../engineering/conventions.md)（`go.work`、双树）
- [`../engineering/testing.md`](../engineering/testing.md)（生成物的 `-check` 变体怎么进门禁）
- [`../runtime/pprof.md`](../runtime/pprof.md)（release 构建下怎么取 profile；为什么 dev 参数会污染结论）
- [`../../ai/inference_serving.md`](../../ai/inference_serving.md)（容器镜像侧的构建期输入 / 多阶段构建）
