# 桌面 agent 的性能治理（opencraft 的十几轮优化）

> 记录一个本地桌面 agent 从"能用"到"能长时间用"的优化过程：每一轮都是先看到一条曲线或一个 heap profile，再动手。
> 项目开源（opencraft，MIT），数字都是实测；量在 Chromium/Go 侧，绝对值随机器变，形状可迁移。

## TL;DR

- **先有基线再优化**：一套可重放的 Playwright 脚本（长回答流式、4000 行 write_file、来回两屏历史、一次 workspace 切换）+ 三跑取中位数，先把"慢在哪一相"钉住。
- **性能债按"账"分类**：分配账（同一份数据被渲染/拷贝多次）、边界账（没有上界的东西）、序列化账（把整块状态反复落盘）、复制账（同一份 transcript 处理三遍）、缓存账（缓存了每轮都变的输入）。先分类再动手，比按直觉找热点省时间。
- **每条优化都要有"不动点"或"结构测试"**：裁剪后的状态再走一遍恢复路径必须等价；索引结构变平了排名必须一致；旧 runtime 不可达必须能被 GC 测出来。
- **最贵的一次错误是缓存迁移**：把缓存从"渲染结果"挪到"输入消息"，看起来省一次渲染，实际命中率接近零，反而把每轮解码量放大到 1.5 GB。
- **能省内存的检查，自己不能分配内存**——预算检查里先 `join` 一次流式块，就分配了这次检查要省下的那点内存。

## 证据来源

每一节的数字都不是"感觉"，来源和取数方式如下——**看数字之前先看它是哪种账**：

| 节 | 指标 | 当时的取数方式 |
| :--- | :--- | :--- |
| §0 基线 | 前端各相耗时 | Playwright spec + 每 phase 一个 probe，三跑取中位数 |
| §1 分配账 | 单次分配 / 耗时 | 基准（`-benchmem`）+ 种子语料逐 case 对齐 |
| §2 边界账 | 进程族 RSS | 打包 App + WebKit Malloc gauge（Go 堆里看不到） |
| §3 序列化账 | 字节 / 分配 / 耗时 | 生产 workspace 的 checkpoint 文件 + 基准 |
| §4 复制账 | 五分钟累计分配 | `alloc_space` heap profile（live 54 MB / alloc 6.78 GB） |
| §5 缓存账 | 单轮解码量 | `alloc_space` + `-focus message.decodeStrict`（1527 MB） |
| §6 索引账 | 内存 / 对象数 | `inuse_space` **成对拍**（间隔 5 分钟、跨 5 次重建） |
| §7 调度账 | 装配次数与耗时 | workspace 日志 + 装配 reason（不是 profile） |
| §8 UI 账 | 帧耗时 / DOM 行数 | RUM + Playwright baseline |

> 取数方法本身写在 [pprof 与性能读法](../runtime/pprof.md)：`alloc_space` 看反复分配，`inuse_space` 看泄漏，成对拍看"只涨不落"的那一项。

## 0. 基线

脚本化四类交互，每个 phase 一个 probe 报告，`--compare` 打印两侧与 run-to-run 波动。主分支上的中位数：

| 场景 | 数字 | 说明 |
| :--- | :--- | :--- |
| 长回答流式时的 flush commit | p95 **9.2ms** | flush 本身 0.6ms，钱在 commit |
| 4000 行 `write_file` 提交 | **19.1ms** | 超过一帧 |
| workspace 切换 | **26.5ms**（frame_max 25ms） | 最贵的单次交互 |
| 启动 hydration | 一帧 > 50ms | 只有一帧超标 |
| DOM 行数 / 挂载行 | ~300 / ≤11 | 虚拟化已生效，剩余空间不在"少挂行" |

结论直接决定了后续方向：**剩下的收益在 commit 成本与大 payload，不在减少 DOM 行**。没有这一步，后面十几轮都会优化错地方。

