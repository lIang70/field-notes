# 分层与扩展点（Go 框架）

> 讲"一个 agent 框架怎么分层、扩展点放哪"。语言细节在 `../language/`，运行时在 `../runtime/`，并发在 `../concurrency/`。
> 案例来自 flowcraft（MIT 开源），可以直接看代码。

## TL;DR

- **框架先在业务里长形状，再抽出去**。flowcraft 2026-03 生在一个业务仓的 `go/flowcraft` 模块里，04-10 删掉 in-tree 副本改吃远端，04-14 才独立成仓。抽早了设计空转，抽晚了迁移成本翻倍。
- **扩展点只放一个位置**：`core` 定义契约，实现按 `impl:` 名字注册，装配资源按名字分发。上层只写配置，不写分支。
- **契约只描述"能问什么、能写什么"，不描述"怎么存"**；存储、向量库、图库都是实现。
- **`Scope` 这类分区键是产品决策，不是实现细节**：它同时决定隔离、计费和检索范围。
- **多 provider 的正确抽象是"部署名 + 一套协议"**——DeepSeek、GLM 骑 OpenAI 协议，于是它们是部署名，不是新模块。

## 1. 四层

```text
core       平台层：agent 执行、graph、tool、model、message、skills、inference、
           memory 契约、event bus、telemetry、workspace、sandbox、deploy、runtime、session
craft      应用装配：craft.yaml、编译期能力、插件宿主、插件贡献（tool / node / skill /
           hook / UI）、进程级 lifecycle（安装、原地更新 + 回滚快照、卸载）
driver     provider 适配：OpenAI 协议族、Anthropic Messages 族、ByteDance、MiniMax、
           DashScope 原生面、TypeSafe
backends/* 平台实现：SQLite checkpoint、长期记忆后端 + eval harness
tools/*    发布自动化：releasegate
```

`core` 自己二十多个包、十几万行量级；整仓一千四百多个 Go 文件、六百多个 `_test.go`。**这类框架契约多、实现多，没有测试密度根本不敢重构**——测试密度是分层的入场费，不是额外开销。

## 2. 契约 + 注册表 + 装配资源

```go
type ContextProvider interface {
    Recall(ctx context.Context, scope Scope, q Query) ([]Turn, error)
}

var factories = map[string]func(Params) (ContextProvider, error){}

func Register(impl string, f func(Params) (ContextProvider, error)) {
    if _, dup := factories[impl]; dup {
        panic("memory: duplicate impl " + impl) // 启动期炸，不留运行期兜底
    }
    factories[impl] = f
}
```

三件套缺一不可：

1. **契约**：`ContextProvider` / `TurnSink` / `DocumentSink` / `ContextRenderer` / `Scope` / `Turn`。
2. **装配资源**：`kind: memory.Assembly` + `impl: <name>` + 实现自有的 settings schema（配置形状归实现所有，平台不解析）。
3. **钩子**：`memory.context` / `memory.turn` 由框架编排，实现只关心自己的语义——换实现不用改调用点。

`Scope` 用 `RuntimeID + UserID + AgentID` 三分区。这条要当 API 稳定面看待：改名就是破坏性变更。

> 为什么注册冲突要 panic：配置错就该起不来。运行期兜底会把"两个实现抢同一个名字"变成一半请求走 A、一半走 B 的幽灵 bug。

## 3. 分层带来的两个副作用

**每个可选接口都要有不走它的测试**。`if m, ok := t.(ToolMetadata); ok` 这种"类型断言失败就走默认路径"的写法很好用（flowcraft `core/tool` 里就有），但会让默认路径无人覆盖——一个可选接口至少配一个不实现它的集成测试。

**"看起来很通用"的包要敢删**。flowcraft 删过泛型 actor：没人用、还占着 API 表面。

## 相关链接

- [Craft ABI 与插件宿主](craft_abi.md)（平台与 sidecar 之间的 ABI、插件宿主）
- [多模块发布与发布工程](../engineering/release.md)（这些模块怎么各自发版）
- [Go 工程约定](../engineering/conventions.md)（分层落地时的 Go 细节）
- [AI 记忆系统](../../ai/memory.md)（契约面在记忆系统里的完整实现）
