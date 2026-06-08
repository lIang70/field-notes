# 沙箱与权限执行 (Sandbox & Permission Enforcement)

> 本篇不重复 [`../agent.md`](../agent.md) 模板里"安全与权限边界"的 prompt 写法，也不写 tool 协议本身（那是 [`../tools.md`](../tools.md) 的事）。本篇聚焦 harness 怎么把 policy 文本**真的落地**为运行时约束——deny-list、HITL 触发、强制 sandbox、资源 quota。

## TL;DR

- **Sandbox 的核心语义是"默认拒绝 + 显式 allow"**，不是"加个 Docker 装上就安全"。装上而不配置 = 默认放行，危害比不装还大。
- **Agent runtime 和 tool runtime 是两回事**。Agent 容器里的 `~/.ssh/` / `~/.aws/` 默认被 tool 继承是经典事故；harness 必须把"工具的执行环境"和"agent 的运行环境"显式切开。
- 资源限制分两层：**单次 tool 调用**（CPU / 内存 / fd / wall-clock，syscall / cgroup 层）和**整个 session 生命周期**（总 quota、cost、步数，harness 状态机层）。
- 网络策略最容易"装了一半"——**DNS 必须显式处理**：只封 IP 没用，agent 用 `getent hosts` 就绕过；egress 过滤必须在 DNS 解析前生效。
- 权限 policy（"高风险动作要二次确认"）必须在 **harness 层强制**，不能只写在 prompt。模型照删不误——harness 要先 deny、再由 UI 弹确认、再放行。
- 反直觉：**sandbox 不是越多越安全**。每多一层隔离，agent 工具能力就少一截；过度 sandbox 会让模型退化到"只能做无害的事"，再无任务价值。

## 1. 文件系统隔离

把 agent 能看到的文件系统显式分层，每层有"读 / 写 / 执行"的边界：

| 层                     | 内容                                   | 默认权限               | 典型风险                           |
| :--------------------- | :------------------------------------- | :--------------------- | :--------------------------------- |
| **working dir**        | 任务相关目录（cwd、用户指定 repo）     | 读 + 写 + 执行（受限） | 路径穿越（`../../etc/passwd`）     |
| **scratch / 临时目录** | `/tmp` 或 harness 私有 tmpfs           | 读 + 写，执行受限      | 符号链接逃逸到 working dir 之外    |
| **持久化目录**         | session 结束仍保留（artifacts / 缓存） | 读 + 写                | 跨 session 残留敏感数据            |
| **挂载的只读视图**     | 工具需要的二进制 / 库（Python、Node）  | 读 + 执行              | 被写入污染（`site-packages` 被改） |

> **关键边界**：working dir 是 agent"以为自己拥有的世界"；持久化目录是"agent 留下来的东西"。两者必须**物理隔离**——一个 session 写坏 working dir 不能影响历史 session 的 artifacts。

### 1.1 读 / 写 / 执行的边界

> 写权限 ≠ 执行权限。`chmod -R 777` 是反例。

- **读**：列出文件、读内容——所有层都应可读（按需）。
- **写**：scratch / working dir 可写；持久化目录**仅限 export** 时可写；挂载的只读视图**绝不写**（mount `ro`）。
- **执行**：working dir 里的脚本**默认禁止执行**——让 agent 调"解释器 + 文件路径"（`python script.py`）而不是 `./script.sh`，借助解释器自身的语法做参数约束。

### 1.2 路径穿越与符号链接

> 经典绕过：agent 写 `safe.txt` 是符号链接，指向 `/etc/shadow`；harness 检查"文件名是否在 allowlist"后照样被 open。

- **canonicalize 后再校验**：`os.path.realpath` / Go `filepath.EvalSymlinks`，否则符号链接是裸奔。
- **不信任相对路径**：所有 tool 入参的路径先转绝对路径再 join 到 working dir。
- **`/proc/self/root` 等内核级逃逸路径** 要进 deny-list。

## 2. 网络策略

### 2.1 三种模式

- **白名单**（默认拒绝 + 显式 allow）：最严。Agent 只能访问配置里允许的域名（`api.openai.com` / `*.anthropic.com`）。适合"我知道 agent 应该访问什么"。
- **黑名单**（默认放行 + 显式 deny）：松。Block 内网 IP / 已知 C2 域名 / 隐私数据外发端点。适合"我不确定 agent 要什么，但绝不能让它干 X"。
- **离线模式**（默认拒绝所有 egress）：最严。Tool 完全不能联网，只靠本地资源（filesystem / 缓存）。Code interpreter 跑不可信代码时是必选。

> **反直觉**：白名单比黑名单**实际更简单**。黑名单永远列不完（你不知道攻击者会用哪个新域名），白名单的"遗漏"是失败安全（deny by default），黑名单的"遗漏"是失败不安全（allow by default）。

### 2.2 DNS 容易被漏