> 诚实的边界：跑的是 Chromium 不是 WKWebView，绝对值不是桌面 App 的数字，只有形状和 delta 可比。CI 里跳过这个 spec——构建机的时序不是基线。

## 1. 分配账：把"渲染后计数"改成"按 parts 计数"

`EstimateTokens` 定义在"消息的渲染形态"上，于是每次 sizing 都要先把每条消息 join 成字符串，再 `utf16.Encode([]rune(text))`。

| | 分配 | 耗时 |
| :--- | :--- | :--- |
| 渲染后计数（1200 条 / ~3MB 频道） | 13.4 MB / 次 | ~8 ms |
| 按 parts 计数 | ~50 KB / 次 | ~1.3 ms |

关键不是快 6 倍，而是**规则仍然只有一份**：text 与 tool result 直接从内容里数，其它形状回落 `RenderMessage`。一致性用种子语料钉住——pointer parts、文本旁的 tool activity、媒体、CJK、astral、非法 UTF-8、不是 JSON 的参数，逐 case 与渲染器对齐。

> 这类"热点路径上的短命对象"在 `alloc_space` 里排最前，在 `inuse_space` 里几乎看不到（[pprof 与性能读法](../runtime/pprof.md) §1）。

## 2. 边界账：transcript 必须有上界

问题不是"某张图太大"，而是"对话里**从来没有**上界"：单张内联图有 10 MiB 上限，但一段会话保留了它加载过的每一帧——截图密集的会话 84 帧就有 16 MiB，往回翻几轮就是几百 MB 的 WebKit Malloc，而 Go 的 gauge 一点不动。

治理方式是**单点强制**：`settleConversation` 成为唯一执行点，先消息条数、再两个字节预算（内联媒体 16 MiB + 文本 16 MiB），最新 24 条不动，最老的先让出字节，**媒体先于文本**——因为截图型会话的内存就是媒体。它挂在所有能让 transcript 变大的写路径上（流式 flush、发送、归档对账），这才叫上界，而不是"尽力而为"。

两个细节值得单记：

- **被驱逐的图片只丢字节**：附件在挂载时重新读预览，`view_image` 卡片首次展开时重新读文件，落盘的那份还在。
- **流式块用 chunk 数组测量**：检查里 `join` 一次块，就等于分配了这次检查要省下的内存。

## 3. 序列化账：checkpoint 只该等于"本轮"

图执行器每完成一个 wave 就把**整块 board** 盖进 checkpoint，store 按 run id upsert——一个长回合实测存了 **895 次**，其中一个是 **37.3 MB**（3747 条历史 + 18.8 MB 的 history 字符串）。

裁剪挂在已经属于 host 的 checkpoint store 上，产出"MainChannel = 本轮消息 + 两个计数置 0"。切片规则没有第二份：复用 `ExtractTurnMessages` 的同一函数，恢复路径对裁剪后的板再跑一次得到同一份消息——**不动点**，用测试钉住。`sessions.resume: true` 时不裁剪（resume 要用整块板给下一轮播种）。

| | 序列化 | 分配 | 耗时 |
| :--- | :--- | :--- | :--- |
| 整块 board | 37.80 MB | +54.7 MB | 352 ms |
| 裁剪后 | **0.17 MB** | **+1.0 MB** | **2.3 ms** |

## 4. 复制账：全量历史 replay 的三次重复

一个 42 MB 会话（14,041 条归档消息）、一轮对话、五分钟内 **6.78 GB** 分配（live 只有 54 MB）。开销全在"同一份 transcript 被处理三遍"：

| 环节 | 分配 | 原因 |
| :--- | :--- | :--- |
| `RenderTurn` 塞进 board var | 155 MB | 把消息序列化成 JSON 放进变量 |
| `Merge` 从变量里解回来 | ~690 MB | 同一个 var 反序列化 |
| `replayContext` 逐行解码 | 731 MB | 每轮都从存储解一遍 |
| `Turns` 物化分页 | 605 MB | 同一个会话被多处同时打开 |

