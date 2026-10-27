# pprof 与性能读法（Go 服务 / 桌面应用）

> 性能治理里最贵的错误不是"优化错了地方"，是"读错了 profile"。这篇记读法与误读。
> 具体案例（优化了哪十几轮）在 [`../engineering/optimization.md`](../engineering/optimization.md)。

## TL;DR

- **先分清 `alloc_space` 与 `inuse_space`**：前者答"总共分配了多少"（热点路径上的小对象），后者答"当下还留着多少"（泄漏）。6.78 GB 累计分配 + 54 MB live，说明是**反复分配**，不是泄漏。
- **profile 要成对拍**：间隔几分钟、跨越几次重建，A/B 看增长项，比单次 top-N 有用得多。
- **Go 的 gauge 干净不等于进程干净**：桌面应用的内存大头可能在渲染引擎（WebKit Malloc），进程族 RSS 才是那个仪表。
- **读 profile 前后都要有分相**：同一个请求里的"本地装配时间"和"provider 时间"必须能分开，否则永远在优化错的那一段。
- **`-gcflags=all=-l` 关内联会让 profile 里出现根本不存在的热门函数**。读 release 构建的 profile，别读 debug 构建的。

## 1. 两种视图

```bash
go tool pprof -inuse_space  ./app heap-1.pb.gz   # 现在占着多少
go tool pprof -alloc_space  ./app heap-1.pb.gz   # 一共分配了多少
```

- `alloc_space` 的 top 通常是"被调用了几百万次的短命对象"——优化它靠减少调用次数或改用值语义，不靠改结构体大小。
- `inuse_space` 的 top 才是泄漏候选——但要先排除"本来就该常驻"的缓存。
- 两者都别只看百分比：`-sample_index` 换视图、`-focus` 收窄到某个包，比盯总表有效。

> 对应到实战：token 计数那一轮（[`../engineering/optimization.md`](../engineering/optimization.md) §1）是 `alloc_space` 的典型；索引拍平那一轮（同文 §6）用 `inuse_space` 才看得出，因为对象数量不减、只是每词一个 map 变成了一条扁平表。

## 2. 怎么在"打包好的应用"里取 profile

桌面应用不像服务能挂 `net/http/pprof`。可行的做法是给诊断面板加一个动作：把 `runtime/pprof` 的 heap profile 写到应用数据目录，附上时间戳与版本号，然后拿同一份二进制离线分析。

```go
f, _ := os.Create(filepath.Join(dir, "heap-"+stamp+".pb.gz"))
defer f.Close()
_ = pprof.WriteHeapProfile(f)   // 与服务端 pprof 产物完全同构
```

> 关键：**profile 文件和产生它的二进制必须配对**。版本变了，符号就变了，`-inuse_space` 的归因会指到错误的函数。

## 3. 成对拍与差分

```text
heap-<t0>.pb.gz   启动 + 5 分钟空闲
heap-<t1>.pb.gz   又切换了 5 次工作区 / 重建了 5 次 runtime
```

- 用 `pprof -diff_base`，或者直接看两个文件的 top-N 差值。
- 差值里"只涨不落"的那一项才是目标：一次实测里涨的是"每次重建都新建一个搜索索引"，靠单次 profile 根本看不出来。
- 配合结构测试钉住：`TestRebuiltHostIsCollectable` 用 weak pointer 断言"旧 host 在重建后不可达"，`TestIndexStaysFlat` 断言索引结构没回退。**profile 定位、测试守门**，两者缺一不可。

> 这一对 profile 的现场：打包 App，间隔 5 分钟且跨 5 次 runtime 重建（[`../engineering/optimization.md`](../engineering/optimization.md) §6）。关键不是"每次重建都建一份索引"这个结论，而是"只有成对拍才能看到它"。

## 4. 分相：先知道慢在哪一段

- **前后端分开**：后端指标（turn 启动、装配耗时）+ 渲染器侧 RUM（首帧、帧耗时、DOM 行数）。→ [`../engineering/observability.md`](../engineering/observability.md)
- **同一请求内部再切**：在"请求离开进程"与"响应头到达"各打一个点，剩下的就是 provider 时间。没有这一刀，会把网络抖动当成自己的装配慢。
- **别用平均值**：每次优化动的是 p95，不是均值；三跑取中位数（基线的四相拆法见 [`../engineering/optimization.md`](../engineering/optimization.md) §0）。

## 5. 其它 profile

| profile | 什么时候用 | 注意 |
| :--- | :--- | :--- |
| `goroutine` | 怀疑泄漏 / 卡死 | 先看 `debug=2` 的栈，再拿 `pprof` 聚合 |
| `block` / `mutex` | 锁竞争（低 CPU 高延迟） | 采样率默认关闭：block 用 `runtime.SetBlockProfileRate`，mutex 用 `SetMutexProfileFraction` |
| `runtime/metrics` | 长期趋势（GC 周期、堆目标） | 比 profile 便宜，可常开 |
| CPU | 只有"CPU 打满"时才值得 | 大量 I/O 等待的路径上它什么都不显示 |

## 6. 常见误读

- **"live 很小所以没问题"**：live 小、分配大，说明 GC 在替你擦地，但擦地本身要 CPU，且会把 p99 拖成锯齿。
- **"Go gauge 没涨"**：桌面应用的渲染进程、GPU 缓冲不在 Go 堆里；进程族 RSS 才是那个数。
- **"top 是 runtime.mallocgc"**：那不是热点，是采样器的底层；`-focus` 到你自己的包再看。
- **"profile 说 A 慢"**：确认是不是 debug 构建（关内联）、是否开了 race（分配会放大数倍）。调试参数与 release 参数的差别见 [`../build/packaging.md`](../build/packaging.md) §2。

## 7. 一份取数清单

- [ ] 用 release 构建取 profile，版本号与符号一起记
- [ ] 至少两份，间隔里有"重复动作"（切换 / 重建 / 发送）
- [ ] `alloc_space` 与 `inuse_space` 都看，分别下结论
- [ ] 前后端各一份，同一时间窗
- [ ] 结论落成测试（结构 / 可达性 / 不动点），不落成"下次注意"

## 相关链接

- [`../engineering/optimization.md`](../engineering/optimization.md)（这些读法得出的十三条修改，以及每节的证据来源表）
- [`../engineering/observability.md`](../engineering/observability.md)（指标与归因；profile 答"分配在哪"，指标答"何时变差"）
- [`../build/packaging.md`](../build/packaging.md)（dev / release 构建参数怎么影响 profile）
- [`memory_allocator.md`](memory_allocator.md)、[`gc.md`](gc.md)（分配与 GC 的机制）
- [`../engineering/testing.md`](../engineering/testing.md)（用测试钉住性能性质）
