# 可观测性（日志 / trace / 指标 / 归因）

> 一个 agent 系统的可观测性和普通服务最大的差别是：**你要能回答"它为什么这么做"**，而不只是"它有多慢"。
> 指标与日志的具体形态，配合 [`../../ai/harness/observability.md`](../../ai/harness/observability.md)（方法论层）一起看。

## TL;DR

- **归因比指标值钱**：`runtime assembled` 这条日志里带 reason（startup / workspace_open / settings_save / plugin_change…）+ 耗时 + 是否在跑 + 该工作区的装配序号，才能回答"谁在重建"。
- **指标单点定义**：一个 meter、一套 instrument 名字，早建晚接（SDK 未初始化时是 no-op），不要每个调用点自己命名。
- **前端 RUM 要按平台裁剪**：WebKit 永远不报 layout shift，那项指标在桌面端是纯噪声，直接去掉。
- **把"请求离开进程"和"响应头到达"分开打点**：这一刀切出"本地装配 vs provider 时间"，否则永远在优化错的一段。
- **日志/事件的量必须自己就有上界**（环形缓冲、行上限、每秒上限），否则观测系统本身会成为 OOM 的第一现场。

## 1. 归因优先

装配类系统最常见的现象是"偶发卡 300ms"，日志里只有一句 "rebuilt"。可行的做法是给每条路径打一个**原因枚举**，并把它作为日志字段而不是注释：

```go
type AssemblyReason string

const (
    ReasonStartup      AssemblyReason = "startup"
    ReasonWorkspaceOpen AssemblyReason = "workspace_open"
    ReasonSettingsSave AssemblyReason = "settings_save"
    ReasonPluginChange AssemblyReason = "plugin_change"
)

log.Info("runtime assembled",
    "reason", reason, "workspace", ws, "duration", d,
    "live", running, "seq", seq)
```

配套三条：失效也要记（`runtime invalidated` + reason）、原地热替换要记（`document reloaded in place`）、**失败回退要记原因**（"本想原地换，为什么改成全量重建"）。

> 这套字段的直接回报：一次"偶发卡 300ms"最后定位到"保存设置 + 切工作区 + 插件写入"在同一个 drain 里各自唤醒一个 watcher（[`optimization.md`](optimization.md) §7）。没有 reason 字段，这个结论只能靠猜。

## 2. 指标

- **单点定义**：一个包暴露 `Meter()` 与 `MustFloat64Histogram(name)`，名字集中在一处；SDK 未初始化时这些 instrument 是 no-op，SDK 设好后自动生效。
- **命名带语义**：`<domain>.<object>.<unit>`，例如 `host.runtime.assemble.duration`；不要出现 `latency2`。
- **基数控制**：不要把 `workspace` / `session_id` 当标签——一多就是基数爆炸；需要细分就进日志，不进指标。
- **版本随指标走**：`service.version` 从单一变量注入（`-ldflags -X`），与 release tag 同一个来源。

## 3. RUM（桌面 / Web 侧）

- 采样的是**用户感知的那几帧**：首帧、长任务、交互耗时、DOM 行数——不是"页面加载完成"。
- **按渲染引擎裁剪**：WebKit 不报 CLS（layout shift），指标留着就是永远 0 或永远空；去掉并把原因写进注释。
- **去重**：同一导航会被上报两次（初始化 + 导航事件），需要按 id 去重，否则分位数全是假的。
- 上报走与后端同一条 telemetry 管线，别另起一套出口——否则两边时间轴对不上。

## 4. 日志

- **结构化 + 有界**：JSON 行，单行长度上限（例如 16 KiB）、每秒行数上限（例如 500），超出就丢弃并计数。
- **环形缓冲**：进程内最近的日志保留在固定大小的 ring 里（10 MiB / 256 KiB 两种量级都见过），供崩溃后导出；不要"全量落盘"。
- **脱敏在入库前**：工具调用的参数与结果在写审计流之前就替换敏感值；展示时再脱敏已经晚了。
- **审计流是 append-only**：`tool-calls.jsonl` 这类文件只追加，带 hash/时间，用于事后复盘而不是实时排查。

## 5. 把 trace 接回评测

- trace 里给每个 span 打 `(turn, agent, tool, prompt_segment)` 四元组，失败归因从"哪里错了"收敛到"哪一行 prompt 错了"。
- 线上 trace → 离线 dataset：`(input, expected_trajectory, expected_outcome)` 三元组，可以直接 replay 失败 case（见 [`../../ai/evals.md`](../../ai/evals.md)）。
- 成本按 `agent.name` / 模型 group by，找"钱花在谁身上"；这是多 agent 系统唯一现实的成本治理手段。

## 6. 反模式

- 每个调用点自己拼指标名 → 同一个延迟有三份曲线，谁都不完整。
- 用日志当指标（grep 统计 p95）→ 量一大就崩，且没法告警。
- 观测数据无上界 → 观测系统自己成为 OOM 现场。
- 只看平均值：p95 才是用户体感；均值被大量快请求稀释。
- 没有 trace 的"玄学调试"：只有"调用了一次 LLM"这一行日志时，任何结论都是猜。

## 7. 清单

- [ ] 关键路径有原因枚举，日志里带 reason 与序号
- [ ] 指标名集中定义、基数可控、版本随包注入
- [ ] 前端指标按引擎裁剪并去重
- [ ] "本地耗时 / 远端耗时"分开打点
- [ ] 日志与事件有行数、大小、速率三重上界
- [ ] 脱敏发生在写入之前
- [ ] trace 能转成离线评测集

## 相关链接

- [`../architecture/layering.md`](../architecture/layering.md)（runtime 装配发生在哪一层）
- [`optimization.md`](optimization.md)（用归因定位装配风暴的那一轮；§证据来源表里有每节指标的出处）
- [`../runtime/pprof.md`](../runtime/pprof.md)（profile 答"分配在哪"，指标答"何时变差"）
- [`testing.md`](testing.md)（trace → offline dataset，测试与线上共用一套用例）
- [`../../ai/harness/observability.md`](../../ai/harness/observability.md)（方法论：四件套、四元组、trace→dataset）
