# Craft ABI 与插件宿主

> 平台与"跑 agent 循环的 sidecar"之间的契约，以及插件模型。案例来自 flowcraft 的 `craft` 模块 + `rust/flowcraft-platformd`（MIT）。
> 分层的整体形状见 [分层与扩展点](layering.md)。

## TL;DR

- **三个接口、三个 owner**：lifecycle 归平台、capabilities 归平台（只能用不能 claim）、业务 payload 归 craft。写清楚谁拥有什么，比写清楚"支持什么"更重要。
- **grant 有租约**：启动发一次授权是不够的，`Renew` 在半 TTL 时整组替换；预算（`credit`）跟着单轮走。
- **checkpoint 故意不算平台能力**：状态归 agent 自己，"能从哪恢复"是 agent 的知识。
- **插件不是沙箱**：插件子进程跑在调用者权限下，权限模型是 API grant，不是隔离。这句话要敢写进文档。
- **不为"不存在的存量"做兼容层**。插件还没发过 tag 时，砍掉桥接比留一层"以后可能有用"的抽象便宜得多。

## 1. 三个接口，三个 owner

| 接口 | owner | 自由度 |
| :--- | :--- | :--- |
| Lifecycle（`Describe` / `Start` / `Run` / `Steer` / `Renew` / `Cancel` / `Close`） | 平台 | craft 必须精确实现，版本不匹配直接拒 |
| Capabilities（workspace / sandbox / secret / tool source…） | 平台 | craft 只能凭 grant **消费**，不能 claim |
| 业务 payload（`Run` 的输入与事件流） | craft | 有类型的 `message` / `delta`，或声明的私有扩展 |

几处值得抄的细节：

- **grant + lease**：`Start` 发放 grants，`Renew` 在半 TTL 时整组替换。授权不是一次性动作。
- **`credit` 随 `Run` 进来**：预算跟着这一轮走，平台才能做准入与限流。
- **checkpoint 不在平台能力里**：agent 拥有自己的可恢复状态，平台不该知道业务状态长什么样。
- **平台负责路由、重放、超时、审计，但不解析业务 payload**。
- **`message` / `delta` 与 tool result 共用同一 schema**：这样"对话契约"和"工具契约"不会各自漂移。
- `Describe` 不鉴权（它是平台了解对方的手段），但**必须没有副作用**。

## 2. 插件宿主

- 插件是**独立子进程**，跑在**调用者权限**下；权限模型是 **API grant**（`mcp:provide`、`skills:provide`…），**不是隔离**。craft 的文档把 "not a plugin sandbox" 直接写在"craft 不是什么"一节。
- 宿主反向暴露原语：`hostmcp` 是个绑在 `127.0.0.1` 临时端口的本地 MCP server（`/mcp`），`Craft.Start` 起、最后停。插件通过它调用宿主工具。
- **manifest 到处设上限**：hooks ≤ 16、MCP servers ≤ 16、nodes ≤ 64、`mcp.env` ≤ 32 条并对 key/value 长度有约束。无界输入在解析期就被拒。
- **权限缺失要可诊断**：`mcp` 段没有 `mcp:provide` 就不起子进程、不铸 token，日志说清原因。
- 安装 / 更新 / 卸载是一个事务面：原地更新带**回滚快照**，卸载带走插件自己的数据，`uninstall` 可续跑（带锁）。

## 3. 一次评审的结论

"要不要保留 MCP 桥接层，让纯 MCP 插件也能跑"——当时的判断依据是存量：插件没发过 tag，`craft/v*` 一个都没有，存量为零。于是桥接被砍掉，而不是留成抽象。

> 这类判断的关键不是"哪个设计更优雅"，而是**存量有多少、要付多久**。存量是零的时候，任何兼容层都是纯负债。

## 4. 反面清单

- 让插件 claim 平台能力（自己声明"我有 sandbox"）——信任边界立刻消失。
- 把 lease 做成"永不过期"：一次授权泄漏就是永久授权。
- 让插件直接继承宿主 env：`~/.ssh` 那类东西会被顺手读走。
- 用 prompt 约束插件行为，而不是用 grant 限制它能拿到什么。

## 相关链接

- [分层与扩展点](layering.md)（四层与扩展点）
- [多模块发布与发布工程](../engineering/release.md)（`craft` 与 `core` 的发布顺序）
- [沙箱与进程隔离](../../os/sandbox.md)（真正的隔离在平台层怎么落地）
- [工具使用](../../ai/tools.md)（MCP 协议本身的形态）