> 很多 sandbox 只在 iptables / nftables 拦 IP 层，agent 用 `getent hosts attacker.com` 拿到 IP 再直连，**直接绕过**。

正确做法：

- **egress 过滤在 DNS 解析前**：DNS 走代理 / 内网 resolver，配合 RPZ 过滤。
- **拦截所有出站 53/udp**（DNS 协议），只放行内网 resolver。
- **解析后回查 IP 是否在白名单**：防 DNS rebinding（解析到合法 IP，复用连接时换绑到 `localhost`）。

### 2.3 egress 的实现层级

- **进程内**（Go `net.Dial` 拦截 / Python `socket` 包装）：易实现、只挡 agent 自身代码，挡不了子进程。
- **OS 层**（nftables / eBPF / network namespace）：能挡子进程；需要 root / 容器能力。
- **Sidecar 代理**（egress proxy + mTLS）：能挡所有 egress 并审计；延迟 +1 跳。

> 实践：**OS 层兜底 + 进程内 allowlist 提示**。前者挡住"忘记配的子进程"，后者给 agent 更明确的错误（"你不能访问 X" vs "connection refused"）。

## 3. sub-process 边界

Agent 调 `shell.run("python train.py")` 时，harness 至少要在 4 个维度对子进程设**硬**限制：

| 资源           | 限制方式                                    | 典型值             |
| :------------- | :------------------------------------------ | :----------------- |
| **CPU**        | cgroup `cpu.max` / `taskset` / `RLIMIT_CPU` | 1-2 核 / 调用      |
| **内存**       | cgroup `memory.max`                         | 512MB - 4GB / 调用 |
| **fd**         | `prlimit(RLIMIT_NOFILE)`                    | 64-1024            |
| **wall-clock** | `alarm` / `RLIMIT_CPU` / 监控进程 SIGKILL   | 30s - 10min / 调用 |

> **常见反模式**：只设 wall-clock。模型写的死循环 CPU 100% 跑满 5 分钟才被 kill，期间把同机其他租户挤爆。**CPU 限制必须有**——`RLIMIT_CPU` 是软的（按 CPU 秒），cgroup 是硬的（按核 × 比例）。

### 3.1 为什么要单独管子进程

Agent 自身是常驻进程；它调 `Bash` tool 时，工具运行器 `fork+exec` 出子进程。子进程**默认继承父进程的 capability / namespace / fd / env**——这就是"工具 runtime 不同于 agent runtime"必须显式切开的原因。

- 子进程的 **cwd**：应是 tool sandbox 的根，不是 agent 的 cwd。
- 子进程的 **env**：剔除 `*_TOKEN` / `*_KEY` / `AWS_*` / `GCP_*` / `KUBE_CONFIG` 等敏感变量——除非 tool 显式声明需要。
- 子进程的 **uid/gid**：应是 sandbox 专用账户（`nobody` / `sandbox-uid`），不是 agent 主用户。

## 4. 权限 policy 的运行时落地

[`../agent.md`](../agent.md) 模板里"安全与权限边界"那一段是 **policy 文本**——模型读了会"尽量遵守"，但**没有强制力**。Harness 必须把它**翻译成**可在运行时 enforce 的规则。

### 4.1 三层强制

- **Deny-list（默认拒绝）**：危险命令（`rm -rf /`、`mkfs`、`dd if=...`、写 `~/.ssh/`、外发邮件命令）在 tool 层直接拒绝，返回结构化错误。
- **二次确认（HITL interrupt）**：高风险动作（删数据 / 付费 / 对外发请求 / 改 prod 配置）由 harness **中断** agent、弹 UI 给用户确认；用户批了再继续，**批的状态写在 trace 里**便于审计。
- **强制 sandbox（escape hatch）**：所有 tool 执行必须先经 sandbox 包装层——`unshare` / cgroup / seccomp / firejail，模型**拿不到绕过入口**。

> **关键**：sandbox 必须是**路径必经的**。如果模型可以"调用 native tool 跳过 sandbox"，policy 就是空话。[`../tools.md`](../tools.md) 里"工具 spec 怎么设计"必须和"工具执行怎么被 harness 包"对齐。

### 4.2 静态 vs 动态决策

| 决策点         | 静态（编译/启动时决定）  | 动态（每调用检查）        |
| :------------- | :----------------------- | :------------------------ |
| **deny-list**  | 启动时从 config 加载     | tool 入参正则匹配         |
| **HITL 触发**  | 哪些 action class 要确认 | 实际调用时判定 + 风险评分 |
| **资源 limit** | 默认值 + 工具维度覆盖    | cgroup 实时写             |
| **凭证范围**   | tool 绑定哪些凭证        | 调前临时取，过期销毁      |

> 动态决策需要 **"risk score"**：把"动作 × 目标 × 历史"合成一个分数，到阈值才进 HITL。**对所有写操作都弹确认**会养成"用户无脑点确认"的肌肉记忆——**比不弹更危险**。

