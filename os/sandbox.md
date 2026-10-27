# 沙箱与进程隔离（实现层）

> 讲机制怎么落地，不重复 policy 怎么写（那在 [`../ai/harness/sandbox.md`](../ai/harness/sandbox.md)），也不讲 namespace/cgroup 原理（那在 [`virtualization.md`](virtualization.md)）。
> 案例来自 flowcraft 的 `core/sandbox` 与 `rust/`、opencraft 的 `execd`。

## TL;DR

- **Runner 是"进程会话管理器"**，一次性 `Exec` 只是 `Start` 的派生视图；超时、取消、回收因此只有一个实现点。
- **执行不了的策略必须报错，不能降级**。降级过的沙箱比没有沙箱更糟，用户会以为自己被保护了。
- **白名单判的是字符串，执行看的是环境**（`PATH`、shell wrapper、pager 环境变量）。中间少一步"字符串 → 真要执行的 inode"，就会被绕过。
- **Windows 上 `chmod` 只是建议值**，真正的边界是 Job Object、Low-integrity token 和创建时继承的 ACL。

## 1. Runner 契约

```go
type Runner interface {
    Close() error
    Capabilities() Capabilities
    Start(ctx context.Context, spec SessionSpec) (Session, error)
    List(ctx context.Context) ([]SessionInfo, error)
    Terminate(ctx context.Context, id string) error
}
```

`Exec(ctx, runner, cmd, args, opts)` 建在 `Start` 上，不是第二条执行路径。`Capabilities()` 回答"这个后端能不能给 TTY、能不能拦网络、能不能限内存"，上层不用靠平台名猜。

`Close` 要释放 runner 拥有的一切：会话、文件 journal、后端状态。包装器（`AllowCommands` / `WithApproval` / `WithDefaults`）必须把 `Close` 和 journal 一起转发，否则包装会把后端藏掉。

## 2. 平台矩阵

| 后端 | 平台 | 隔离 | 网络 | 写策略 | 资源上限 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `local` | 全平台 | 无 | 仅默认 | 只支持 `WriteWorkspace`；`WriteReadOnly` 直接返回 `NotAvailable` | 组 watcher（unix） |
| `bwrap` | Linux | user/mount/pid/net ns | 默认/全拒/allow-list/代理 | root + 可写路径，可 per-call 只读 | watcher |
| `seatbelt` | macOS | SBPL profile | 同上 | 同上 | watcher |
| `windows` | Windows | Job Object + 可选 Low-integrity token + AppContainer | 同上 | 同上（需开 write confinement） | job limits |

配置面也各有取舍：bwrap 的 `extra_flags` 会拒绝可能削弱策略的 flag（`--ro-bind`、`--args`）；`readonly_root: true` 与指向 root 的 `writable_paths` 冲突时构建期报错，而不是悄悄丢掉。

## 3. 三处容易写错的语义

**EnvPolicy 有三态**，不是两态：

```go
Allow == nil        // 继承整个宿主环境
Allow == []string{} // 什么都不继承，子进程只见 Inject
Allow == ["PATH"]   // 只转发这几个
```

Go 里 `nil` 和空切片混着用，等于把"没有策略"和"空白名单"当成一回事。到了 wire 层，这就是 fail-open。

**Stdin 是声明，不是数据。** 非 TTY 会话如果没声明 `SessionSpec.Stdin`，子进程 fd 0 停在 `/dev/null`；此时 `Write` 返回 `NotAvailable`，不静默缓冲。原因是 `rg`（无路径参数）、`cat`、`test -p /dev/fd/0` 这类工具遇到"永不结束的空管道"会挂住，读到 `/dev/null` 才会正常回退到参数。

**WritePolicy 只能收紧。** 只有 `WriteWorkspace`（零值）和 `WriteReadOnly` 两个取值，故意没有"更宽"的档位；未知值左右两侧都保留原样，交给后端校验拒绝，不退化成宽档。

## 4. 命令分类：字符串判不出要跑什么

同一个根因有三种表现：`allow_commands` 只比 basename（`/tmp/go` 满足 `go *`）；grant 范围被请求里的 `inject.PATH` 绕过；只读分类为真但没硬化环境，`LESSOPEN` / `GIT_PAGER` / `MANPAGER` 让 pager 执行了任意命令。

补法不是给三处各打一个洞，而是在所有策略层之前加一个阶段：

```text
plan(SessionSpec, HostPolicy) -> PlannedCommand
```

allow-list 比 `program: PathBuf`，grant 比 canonical 路径，只读分类比 `program` + 有效 env。配套两条：

- 只读分类一旦成立，同一层必须同时改写 env/argv 去掉执行开关。只有分类没有硬化，本质还是字符串检查。
- 程序解析只用 host 侧白名单目录，请求里的 `inject.PATH` 只影响子进程后续行为，不参与判定。

allow-list 本身要认命：它是个 tripwire，不是墙。`NormaliseExec` 只在能证明是单条简单命令时解包 `sh -c` / `cmd /c` / `pwsh -Command`，其余一律落到人工审批。

## 5. Windows

