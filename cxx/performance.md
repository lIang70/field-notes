# Performance（性能分析）

## TL;DR

- 性能问题的工程抓手就三句话：**先 profile 再优化**、**用 sampling 看大画面**、**用 micro-bench 验证假设**。直觉和论文几乎一定错。
- Linux 上首选 `perf` + 火焰图（on-CPU / off-CPU / 差分），它能直接回答"时间花在哪"，而不只是"哪个函数被调过"。
- Micro-bench 的可信度取决于它是否**阻止了优化器**（防 DCE/常量折叠）、**隔离了微架构噪音**（缓存、分支预测、freq scaling）、并报告了**统计显著性**。Google Benchmark 是目前 C++ 生态里最省心的工程方案。
- 编译器优化级别（`-O0/-Og/-O2/-O3/-Os`）、LTO、PGO 不是单纯的"调快"，而是在**改写程序的语义边界**——同一份代码不同 `-O` 下的行为/数字不能直接比。

## 相关链接

- `compiling.md`（`-O`/`-g`/`-flto`/`-fprofile-...` 这些都属于编译选项，详见该文）
- `memory.md`（分配器、对齐、`new/delete` 行为会直接影响 cache/TLB 数字）
- `template_stl.md`（容器选型对 cache locality 的影响，是常见的"换 STL 容器就快了"的根因）
- `multi_thread.md`（线程竞争、锁开销在 perf 上的特征与单线程不同）

## 1) 两种基本视角：sampling vs instrumentation

> 先选"看哪里"，再选"怎么看"。

- **Sampling（采样）**：周期性中断程序，**记录 PC（指令地址）/调用栈**。开销低（~1%–5%），能覆盖整个程序，**统计意义上**告诉你时间花在哪。
  - 工具代表：Linux `perf`、Intel `VTune`、AMD `uProf`。
- **Instrumentation（插桩）**：在函数/分支/边界处**主动插入探针**。精确、能拿到调用次数/分支命中数等绝对计数，但**会改变程序行为**（cache、循环展开、I-cache pressure），且探针本身有开销。
  - 工具代表：`gperftools`、callgrind、`llvm-xray`、各种 trace-based profiler。

工程经验：

- 排查"系统级热点"（CPU 整体消耗、某段代码路径）→ 优先 sampling。
- 排查"调用次数/分支命中率"（验证算法假设、查找遗漏的虚函数调用）→ 选 instrumentation。
- 不要用 instrumentation 出的"指令数/分支数"直接换算时间——CPU 不是按"指令数"跑的。

## 2) Linux `perf` 速查

> `perf` 是基于 `perf_event_open(2)` 内核接口的 front-end。**采样数据是内核记录的**，不需要在进程内插桩（除了用户态栈回溯需要的 frame pointer 或 DWARF）。

常用子命令的最小可用流：

```bash
# 总体计数器：cycles、instructions、cache-misses、branch-misses、page-faults...
perf stat -e cycles,instructions,cache-misses,branch-misses,context-switches ./prog

# 记录调用栈（采样）
perf record -F 4000 -g -- ./prog          # -F 4000 = 每秒 4000 次采样；-g 记录调用栈

# 在另一个终端上观察正在运行的进程
perf record -F 4000 -g -p <PID>

# 交互式报告（按 self/children 排序）
perf report --sort=dso,symbol

# 定位到具体源码/汇编行
perf annotate <symbol>          # 汇编级热点（需 -g 编出的二进制）
```

读 `perf stat` 的最关键一条：**IPC = instructions / cycles**。

- IPC 接近 1.0+：CPU 流水线喂得很满，主要瓶颈在**前端/后端吞吐**或**依赖链**。
- IPC 远低于 0.5：通常意味着**长延迟停顿**——cache miss、TLB miss、分支 mispredict、原子指令、syscall 等待。
- 比较两个版本时，**比 IPC + cache-miss + branch-miss + cycles 一起看**，单看 cycles 会被频差/调度差带歪。

## 3) 火焰图（Flame Graph）

火焰图本质是**把采样到的调用栈在 Y 轴堆叠、X 轴按耗时比例排列**。Brendan Gregg 风格，颜色无意义，仅做"区分不同栈帧"用。

- **on-CPU 火焰图**：回答"CPU 在跑什么"。`perf record -F 4000 -g` → 用 `flamegraph.pl` 的 `stackcollapse-perf.pl | flamegraph.pl > out.svg` 即可生成。
- **off-CPU 火焰图**：回答"线程在等什么"（锁、I/O、调度）。需要 `-e sched:sched_switch` 之类的 tracepoint，再用同一组脚本生成。
- **差分火焰图（differential）**：把"优化前 / 优化后"两份火焰图叠在一起，**红 = 变慢，蓝 = 变快**。在 PGO/重构/换算法时定位 ROI 极高。

