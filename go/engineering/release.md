# 多模块发布与发布工程

> 一个仓库里放十几个 go module 之后，"发版"就不再是一个命令，而是一套需要门禁的流程。
> 案例来自 flowcraft（changeset + `tools/releasegate`）与 opencraft（桌面分发）。

## TL;DR

- **每个 module 独立 tag**：使用方只引需要的层，破坏性变更可控。代价是**升级传染**——一次 core 变动连带升 driver/backends，一个下游业务一年累积几十次"升级框架"提交。
- **一驱动一 module 是陷阱**：六个 provider 各一个 module 时，跨驱动修正要发六次版，最后只能靠一次破坏性重构合一。
- **发布门禁要硬编码依赖顺序**（`core` 先于 `craft`），否则"同批发版"会在 `go.sum` 上打结。
- **高频小版本只能靠流水线**：一年 139 次发布提交、`core` 从 v0.1 到 v0.9。
- **模块边界的成本不在"拆"，在"拆完之后每次横切变更要发几个版"**。拆之前先问：这类变更一年会发生几次？

## 1. 账

| 做法 | 好处 | 代价（真实付过的） |
| :--- | :--- | :--- |
| 每层一个 module + 独立 tag | 使用方只引需要的层；破坏性变更有边界 | 升级传染：下游累积几十次升级提交 |
| 一驱动一 module（6 个） | 依赖隔离干净 | 依赖图成网，跨驱动修正发六次版 → 最后 `refactor(driver)!: fold the six provider modules into one driver module` 合一 |
| changeset + releasegate | 发版可自动化、changelog 能聚合 | 要写校验规则（该 bump 没 bump 就红、`!` 算破坏性） |

## 2. 门禁的实际形状

`tools/releasegate` 做三件事：

1. **依赖顺序**硬编码（`core` → `craft`）。同批发布时被依赖方先进 `go.sum`，preflight 先跑 tidy，否则 tidy 会把"还没发布的版本"当成错误。
2. **一致性校验**：changeset ↔ 实际 diff ↔ 模块列表三者对得上；版本号 / changelog 段落 / tag 名字一处都不能漏。
3. **规划与聚合**：按模块算下一个版本、生成 changelog 段落、打 tag。

配套两条纪律：

- **changelog 归 CI 生成**，人工只在必要处 revert。人工写 changelog 一定会漂。
- **安全扫描钉进模块测试矩阵**（govulncheck），不是一个"有空再跑"的独立 workflow。

## 3. 桌面分发（opencraft）

- Homebrew cask + universal dmg；**签名在公证之前**，顺序错就白跑一次。
- Apple 公证的硬要求：API key 必须是**裸 PEM**（带多余换行的 secret 会失败）；`notarytool` 要流式输出并诊断上下文，否则失败只看到一行 timeout。
- 平台产物命名固定（zip 名、portable 变体），否则安装脚本分不清。
- 版本节奏：50 天 16 个版本（0.1.0 → 0.7.1），每个版本一段 changelog。

## 4. 约定机器化

- **required check 的 context 就是 workflow 里的作业名**。改作业名必须同步改分支保护规则，否则所有 PR 卡在 `Expected — Waiting for status to be reported`。
- 新服务的测试必须接进 workflow，否则那份回归没人跑——于是写了一个静态检查：全仓 `services/*/tests/test_*.py` 与 `ci.yml` 对表。
- **长任务不进 PR 门禁**：镜像构建、真机压测单独跑，PR 只跑离线秒级检查。

> "别忘了做 X"的约定，都要有一行代码去查 X。文档会过期，对表不会。

## 5. 反模式

- 用 `[skip ci]` 绕过对表检查——开一次口子，约定就死了。
- 版本号散落在多个文件（`package.json` + 插件 manifest + `version.json`）却没有一个 stamp 脚本。
- 把"发版"当成一个人的肌肉记忆，而不是一个能重放的脚本。

## 相关链接

- [分层与扩展点](../architecture/layering.md)（被发布的这些层是什么）
- [Go 工程约定](conventions.md)（`go.work`、依赖纪律）
- [自建推理服务](../../ai/inference_serving.md)（required check 那段的另一侧实践）
- [桌面 agent 的性能治理](optimization.md)（同一个项目的性能基线脚本怎么进 CI）
