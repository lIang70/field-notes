# Go 工程约定（分层项目里的细节清单）

> 前面两篇讲结构（[分层与扩展点](../architecture/layering.md)、[Craft ABI 与插件宿主](../architecture/craft_abi.md)），这篇只记"写代码时反复用到的那些决定"。
> 都是踩过之后固化的，不是风格偏好。

## TL;DR

- **错误分类走 `errdefs`**：`IsXxx(err)` 谓词判类、`Xxx(err)` 构造函数包类；上层按类决定重试还是放弃，构造函数必须留在链上的 wrapped error。
- **panic 恢复放在每个扇出边界**，只在顶层 recover 会丢掉一半上下文。
- **注册冲突显式处理**：`Register` 返回 `Conflict` 错误，`MustRegister` 才 panic；装配期把前者当启动失败。
- **并发正确性用测试钉住**（race 报告 → 修复 → 测试），不靠 review 眼神。
- **`go.work` 看仓库形态**：多模块仓（flowcraft）把它当工作区入口提交；单模块仓只在本地临时用、不进版本库。
- **同一份配置有两套实现（Go + Rust）时，只改一边比留着缺陷更危险**——bug 会变成"换个平台不复现"。

## 1. 错误

```go
// 分类看谓词，不看字符串；context.Canceled 由 errdefs 归到 Aborted
switch {
case errdefs.IsNotAvailable(err): // 后端不支持 → 降级或换路
case errdefs.IsConflict(err):     // 并发冲突 → 重试
case errdefs.IsInterrupted(err):  // 上层取消 / 中断 → 直接返回
}
```

- 包错误用构造函数：`errdefs.NotFound(err)`、`errdefs.Conflictf(...)`、`errdefs.NotAvailable(err)`——它们把原始 error 留在链上，谓词才能 `errors.As` 出来。
- 不要用字符串匹配判语义；也不要为了"上下文更清楚"把 error 拍平成 `fmt.Errorf("%s", err)`。

## 2. panic 与恢复

- **每个 goroutine 扇出点单独 recover**：只在 `main` 或 HTTP 中间件顶层 recover，堆栈已经只剩"某个请求挂了"。
- 恢复之后要带出上下文（谁调的、什么参数、跑到第几步），否则日志等于没写。

## 3. 可选接口

```go
if asm, ok := impl.(ContextAssembler); ok {
    return asm.Assemble(ctx, req)
}
return defaultAssemble(ctx, req)
```

- 适合"想加钩子又不破坏现有实现"。
- 代价：默认路径没人覆盖。**每个可选接口至少配一个不实现它的集成测试**。

## 4. 并发

- 竞态用测试钉：pty 的 `Resize` 与 `reap/Close` 序列化、`Close` 与事件 reloader 的竞态——都是先有 race 报告，再补修复 + 回归测试。
- 共享状态优先用"快照 + revision"而不是加锁读：读写少冲突的路径上，锁会先成为延迟瓶颈。

## 5. 仓库与依赖

- **`go.work` 分成两个结论**：多模块仓（flowcraft 那种十几个 module）把它当工作区入口**提交**，CI 才能整仓构建；单模块仓（opencraft）只需要 `go.work.sum`，本地联调用 `replace` 临时加、提交前删。
- **"看起来很通用"的包要敢删**：泛型 actor、无人使用的 helper 层，占着 API 表面就是负债。
- 多模块的发布顺序见 [多模块发布与发布工程](release.md)。

## 6. 双树（Go + Rust 同语义）

同一份 deploy 文档有两套实现时：

- 改一条缺陷，**先找另一棵树里同构的代码**，在改动说明里写出双树步骤。
- 同一份配置在两条路径上语义不同，比明摆着的缺陷更难查——它只在特定平台复现。
- 跨平台测试要能在 CI 里跑（Windows lane），否则第二棵树永远是"理论上支持"。

## 7. 一份可以直接抄的 self-review 清单

- [ ] 新注册点：`Register` 的重名返回 `Conflict`（不是 panic），`MustRegister` 只用于 init 期已知良好集合
- [ ] 错误用构造函数据类，谓词能用 `errors.As` 判出来
- [ ] 新 goroutine 有 recover、有 ctx、有退出条件
- [ ] 新接口若可选，有不实现它的测试
- [ ] 新配置项：非法值在启动期报错，不是静默退化
- [ ] 涉及平台差异：双树是否同修，CI lane 是否覆盖

## 相关链接

- [分层与扩展点](../architecture/layering.md)（结构从哪来）
- [多模块发布与发布工程](release.md)（多模块发布）
- [测试布局](testing.md)（上面那些约定怎么被测试守住）
- [可观测性](observability.md)（错误与原因枚举怎么进日志）
- [桌面 agent 的性能治理](optimization.md)（这些约定在性能治理里怎么体现）
- [Interface & Reflection](../language/interface_reflection.md)
