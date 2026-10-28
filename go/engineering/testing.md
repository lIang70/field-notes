# 测试布局（多模块 / 跨平台 / 长跑）

> 一个既有 platform 分支、又有协议层、又跑长会话的项目，"测试"不是一个命令，是一张矩阵。
> 例子来自 flowcraft（多模块 + 平台 lane）与 opencraft（fuzz / soak / 性能基线）。

## TL;DR

- **测试密度是分层的入场费**：契约多、实现多的框架，没有几千个 `_test.go` 根本不敢重构。
- **平台差异必须有自己的 lane**：Windows / macOS / bwrap / Postgres 各自一组作业，混在一个 `go test ./...` 里既跑不动也定位不了。
- **required check 要挂一个"聚合门"**（`ci-pass` 那种），而不是把十几个作业名逐个写进分支保护——否则改一次 workflow 就全线卡住。
- **动态矩阵**：按改动文件算出要跑的模块，全量跑只在 main 上发生。
- **性能测试要钉"性质"不要钉"数字"**——`TestScanLinesAllocatesNothing`、`TestIndexStaysFlat`、不动点断言能守几年；`< 50ms` 只会在换机器那天红。

## 1. 四层，别混

| 层 | 目的 | 形态 |
| :--- | :--- | :--- |
| 单元 | 一个函数一个决定 | table-driven，输入/输出/错误三类断言 |
| 集成 | 一个 capability 走通（含真实 SQLite / 真实子进程） | 用临时目录 + `t.Cleanup` |
| 契约 | 多实现共享同一套行为 | 一个 `contract_test.go`，每个实现都跑一遍 |
| 端到端 | 一条真实用户路径 | 少而稳，跑在干净环境 |

契约测试是这套里最容易被忽略的：**同一份 deploy 文档有多种后端实现时，行为必须由契约测试保证，而不是由每个实现各自的测试"碰巧一致"**。

## 2. 平台矩阵

一个真实的多模块 CI 长这样：

```text
gofmt      lint      test(矩阵)      rust      rust-windows
test-memory-postgres   test-memory-windows
test-sandbox-bwrap   test-sandbox-macos   test-sandbox-windows
test-core-windows    test-craft-windows
                    ↓
                 ci-pass (if: always(), needs: 上面全部)
```

- 平台 lane 的价值在"回归能被复现"：没有 Windows lane，`HOME` 假设那类测试会成片红在用户机器上，而不是 CI 上。
- **`ci-pass` 是个聚合作业**：`if: always()` + `needs` 全列，分支保护只要求它一个。它自己判断"上游有任何一个失败/取消就失败"。这样改 workflow 内部结构不影响保护规则（发布侧的交待见 [`release.md`](release.md) §4）。
- **作业名就是 required check 的 context**：这条依旧成立——但挂了聚合门之后，只有 `ci-pass` 的名字是稳定 API。

## 3. 动态矩阵

全量多模块跑太慢，按改动算：

- 一个脚本（Python 就行）读 `git diff` 的文件列表 → 映射到所属 module → 输出矩阵 JSON；
- 没有匹配到任何模块的改动（只动 docs / CI）→ 矩阵为空，作业直接跳过，但 `ci-pass` 仍要成功；
- 破坏性改动（改了某个底层 module）→ 强制带上依赖它的下游模块。

> 经验：**矩阵计算本身要可测试**。写成一坨 YAML 里的内联脚本，出问题时你只能靠"看它有没有跑"来 debug。

## 4. fuzz 与 soak

- **fuzz 挑"解析器"**：配置解析、protobuf 解码、patch 解析、skill manifest、extract 正则、协议帧——这些地方输入空间大且失败代价高。8 个 `Fuzz` 目标比 800 个 happy-path 测试更值。
- **soak 挑"生命周期"**：进程池、插件子进程、交互通道。跑几千次起停，看有没有 fd / goroutine / 子进程泄漏。
- 两者都放独立作业（或 nightly），不要塞进 PR 门禁。

## 5. 基准与门禁

- `Benchmark` 只报数，不判定——**判定靠"性质测试"**：分配次数为 0、索引结构是扁平的、裁剪前后恢复等价、旧对象可回收。
- 分配计数门禁：`testing.AllocsPerRun` 或 `ReportAllocs` + 阈值，钉住"这条路径不再新增分配"。
- 性能基线脚本（跨进程、跨渲染器）单独存在，CI 里跳过并注明"构建机时序不是基线"（脚本形态见 [`optimization.md`](optimization.md) §0）。

实际长成的样子（都真实存在，改名或删掉就会红）：

| 测试 | 钉住的性态 |
| :--- | :--- |
| `TestIndexStaysFlat` | 搜索索引不得退回"每词一个 map" |
| `TestRebuiltHostIsCollectable` | 重建后旧 host 不可达（weak pointer） |
| `TestClosedRuntimeIsCollectable` | 关闭后的 runtime 可回收 |
| `TestScanLinesAllocatesNothing` / `TestScanLinesMatchesSplitSemantics` | 扫行零分配，且与 `split` 语义一致 |
| `handoff_test.go` / `assembly_test.go` | 消息 take-once、前缀缓存不读旧数据 |
| 裁剪后恢复路径 | 对裁剪过的板再跑一次恢复，得到的消息集不变（不动点） |

## 6. 反模式

- 只测 happy path：错误分支没人跑过。
- 用 e2e 替代单元测试：一次失败要 5 分钟才知道是哪层。
- snapshot 全量对比：改一个空格红一片，最后大家学会 `-update` 一切。
- 依赖时间与顺序：没有 `t.Cleanup`、没有临时目录隔离，测试之间互相污染。
- 测试里假设环境（`HOME` 存在、有网络、是 unix）：这些在平台 lane 上会一次性爆掉。

## 7. 清单

- [ ] 多实现：有共享的契约测试
- [ ] 平台差异：有独立 lane，且是聚合门的一部分
- [ ] 解析器：有 fuzz 目标
- [ ] 生命周期资源：有 soak
- [ ] 性能相关结论：落成性质测试，不是阈值
- [ ] 新服务 / 新模块：测试已接进 workflow（对表检查）

## 相关链接

- [`conventions.md`](conventions.md)（错误分类、可选接口——测试要覆盖默认路径）
- [`release.md`](release.md)（required check 与聚合门）
- [`optimization.md`](optimization.md)（上面那些性态测试到底在守哪一轮优化，§5 有全景）
- [`../runtime/pprof.md`](../runtime/pprof.md)（profile 定位、测试守门）
- [`observability.md`](observability.md)（trace → offline dataset，测试与线上共用一套用例）