- Job Object 管生命周期与资源：超时、取消、关闭都终止整棵进程树，内存和 CPU 由内核记账。
- 写约束是 opt-in 的 Low-integrity token，只有 runner root 和显式 `writable_paths` 可写，读全开。
- 网络策略跑在 AppContainer token 下。OS 防火墙的 AppIsolation 默认挡 TCP 和 connected 流，但不约束 unconnected UDP / ICMP，所以还要按包 SID 装 WFP 的 bind 层过滤器：`NetDenyAll` 拦所有 bind，`NetAllowList` / `NetProxy` 只放 TCP bind，并把容器钉到宿主侧 proxy、注入 proxy 环境。
- 这些模式需要 elevated host，否则 fail closed 返回 `NotAvailable`，不会静默放行。
- 交互会话走 ConPTY：stdout/stderr 合并成单一 TTY 流，`Resize` 作用在伪控制台上。TTY 和 write confinement / net policy 目前不能组合。
- `chmod` 在 Windows 上只映射到只读属性，真正的访问控制是创建时继承的目录 ACL。传 `0o600` 不会对同机其他用户隐藏文件，把它当意图就行，别当边界。

## 6. 文件 journal

只记写类操作，四类事件：

```text
create  路径出现在边界内（创建并写入一次就是 create，不是 create+write）
write   已存在路径的内容变了
rename  能配对时折叠成一个事件，OldPath 记住来源
remove  删除，消费者据此退役一个 chip
```

`chmod`、`utimes`、读都不算事件，否则"文件被看过"会变成"文件被产出"。监听的是 runner 级边界，per-call 的 `WriteReadOnly` 不重新注册监听，只是让这次调用写不进去。平台给不了 journal 时要在构建期失败（严格解码），不能忽略。

## 7. 审批和提权是两件事

approval 决定"能不能跑"，它从不放宽命令运行时的策略。所以"批准过的命令仍被 OS 拒绝"是正常结果。

escalation 是针对这种情况的第二个问题：这一次允许越狱吗？提示里有两个字段要分开：`Command` 是渲染行（按 POSIX shell 回读的方式加引号，`sh -c 'pip install x'` 原样展示），`Rule` 是真正写进规则库的归一化前缀。shell 包装过的调用两者不同，只有 `Rule` 进规则库。

## 8. 一次系统评审留下的缺陷类

把一年多零散修的东西归类，比记单个 bug 有用：

| 类 | 症状 | 修法 |
| :--- | :--- | :--- |
| check-then-use | `symlink_metadata` → `canonicalize` → `read`，中间换掉父目录就能读出 root 外的文件 | 换 capability handle（`openat2` + `RESOLVE_BENEATH`），一次系统调用定边界 |
| 先创建后 chmod | `fs::write` 再 `set_permissions(0600)`，中间有可读窗口 | `OpenOptions::mode(0o600).create_new(true)` |
| 规则没有单一归属 | `platform.yaml` 校验 dns ⇒ TLS，但 capability 的 settings 走 serde 直解，同一规则漏一处 | 规则写进配置解析期校验，不靠各模块自觉 |
| 有界容器缺一半 | log ring 有上限，session registry / proxy 连接数 / 撤销表没有 | 上限 + 回收 + 观测；只加上限会变成 DoS 型拒绝 |
| async 里做阻塞 I/O | `async fn` 里调 `std::fs::*` / `to_socket_addrs` / `thread::sleep` | `spawn_blocking` 包一层；门禁目前是 CI 的 `cargo clippy -D warnings` + review，`disallowed-methods` 还在计划里 |
| 逐层深拷贝 | catalog 每次深拷贝、tool search 每次重建索引、session log 每 chunk 拷两次 | `Arc` 共享 + 单调 revision，消费方 `ptr_eq` 判快照变化 |

另外两条踩过的：proto3 的"字段缺失 = 继承"是 fail-open 编码，区分不了"没有策略"和"空白名单"，翻默认值必须同时改 wire 字段，否则老 peer 能读回 fail-open；Go 和 Rust 两套实现必须同修，只改一边比保留缺陷更危险——同一份配置在两条路径上语义不同，bug 就变成"换个平台不复现"。

## 9. Rust 平台层

`cap-sandbox` 管会话全表面（一次性视图、policy chain、approval、escalation、cgroup 上限、pty、dial 前的网络判定、文件 journal、退出分类）；`cap-workspace` 管受限读取，每个拒绝带调用方可以处理的 code；`platformd` 把 craft ABI 从 unix socket 扩到 TCP + TLS，并支持 Windows 进程树、重载时排空退役代。

布局从 11 个 layer crate 塌成 2 个 package（`flowcraft-win32`、`tools/proto-gen` 另算），wire 生成器移出发布 crate，依赖方向做成 CI gate。

> 重写的收益不在性能：漏写 `#[cfg(unix)]` 会编译失败，而不是在 Windows 上静默跑错。

## 10. 踩坑

- AppContainer 只能在建进程时给，建完再打 token 是错的。
- cap 是否用尽要问内核装了什么限制，别按配置推算。
- pty 的 fd 继承要断言继承本身，不是断言 pty 存在。
- keychain 后端按路径键，不按 inode 号（inode 复用，测试会飘）。
- 测试假设 `HOME` 存在，在 Windows lane 上会成片红。
- 隔离面没有双平台 CI lane 就会回归。Windows 压力 lane 的 `checkptr` 就是回归逼出来的。

## 相关链接

- [`../ai/harness/sandbox.md`](../ai/harness/sandbox.md)（policy 侧：deny-list、HITL、quota 分层）
- [`virtualization.md`](virtualization.md)（namespace / cgroup 原理）
- [`process.md`](process.md)（进程创建、job object、回收）
- [`network_stack.md`](network_stack.md)（egress / DNS / 代理）
- [`../go/architecture/craft_abi.md`](../go/architecture/craft_abi.md)（同一套沙箱在 Go 侧的分层与 ABI）