对应四招：

1. **消息不过 JSON 桥**：改成按 board 索引的小型有界 registry，`Merge` 从里面取，take-once 消费——顺带避免 `[]message.Message` 被每个图快照深拷贝。
2. **前缀只渲染一次**：transcript 是 append-only，读者持有前缀时只需读追加部分（新增 `LoadAfter`）；没变就是一次 `MaxSeq` 查询。预算受限的请求不共享前缀（结果取决于预算在哪耗尽），transcript 变短则从头重渲。
3. **一页有上限**：`limit <= 0` 原本等于"读全部"，一次 ~42 MB 读还要序列化两遍到 webview。改成 clamp 到 64 轮，其余用 `beforeSeq` 翻页；每页 debug 日志带上 limit / cursor / 载入字节，重复读能在日志里看见，而不是只在 heap profile 里。
4. **一个会话同时只开一次**：双击、切换焦点重试、resume 事件会同时触发 open，改成按会话 id 合并成一次（失败可重试）。

这一节的性质都用测试钉住了：`handoff_test.go` 钉 take-once 与 var 回退，`assembly_test.go` 钉前缀缓存（没变不读、一轮读 tail、变短后重渲），再加一个"导入超过一页的会话再读回来"的绑定测试。写法见 [测试布局](testing.md) §5。

## 5. 缓存账：一次迁移的教训

先把缓存从"memory 渲染出的 items"挪到"worldstate 构造出的 messages"，理论上是"一份缓存代替两份"。测完发现是错的：

- world 节点**每轮都跑**（graph 从 world 进，循环 compact → llm → tools → steer），所以每轮 cursor 都动过，渲染缓存几乎不命中；
- 结果每轮重新解码整条 transcript：单个 turn 的 profile 里 **1527 MB** 在 `message.decodeStrict`，而 item 缓存此前只读上次之后追加的行。

于是回滚到 item 缓存 + `LoadAfter` 增量读，并保留那次顺手加的字节预算（800 万字符，跨会话，最老的先驱逐，正在读的那条即使超额也保留）。

这次判断用的是 `alloc_space` 加 `-focus message.decodeStrict`——不聚焦到那个符号，只会看到"runtime.mallocgc 排第一"（[pprof 与性能读法](../runtime/pprof.md) §6 里那几条误读）。

顺手记下"为什么不能给渲染结果做增量缓存"：`normalizeRawItems` 会丢掉"调用不在本批次里"的 tool result、给"永远没有回答的调用"补一个合成结果——单独渲染一个 delta 就得携带这套配对状态，否则会出现全量渲染里根本不存在的合成 "aborted" 结果。**那是设计改动，不是缓存微调**。

> 教训：缓存的收益取决于"输入多久变一次"。缓存"每轮都变的输入"，等于把一次便宜的重算换成了昂贵的解码。

## 6. 索引账：把嵌套 map 拍平

skills 搜索索引原来是 `map[term]map[doc]tf`（每个词一个 Go map），且桌面 `Runtime` 用一个 `map[*host.Host]marker` 记"已配置"，**从不删除条目**——每次切工作区、保存设置、装插件都把整个退役 runtime（含搜索索引、MCP schema、事件总线）钉住不放。

| | 内存 | 对象数 | 排名 |
| :--- | :--- | :--- | :--- |
| `map[term]map[doc]tf` | 0.93 MB | 9,755 | 基线 |
| 按词排序的扁平 postings | **0.41 MB** | **2,859** | 一致（31 个查询，最大分差 7e-15） |

两条测试钉住它：`TestIndexStaysFlat`（结构回退就红）、`TestRebuiltHostIsCollectable`（用 weak pointer 断言"旧 host 在重建后不可达"）。同一个 profile 还顺手修了 `grep`：先按 `stat` 退休超大文件，不再读 1 MiB 再丢弃；不再把每个文件复制成 string 再切成行（`TestGrepSkipsBinaryAndOversizedBeforeReading`、`TestScanLinesAllocatesNothing`）。