> 火焰图能告诉你"**比例**"，但不能告诉你"**绝对时间**"。把它和 `perf stat` 的总 cycles/时间对起来看，避免误读"小函数变宽了" = "优化让小函数变慢"——可能只是别的函数被优化掉了。

## 4) 怎么读 `perf` 报告里的 cache / TLB / 分支 miss

- `cache-misses` / `L1-icache-load-misses`：L1 数据/指令缓存未命中。**迭代 `std::vector` 顺序遍历**一般不会触发；但**跳着访问（指针链/链表/哈希表）**会显著上升。
- `LLC-load-misses`（Last Level Cache）：LLC（通常 L3）未命中，会拖到主存。**多核场景下更要看 LLC**，因为 LLC 是核间共享的，跨核搬运代价高。
- `dTLB-load-misses` / `iTLB-load-misses`：TLB miss → 要去页表走一趟。**大页（HugePage）减少 dTLB 压力**；大内存工作集又随机访问时尤其值得。
- `branch-misses` / `branches`：分支预测失败率。`if` 条件越不可预测，miss 率越高（典型 >5% 就开始影响 IPC）。**虚函数调用、间接调用、动态类型分发**是 C++ 里常见的"看不见的间接分支"。
- `context-switches` / `cpu-migrations`：上下文切换与核间迁移。线程频繁迁核会让 cache 全冷，热路径性能会断崖式下跌。

## 5) Micro-bench：Google Benchmark 与自旋循环

### 为什么需要专门的 micro-bench 库

自己写 `chrono::now()` 测时间几乎一定不对：

- **被测代码很可能被 DCE 掉**（结果没人用 → 编译器删掉）。
- **单次执行太短**，clock 精度/调度抖动淹没信号。
- **没有统计意义**，单次快 5% 可能是噪声。

### 最小可用 Google Benchmark 例子

```c++
#include <benchmark/benchmark.h>

static void BM_VectorPushBack(benchmark::State& state) {
    for (auto _ : state) {           // Google Benchmark 帮你跑 N 次
        std::vector<int> v;
        v.reserve(state.range(0));
        for (int i = 0; i < state.range(0); ++i) {
            v.push_back(i);
        }
        benchmark::DoNotOptimize(v); // 关键：阻止编译器把 v 当 DCE
    }
    state.SetItemsProcessed(state.iterations() * state.range(0));
    state.SetComplexityN(state.range(0));
}
BENCHMARK(BM_VectorPushBack)->Range(8, 8 << 10)->Complexity();

BENCHMARK_MAIN();
```

- `benchmark::DoNotOptimize(x)`：告诉编译器 "`x` 的值会被使用"——这是**防 DCE 的最小工具**。必要时配对 `benchmark::ClobberMemory()` 防止被测区域被提到循环外。
- 不要再用 `volatile` 假装"防优化"——`volatile` 不是优化屏障，且会污染对变量的所有访问。

### 自旋循环与"阻止优化"

> "被测代码 + 一个 sink" 是 micro-bench 的标准模式。

```c++
volatile int sink = 0;            // 全局 sink，编译器无法证明无副作用
int x = compute_something();
sink ^= x;                        // 把 x"用掉"
```

更标准的做法仍然是 `DoNotOptimize`，但理解其原理（"对编译器而言这是一个 use"）比记住 API 名字更重要。

### 统计显著性

- 跑出的 `mean ± stddev` 看起来"差不多"时，看 **CV（变异系数，stddev / mean）**：超过 ~2%–5% 的差通常不可信。
- Google Benchmark 的 `--benchmark_repetitions=...` 会重复跑整组并报告 `median` / `mean` / `stddev` / `cv`，永远开着它。
- 看 **`ns/op` 的比值**而不是**差值**；用对数坐标画图；少用"快 100ns"这种说法。

## 6) Micro-bench 常见陷阱清单

> 下面这些几乎都来自"优化器 + 微架构"对短循环的副作用，每一条都曾让资深工程师怀疑人生。