## 5. 资源 quota

与"单次 tool 调用的限制"（§3）不同，quota 是 **session / agent 生命周期**的总量限制：

- **总 wall-clock**：单 session 上限（30min - 数小时）。
- **总 token / cost**：美元上限 + 单步上限。
- **总 tool 调用次数**：防"工具循环空转烧钱"。
- **总网络 egress bytes**：防"上传整个 repo 到外网"。
- **总文件写字节数 / 文件数**：防"误操作或注入导致大量写"。

> 配额必须是**累加的、有上限的、超额就 fail-fast 的**。Soft warning 在 agent 场景基本无效——agent 不会"读警告然后改行为"，它只把警告当噪声继续跑。

### 5.1 quota 落地的最小集合

```text
session_start() -> quota = {
  wall_clock:       30m,
  llm_cost_usd:     1.0,
  tool_calls:       200,
  egress_bytes:     50MB,
  fs_write_bytes:   100MB,
}
every_step() -> {
  if (elapsed > wall_clock)   -> abort(session, "wall_clock")
  if (cost  > llm_cost_usd)   -> abort(session, "cost")
  if (tool_calls > N)         -> abort(session, "tool_calls")
  if (egress > bytes)         -> abort(session, "egress")
  ... // 不存在 "soft warning"
}
```

## 6. 工具 runtime ≠ agent runtime

最容易踩的坑：把 agent 跑在容器 / VM 里就觉得"安全"了。**但 agent 调的工具**（尤其是 `Bash` / `code_execution`）如果允许在 host 上跑、或在 agent 容器里跑、且共享 filesystem / network，agent 仍然"等于"宿主机用户。

正确的分层：

```text
[user] -> [host harness] -> [agent container]
                              |
                              |  tool call
                              v
                        [tool sandbox]   <- 每次 tool 调用 new 一个
                        - private fs
                        - network namespace
                        - cgroup limits
                        - ephemeral uid
```

> **反直觉**：tool sandbox 通常**比 agent 容器更严**。Agent 容器是"agent 长期工作的地方"，允许装包、缓存、写自己的 state；tool sandbox 是"每次新开的、销毁的、最小化的"。

- 工具 sandbox 用 **ephemeral**（每次调用新创建、结束后销毁）。
- Agent 容器**不直接调原生 shell**——它的 shell tool 是个 stub，真正执行转发到 tool sandbox。
- 工具的 stdout / stderr / 文件产物**只回传必要的部分**给 agent（如返回文件 ID + summary，而不是把文件直接送回 agent 容器 fs）。

## 7. 常见反模式

- **"开了 sandbox 就行"**：sandbox 没配 deny-list、没限制 egress、没切 uid——agent 进去等于进了个有色的盒子，盒子没盖。
- **只封 IP 不管 DNS**：agent 用 DNS rebinding / 自带 resolver 直连。
- **"agent 容器 = tool 容器"**：agent 自己的 state 被 tool 误改、agent 的凭证被 tool 读走。
- **policy 只写在 prompt**：模型没遵守时 harness 不知道——也没法阻断。
- **soft warning 代替 hard limit**："你快超过 quota 了"对 agent 是噪声。
- **跨 session 共享 sandbox 状态**：session A 写的文件 session B 能看到，攻击面扩散。
- **凭证跟着 agent 走**：harness 把 `*_TOKEN` 全塞进 agent 容器环境变量，agent 调 tool 时 tool 也看得见——应按 tool 维度**临时注入，调用完销毁**。

## 8. 上线前自检

- 默认拒绝？所有 egress / exec / 写都从 deny 出发？
- 文件层 4 类（working / scratch / 持久 / 只读）物理隔离 + 权限明确？
- 网络层 DNS 显式处理 + 拦截出站 53？
- 子进程有 CPU / 内存 / fd / wall-clock 四件套硬限制？
- 工具 sandbox 是 ephemeral、agent 容器 ≠ tool 容器？
- 高风险动作由 harness 强制 HITL，prompt 不参与决策？
- 配额累加、硬上限、超额 fail-fast？
- 凭证按 tool 维度临时注入？

## 相关链接

- [`../agent.md`](../agent.md) — 权限 policy 在 prompt 模板里的写法（harness 把这些 policy 翻译成运行时规则）
- [`../tools.md`](../tools.md) — tool 协议本身，以及 spec 怎么描述 sandbox 约束
- [`./control_loop.md`](./control_loop.md) — 控制循环每一步都跑在 sandbox 之内
- [`./lifecycle.md`](./lifecycle.md) — 进程级隔离（子 agent / 子进程何时 spawn、何时销毁）
- [`../context_engineering.md`](../context_engineering.md) — 敏感数据在 sandbox 边界怎么处理（哪些进 context、哪些只是工具参数）