> 这类测试的共同点：**钉性质，不钉数字**。阈值会在换机器那天红，"索引是扁平的"、"旧 host 不可达"、"扫描零分配"能守几年。写法见 [测试布局](testing.md) §5。

## 7. 调度与 IPC 账

- **装配风暴**：一次 drain 里落进"保存设置 + 切工作区 + 插件写入"，会各唤醒一个 watcher、各建一份 replacement，然后关掉除第一个之外的。改成每个工作区一个延迟重建、`Acquire` 共享同一份装配，follower 直接复用 leader 的结果（连错误一起复用）；没改动内容的设置写入不再触发失效。
- **流式事件合并**：一个 token 一个 IPC 事件。现在同一流里相邻的 text/reasoning delta 按 25ms 窗口（或 64 KiB / 128 条上限）折叠成一条 `stream` 事件；**每个非 stream 事件都是顺序屏障**（先 flush），保证 turn_end / usage 不会越过它前面的文本。合并规则与窗口自己的 coalescer 一致，所以已合并的流原样通过。
- **归因**：装配路径打 `AssemblyReason`（startup / workspace_open / settings_save / plugin_change / …），日志里带 reason、耗时、是否在跑、该工作区的装配序号。没有归因，"谁在重建 runtime"永远说不清（字段设计见 [可观测性](observability.md) §1）。
- **脚本桥**：world 的 merge 改在 Go 侧完成，消息不再跨 goja 桥；compaction 节点不再用 JS 重新实现 token 估算（每个字符过一遍 goja 数 CJK：230 KB 频道 **62ms/次**、560 KB **171ms/次**，约 80 倍于原生），改成向 Go 要数——**策略留在脚本，测量归 Go**。

## 8. UI 渲染账

一个 ~10k delta、~3k 工具调用的回合会把聊天视图冻住：turn 内没有任何 memo，每个 delta 重渲所有块；每张工具卡都内联渲染完整 patch；store 无界且每个 delta commit 一次；打开会话要先把全部归档 turn 解码、排序、diff 再首绘。

现在的形态：`TurnBlock` memo 化、工具分组结果进 WeakMap、超阈值的行虚拟化、大 patch 折叠成一行（两个行数阈值 + "展开完整 diff"）、store 对每个消息的块数 / reasoning / 文本 / 工具参数分别设上限、流式文本按 100ms 节流 commit；历史分页（`Turns(id, limit, beforeSeq)`，首屏 1 页、每次点击 1 页），截断只在 turn 边界发生，`tool_result` 用 Map 按 `call_id` 索引而不是重扫。

## 9. 可迁移的六条

1. **先定基线再动手**，并把"慢在哪一相"写进脚本：没有 phase 分解的优化都是猜。
2. **按账分类**：分配 / 边界 / 序列化 / 缓存。缺哪本账，优化就会跑到别的账上去。
3. **单点强制**：上界只能有一个执行点，挂在所有写路径上，否则它是"尽力而为"。
4. **测试钉住性质**，不是钉住数字：不动点、排名一致、旧对象可回收、结构不回退。
5. **检查不能比被检查的分配还贵**（`join` 那个坑）。
6. **策略留在脚本，测量归原生**：同一个数不要有第二份实现，否则它会漂。

## 相关链接

- [Go 工程约定](conventions.md)（错误分类、可选接口、双树这些约定）
- [pprof 与性能读法](../runtime/pprof.md)（取数方法：两种视图、成对拍、分相、误读）
- [测试布局](testing.md)（怎么把上面的性质钉成测试）
- [可观测性](observability.md)（装配归因与指标）
- [多模块发布与发布工程](release.md)（基线脚本与 required check 的关系）
- [分层与扩展点](../architecture/layering.md)（board / checkpoint / assembly 这些概念来自哪一层）
- [上下文工程](../../ai/context_engineering.md)（上下文预算与计量单一归属）