- **Dead-Code Elimination (DCE)**：结果没人用 → 编译器整段删掉。解决：`DoNotOptimize` / sink。
- **常量折叠**：`for (int i = 0; i < 10; ++i) ...` 在 `-O2` 下可能被识别为常量，循环展开甚至被消除。Micro-bench 一定要让循环上界来自运行时（state.range、随机数、time）。
- **缓存命中/未命中**：测一次 vs 测一百万次，前者可能完全在 L1 里、后者被换出。**冷/热分别测**；`std::vector` 顺序遍历 vs 随机访问，数字能差一个数量级。
- **分支预测**：可预测的 `if`（比如循环里 `if (i < N)`）会被预测器学会，代价接近 0；不可预测的（数据相关）会每次 stall 数周期。Micro-bench 别只看"快"的版本，要看"典型输入"。
- **循环展开（loop unrolling）**：编译器会按"看起来值得"展开小循环，导致两段等价代码汇编完全不同。**对比实现 A vs B 时，编译产物要先看一眼**（`objdump -d` / Compiler Explorer / `godbolt.org`）。
- **频率调节（freq scaling）**：Turbo Boost / 节能策略让 CPU 频率随负载变化。**锁频**（`cpupower frequency-set --governor performance` + 关闭 turbo）后基准才有意义；或者至少交叉运行 A/B/A/B 取最小。
- **首次执行效应**：页缺失、TLB 冷、branch predictor 冷、I-cache 冷。"第一项"和"稳态"差很多，bench 库会自动跳过前几次，但仍要意识到差异。
- **多核干扰**：同机器上其他进程/线程抢核/抢 LLC/抢内存带宽。专用机器或 `taskset` 绑核能减少噪声，但不能消除。
- **`-O3` 不总是更快**：在某些 I-cache 敏感、代码体积大的场景 `-Os` 反胜。先 profile，再决定。

## 7) 优化器视角：`-O0 / -Og / -O2 / -O3 / -Os`、LTO、PGO

> 这些选项**改的是编译器的"愿意做多大改动"**，而不是"快多少"。

- `-O0`：不做优化，**不内联**、**不省略帧指针**（perf 栈回溯最准，但代码最慢）。
- `-Og`：优化 + **保留调试体验**。**日常开发首选**：可以 step、能看变量，性能接近 `-O1`。
- `-O2`：工程上"该开的优化基本都开了"（内联、CSE、循环变换、强度削减等）。生产构建的常见基线。
- `-O3`：在 `-O2` 之上再开**激进的向量化、循环展开、函数克隆**。**对部分代码能再快 10%–30%，对部分代码反而变慢**（代码膨胀破坏 I-cache、激进的别名假设被违反等）。无脑开 `-O3` 几乎一定不是最优。
- `-Os`：优化代码**体积**。嵌入式、I-cache 紧张、热路径上的小函数多的场景可能比 `-O2/-O3` 更快。
- **`-flto`（Link-Time Optimization）**：跨翻译单元优化。**能消除未引用的虚函数调用、跨 TU 内联、做 whole-program 分析**。**配合 PGO 收益最大**。代价：链接变慢、调试更难（DWARF 体积大）、某些 sanitizer 行为有差异。
- **PGO（Profile-Guided Optimization）**：先跑一遍带 `-fprofile-generate` 的二进制收集 profile，再用 `-fprofile-use` 重新编译。**典型收益 5%–20%**，且几乎从不出负优化（因为它是用真实数据指导决策）。三步走：

  ```bash
  # 1) 编译出"会写 profile"的版本
  g++ -O2 -fprofile-generate=/tmp/pgo -o prog.inst app.cpp
  # 2) 用一组**有代表性的输入**跑一遍
  ./prog.inst < real_input_1>
  ./prog.inst < real_input_2>
  # 3) 用收集到的 profile 再编译
  g++ -O2 -fprofile-use=/tmp/pgo -o prog app.cpp
  ```

  关键点：第二步的"代表性输入"决定收益方向；用合成数据训练 PGO 是常见的"PGO 变慢"原因。

## 8) 工程建议（性能优化的常用顺序）

> "先正确、再快、再更快"。

1. **先 profile**：`perf stat` 一次知道瓶颈在 CPU / cache / 分支 / 锁哪一档。
2. **看火焰图**：定位到具体的函数/路径。
3. **改算法/数据结构**：同样的复杂度，容器选型（`vector` vs `list` vs `unordered_map`）差异常常 >10x。**这一档 ROI 最高**。
4. **再去 micro-bench 验证细节**：特定小函数/特定输入下的差异。
5. **再考虑编译选项/LTO/PGO**：在算法/结构稳定后锦上添花。
6. **永远带着"数据"说话**：微基准的 CV、火焰图的变化、`perf stat` 的 IPC 三者互证；不要用"看起来好像快了一点"做决策。
