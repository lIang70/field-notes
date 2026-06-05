# 上下文工程 (Context Engineering)

## TL;DR

- **CE = 决定"塞进窗口里那一坨信息"的工程学**，是 PE 的上位概念：PE 关心"写好一句话"（几十～几千 token），CE 关心"用好整个窗口 + 窗口外的一切"（1K–1M token）。
- **Context 是有限资源**：Chroma 在 18 个 LLM 上的 [context rot 实证](https://www.trychroma.com/research/context-rot) 说明随 token 增长，模型关注度**非均匀**下降——长度增加 ≠ 质量增加。
- **三条主线**：(1) **预算**——窗口大小 × 单价 × 缓存折扣；(2) **装配**——静态/动态/外部三层 + just-in-time retrieval；(3) **节奏**——compaction、锚定（首尾优先）、缓存（prefix 命中）、sub-agent 隔离。
- **核心原则**：just-in-time 优于 just-in-case；最小高信号 token 集合优于堆料；失忆保护靠"显式锚到外部 state"而非"塞进 prompt"。
- **反直觉**：长上下文不是答案；中段信息关注度低（Lost-in-the-Middle）；把"模型刚吐出来的中间思考"原样塞回会污染 context；缓存前缀混入"每请求变"的内容**永远 miss**。

> **本篇边界**：全局 context 设计（什么进窗口、什么不进、如何滚动、如何持久化、如何计费）。**不重复** `prompt_engineering.md` 的"长上下文技巧"一节——那一节讲的是"长 prompt 内部怎么排版"（锚定、分段标签、`[[id]]` 引用、缓存友好布局），本篇讲的是"窗口里外各放什么"。

## Context Engineering vs Prompt Engineering

| 维度         | Prompt Engineering (PE)            | Context Engineering (CE)                                                        |
| :----------- | :--------------------------------- | :------------------------------------------------------------------------------ |
| 关注对象     | 一条指令 / 一段对话模板            | 装进窗口的全部 token：系统指令 + 工具 + 检索结果 + 对话历史 + memory + 工具输出 |
| 尺度         | 几十～几千 token                   | 1K～1M token                                                                    |
| 优化手段     | 措辞、示例、CoT、ReAct、结构化输出 | 压缩、检索、缓存、compaction、sub-agent、tool result clearing、memory paging    |
| 失败模式     | 指令模糊、样本偏好、注入           | context rot、lost-in-the-middle、cache miss、context pollution、token 超限      |
| 典型工程动作 | 改 prompt 模板                     | 改装配管线、改检索策略、改缓存布局、改 agent 拓扑                               |

> **CE 是 PE 的超集**：长上下文 agent 场景里，PE 的边际收益迅速饱和——把 prompt 写漂亮只能让"那 1K 指令"更稳；剩下 99% 的 token 怎么装、怎么管，是 CE 的事。Anthropic 把它定位为 PE 的 [natural evolution](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)：从"craft instructions"升级到"curate tokens across system instructions, tools, external data, and message history"。

## Context 预算心智模型

> 把 context 想象成"带单价 + 缓存折扣的 RAM"——每一类内容都有成本，都有机会被压缩或缓存。

### 窗口大小

| 区间     | 典型模型                    | 适用场景                                         |
| :------- | :-------------------------- | :----------------------------------------------- |
| 4K–8K    | 旧模型 / 边缘部署           | 单轮简单任务                                     |
| 32K–128K | 主流大模型                  | 中等长文档、多轮对话                             |
| 200K     | Claude Sonnet 4 / 4.5       | 长文档分析、agent 中等任务                       |
| 1M+      | Claude Opus 4.x、Gemini 1.5 | 长视频/长代码库——但**长 ≠ 稳**（见 context rot） |

### 单价与计费

输入 / 缓存写 / 缓存读 / 输出是**四种不同单价**。以 Claude 3.5 Sonnet（2024 年参考价）为例：

| 项          | 单价（USD / MTok） | 倍率     |
| :---------- | :----------------- | :------- |
| Base input  | 3                  | 1×       |
| Cache write | 3.75               | 1.25×    |
| Cache read  | 0.30               | **0.1×** |
| Output      | 15                 | 5×       |

> **关键经济学**：缓存读是 base 的 1/10。"稳定的静态前缀"放进 cache 是直接省钱——但前提是它**真的稳定**（见缓存反模式）。

### 什么时候"长上下文"不是答案

- **成本**：1M token × 单价 × N 次调用 = 快速烧钱；缓存能救一部分，TTFT 仍与长度挂钩。
- **延迟**：每多 1K input 增加不可忽略的首 token 延迟；output 自回归每多 1K 增加更可观的尾延迟。
- **质量**：中段信息关注度下降（[Lost in the Middle](https://arxiv.org/abs/2307.03172)，U 形曲线），需要检索/摘要/压缩才能维持。
- **可观测性**：长上下文里"哪些信息被实际用上"难以追踪，需要专门的 eval。

> 经验法则：**先按 8K–32K 设计，超出时用"压缩 + 检索 + 缓存"放大**，不要无脑喂 1M。

## Context 的三层来源

把"装进窗口的全部 token"按来源分层，能避免"高频变化"和"稳定系统指令"混在一起。

| 层         | 内容                                                          | 特征                       | 优化手段                                |
| :--------- | :------------------------------------------------------------ | :------------------------- | :-------------------------------------- |
| **静态层** | system prompt、persona、约束、schema、工具定义、长期 few-shot | 跨请求**不变**             | 放最前部 + `cache_control`              |
| **动态层** | 当前用户输入、当前任务、对话历史、中间思考、当前工具结果      | 每请求 / 每回合变化        | 在静态层之后；过长时压缩 / 截断 / 折叠  |
| **外部层** | 长期 memory、向量库 / RAG、artifacts、skills、session state   | **不进默认窗口**，按需检索 | 返回"最小高信号单元"（chunk + context） |

> **三层不能混**：把"今天的状态"塞进"系统 prompt"→ 缓存命中率 0；把"系统约束"塞进"用户消息"→ 位置效应（Lost-in-the-Middle）把约束埋掉。

## 关键原则

### Just-in-time vs Just-in-case

- **Just-in-case（预装）**：把所有可能用到的材料塞进 prompt，模型"挑着用"。简单但成本高、缓存难命中、context rot 严重。
- **Just-in-time（即时取）**：窗口里只放"指针 / 标识符 + 当前任务"，需要时通过工具（`grep` / `read_file` / `search_kb`）按需拉。复杂但更省、更稳、更可观测。

> Anthropic 建议很直白：_keep lightweight identifiers in context; load data on demand via tools_。**让 agent 用工具去看，而不是你替它看**。

### Context 卫生 & 失忆保护

- **过期清理**：老 user 消息 / 老 tool 结果 / 老中间思考 → 压缩或丢弃，不是简单 FIFO。
- **矛盾去重**：检索结果 / 工具结果之间可能冲突；要显式选"最新 / 最可信"或并列保留并标注。
- **不污染**：不要把"模型刚吐出来的失败尝试 / 自我辩论"原封塞回下一轮——会让模型重复同样的错（context pollution）。Anthropic 4.x+ 的 thinking 块由 API 自动从下一轮剥离。
- **失忆保护（Anchoring）**：不显式保留就一定会丢的事实，**锚到外部可恢复的位置**——`memory`（Mem0 / 向量库 / 笔记，下次按需检索）、`artifact`（版本化文件，`trpc-agent-go` 的 `artifact` 包就是这种设计）、`schema/校验`（JSON schema 字段 / 单元测试 / 规则，让程序而非注意力来保证）。

## 压缩与摘要

| 层级                | 手段                                                                                                         | 适用                                 |
| :------------------ | :----------------------------------------------------------------------------------------------------------- | :----------------------------------- |
| **Pre-compression** | 强而便宜的 LLM 先抽 10 条要点 + 不确定清单，再做最终任务                                                     | 超长单次输入（百 K+ token）          |
| **Compaction**      | 老消息摘要（Anthropic 的 server-side compaction 在 API 层做，应用透明）                                      | 多轮长会话                           |
| **Context editing** | 手动 `tool result clearing` + `thinking block clearing`                                                      | 需要精细控制时                       |
| **Token 级压缩**    | [LLMLingua](https://arxiv.org/abs/2310.05736) 类工具，20× 压缩 + 几乎不掉质量（GSM8K / BBH / ShareGPT 验证） | 长 CoT、long ICL、超长 system prompt |
| **工具结果折叠**    | 截断到 N token → 二次抽取；或返回"摘要 + 指针"                                                               | 大块工具结果                         |

工程要点：

- 摘要要**保留决策与不确定性**，不能只保留"事实"——决策理由是后续轮次最缺的东西。
- Anthropic 建议：先保 recall，再迭代 precision；不要直接追求"摘要又短又准"。
- LLMLingua 的关键工程教训：压缩 LLM 需要**和推理 LLM 指令对齐**（instruction tuning），否则压缩后的分布与下游模型不匹配，反而损害质量。
- Token counting API（Anthropic 提供）预算剩余 token，跌破阈值时**主动触发** compaction，不要等溢出。

参考：[Anthropic: Context editing](https://platform.claude.com/docs/en/docs/build-with-claude/context-editing)。

## 锚定与位置效应

### Lost-in-the-Middle

[Liu et al., 2023](https://arxiv.org/abs/2307.03172) 的核心发现：把关键信息放在 prompt **首部**或**尾部**召回率最高，中段**显著下降**（U 形曲线，在多个 long-context 模型上一致）。

工程动作：

- **硬约束放首尾**（任务定义 / 必守规则 / 输出 schema）。
- **示例、参考材料放中段**（对位置敏感度低于硬约束）。
- **最终任务指令放尾部**（让最近注意力落在"现在要做什么"）。
- **不要把关键事实藏在示例之后**——容易被示例的"风格"带跑。

### 引用与可追溯

- 用 `<<<DOC id=N>>> ... <<<END>>>` 分块，要求输出里**每个关键结论都标 `[[id]]`**（与 `prompt_engineering.md` 长上下文技巧一节对齐）。
- 程序侧反查：`[[id]]` 集合必须 ⊆ `提供材料的 id`；不一致即重试或降级。
- 对外部检索（RAG），把"检索得分 / 来源 / 时间"作为元数据一并返回，方便模型和程序都判断可信度。

### 多源冲突的解决规则

当两条信息冲突时，不能让模型自己挑——必须显式规则：

- **时间优先**：新源覆盖旧源（明确标注"已弃用"）。
- **权威优先**：官方 > 社区 > 模型先验。
- **置信度优先**：低 confidence 被高 confidence 覆盖。
- **保留并标注**：当不能判断时，**并列保留并提示模型"以下两条冲突，请基于 X 选择"**——比"静默选一条"安全。

## 缓存与持久化

### 厂商对比

| 厂商                            | 触发                       | 最小长度     | TTL           | 折扣              | 关键约束                                       |
| :------------------------------ | :------------------------- | :----------- | :------------ | :---------------- | :--------------------------------------------- |
| **Anthropic** (`cache_control`) | 显式 marker（最多 4 断点） | 1K–4K 视模型 | 5min / 1h     | 写 1.25×、读 0.1× | 严格 prefix 匹配；2026-02-05 起 workspace 隔离 |
| **OpenAI**                      | 自动（prefix hash）        | 1024         | 5–10min / 24h | 读 ≈ 0.1×         | 完全自动；工具 / 图片 / schema 也算 prefix     |
| **Google (Gemini)**             | 隐式 prefix 缓存           | 视模型       | 视模型        | 视模型            | 与 Vertex / AI Studio 行为略不同               |

> **共同点**：都靠 **prefix 匹配**——prefix 变，缓存就 miss。"高频变化"绝对不能放进 cache 前缀。参考：[Anthropic: Prompt caching](https://platform.claude.com/docs/en/docs/build-with-claude/prompt-caching)、[OpenAI: Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)。

### 缓存工程抓手

- **稳定前缀结构化**：把"系统指令 + 工具定义 + 长期 persona + 大 few-shot"按这个顺序拼，**变化的内容永远放最末**。
- **断点放"最后稳定块"**（Anthropic）：marker 放在"每请求都不会变的那一块"的**末尾**，不是变化的块。
- **预热**（Anthropic）：用 `max_tokens: 0` 把 prefix 写进 cache，消除首请求的 TTFT 惩罚；按 TTL 周期重写。
- **窗口约束**：Anthropic 的 20-block lookback window——marker 之间块数不要超出；OpenAI 用 `prompt_cache_key` 在 prefix 之外多一层路由键。
- **观测**：读 `usage` 里的 `cache_read_input_tokens` / `cache_creation_input_tokens` / `input_tokens` 比例。

### Session / Artifact 持久化

跨请求 / 跨会话的状态用外部持久化层而不是塞 prompt：

- **trpc-agent-go（腾讯）**：三层 `session`（当前会话事件）/ `memory`（in-memory + Redis）/ `artifact`（in-memory / S3 / COS，版本化产物）。[GitHub](https://github.com/trpc-group/trpc-agent-go)。
- **Mem0**：把"记忆"做成一类"外部 context"——按 user / agent / session 分层，按需检索注入。[overview](https://docs.mem0.ai/overview)。
- **MemGPT / Letta**：memory tier 显式分 main context / archival / recall，让 agent 自己管理 paging（[Packer et al., 2023](https://arxiv.org/abs/2310.08560)）。

> **Cache vs Session/Artifact**：Cache 是"瞬时 prefix 复用"（TTL 几分钟到一天），Session/Artifact 是"跨 session 持久化"。两者**互补不替代**——典型架构是：Cache 装静态 + 当前对话，Session 装结构化状态，Memory 装长期事实，Artifact 装大文件。

### 缓存反模式

- **把"每请求变"的内容放进 cache 前缀**（timestamp、用户 ID、轮次计数）——每变一次就 miss，浪费 cache write 钱。
- **依赖组织间共享**（Anthropic 自 2026-02 起 workspace 隔离；OpenAI 不跨组织）。
- **以为 OpenAI 缓存 0 配置**——prefix 至少 1024 token 且完全相同；前缀差一个空格就不命中。
- **cache write 比 base 贵 1.25×**——TTL 内命中率不够时，缓存可能比直接 read 还贵。

## 长上下文的几个权衡

> `prompt_engineering.md` 的"长上下文技巧"讲 prompt 内部排版。这里讲**怎么决定上下文总量**。

### "小上下文 × 多轮" vs "大上下文 × 单轮"

| 方案         | 优势                                 | 代价                                              |
| :----------- | :----------------------------------- | :------------------------------------------------ |
| 小上下文多轮 | 每轮快、便宜、易调试、上下文卫生好做 | 轮次间状态依赖工具 / memory，易丢中间信息         |
| 大上下文单轮 | 一步到位、不依赖外置                 | TTFT 慢、贵、context rot、Lost-in-the-Middle 风险 |

经验：**优先"小上下文 × 多轮"**——但每轮之间必须**显式状态交接**（写到 artifact / memory），否则就是"忘得快"。只有"必须一次性看到全部"（代码 review、长文摘要、跨章节问答）才用大上下文单轮，且要做"前置摘要"和"末段强指令"。

### 递归 / 分层摘要

对超长任务分层：原文（外置）→ 章节摘要 → 合并摘要 → 全文档要点 + 决策与未决项。配合 tree-of-thoughts：第 1 层可并行生成，最后 join。

> 分层的本质是**用算力换 context 容量**——把"一次看 N"拆成"看 N 次小窗口 + 一次看 N 摘要"。

### 结构化替代长文字

同样的内容，**结构化（表格、清单、带 ID 的块）**比"自然语言散文"在长上下文里**召回率更高**。代码 / diff / schema 保留结构（行号、字段名）远比转成自然语言有效。

## 外部层：RAG / Memory / Tools / Artifacts

> 外部层是 CE 的"超能力"——把"窗口容量"问题转成"检索 + 持久化"问题。

- **RAG（检索增强）**：本质是按 query 动态选择塞进窗口的 chunk。Anthropic 的 [Contextual Retrieval](https://www.anthropic.com/news/contextual-retrieval) 在 chunk 嵌入前用 LLM 生成 50–100 token 的"chunk 上下文"再 prepend——失败率（1 − recall@20）从 5.7% 降到 3.7%（-35%）；加 Contextual BM25 降到 2.9%（-49%）；再叠加 reranker 降到 1.9%（-67%）。**embedding + BM25 + reranker** 是目前最稳的组合。
- **Memory**：短期 = 当前上下文（动态层），长期 = 外部 store（外部层）。Self-RAG 思路（[Asai et al., 2023](https://arxiv.org/abs/2310.11511)）让模型发 **reflection tokens** 决定"要不要检索 / chunk 是否相关"，比"无差别先检索 N 条"更准。
- **Tools**：工具结果**不能默认全量进 prompt**。设计原则：返回结构化小 payload + 指针（不是大块原文）；指针指向的内容由 agent 用下一个工具按需拉。Anthropic 推荐"progressive disclosure"——agent 用 `glob` / `grep` / `ls` 等**轻探针**先发现资源，再决定要不要 `read_file`。
- **Artifacts**：中间产物（生成的代码、报告、图片、数据集）写进 artifact store，prompt 里只保留"artifact id + 关键摘要"。原始数据**不进窗口**（省 token），但可追溯（artifact id 不变），可被多 agent 共享。注意保留 schema/version，避免 agent 读旧 artifact 时错位。

## Sub-agent 架构与 Context 隔离

当一个 agent 的 context 装不下整个任务时，**拆 agent**比**塞更长 prompt**更稳。Anthropic 的 [Building Effective Agents](https://www.anthropic.com/research/building-effective-agents) 推荐 orchestrator-workers 模式：中心 agent 拆任务、worker agent 各自带**干净 context**做子任务、最后回传 ~1K–2K token 的浓缩结果。

工程要点：

- **Lead agent 不亲自做脏活**——只持有"任务清单 + worker 摘要"，不持有 worker 的中间工具结果。
- **Worker agent 的 context 是它自己的**——worker 用完即弃，**不把它的工具结果原样塞回 lead**。
- **回传要浓缩**——worker 返回 1K–2K token 的结论，而不是把"我跑了哪些工具"全写回来。
- 适合**并行研究**（同一问题从不同角度查）、**多步骤流水线**（每步独立 context）。

> Sub-agent 本质是**用拓扑换 context 容量**——把"一个长 context 跑全程"拆成"多个短 context 各自跑一段 + 摘要回传"。

## 反模式清单

- **把"全部历史"塞进 prompt**：context rot、TTFT 暴涨、缓存 miss、中段事实被忽略。修：compaction + 老轮次摘要 + 老工具结果清空。
- **把"模型刚吐出来的中间思考"原样塞回**：context pollution——模型会在后面的轮次重复同样的错误推理路径。修：Anthropic 4.x+ 的 thinking 块 API 自动剥离下一轮；其他场景手动剥离或摘要化。
- **不知道哪些是静态、哪些是动态**：把"今天的状态 / 当前时间戳"和"系统约束"混在同一个前缀 → 缓存命中率 0。修：明确分层，变的东西永远在不变的东西之后。
- **把"高频变化"放进缓存前缀**：cache marker 放在"每请求变"的内容上 → 永远 miss + 付 cache write 钱。修：marker 放在"最后稳定块"的**末尾**。
- **把"任务无关"的内容塞进 prompt 凑数**：挤占有效 token 容量、核心指令被埋、风格被无关示例带跑。修：few-shot 要**精不要多**（1–2 个典型 + 1 个反例）；背景要**任务相关**。
- **把"大块工具结果"原样塞回**：单次工具调用占光窗口，缓存布局全乱。修：工具层截断 + 二次抽取；或让 agent 用指针二次取用。
- **相信"模型能识别坏指令"**：外部材料里夹"忽略以上规则" → 注入成功。修：明确优先级（系统 > 用户 > 外部材料）、高风险动作二次确认（见 `prompt.md` 安全一节）。

## 评估：怎么知道 context 装得好不好

Context 装得好不好，独立于"最终任务成功率"也要测。四类指标：

1. **信息命中率**：prompt 提供的 chunk / memory / 工具结果里，**被最终输出引用的比例**（用 `[[id]]` 反查或让模型显式列"我引用了 N 段"）。< 30% → 装配太冗余；> 90% 但质量低 → 装配太窄。
2. **必要 token 比 = 完成任务的最小 token / 实际使用 token**。1.0 = 完美；0.3 = 用了 3×。计算：ablation——逐步删 chunk，看任务分数何时下降。
3. **Cache hit rate**（Anthropic: `cache_read / (cache_read + cache_creation + input)`；OpenAI: `cached_tokens / prompt_tokens`）。长期 < 50% → 装配有高频变化渗入前缀。
4. **任务质量**（与 `evals.md` 接通）：任务成功率、引用准确率、幻觉率、注入抗性。CE eval 要**含 adversarial 子集**（注入、长 distractor、过期 memory），见 Chroma context rot 的"distractor 敏感"结论。

> 评估不能只看"任务成功率"——同样的成功率，**靠 5K context vs 靠 200K context 达到的，工程质量完全不同**。

## 上线前自查

- [ ] **分层明确**：静态 / 动态 / 外部三层划分清楚，没有"高频变化"渗入静态层
- [ ] **缓存布局合理**：marker 在最后稳定块末尾；首请求预热；TTL 选择有依据
- [ ] **压缩与摘要**：超过 N 轮主动触发 compaction；超长输入有 pre-compression；LLMLingua 类工具只用于长 prompt
- [ ] **失忆保护**：关键事实锚到 memory / artifact / schema，不依赖模型"应该记得"
- [ ] **位置效应**：硬约束在首尾；示例在中段；关键结论尾部重申
- [ ] **引用可校验**：输出有 `[[id]]` 引用，程序侧能反查
- [ ] **工具结果收敛**：返回结构化 payload + 指针；大块结果折叠 / 二次抽取
- [ ] **eval 含 context 指标**：信息命中率、必要 token 比、cache hit rate、adversarial 子集
- [ ] **观测齐全**：`usage` 块里的 cache 字段都有；TTFT / p99 latency 按长度分桶

## 关键参考来源

- **Anthropic**：[Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)（CE 官方定义）、[Contextual Retrieval](https://www.anthropic.com/news/contextual-retrieval)（RAG 失败率 -35% / -49% / -67%）、[Prompt caching](https://platform.claude.com/docs/en/docs/build-with-claude/prompt-caching)（`cache_control` / TTL / 预热）、[Context windows](https://platform.claude.com/docs/en/docs/build-with-claude/context-windows)（context rot / compaction）、[Building Effective Agents](https://www.anthropic.com/research/building-effective-agents)（orchestrator-workers）
- **OpenAI**：[Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)（自动 prefix / 1024 token / 5–10 min / 24h）
- **学术 / 研究**：[Chroma: Context Rot Research](https://www.trychroma.com/research/context-rot)（18 LLM 上"长 ≠ 稳"）、[Liu et al., 2023: Lost in the Middle](https://arxiv.org/abs/2307.03172)（U 形曲线）、[Packer et al., 2023: MemGPT](https://arxiv.org/abs/2310.08560)（虚拟 paging / 分层 memory）、[Asai et al., 2023: Self-RAG](https://arxiv.org/abs/2310.11511)（reflection tokens）、[Jiang et al., 2023: LLMLingua](https://arxiv.org/abs/2310.05736)（20× 压缩）、[Lilian Weng: LLM Powered Autonomous Agents](https://lilianweng.github.io/posts/2023-06-23-agent/)（CE 概念前驱）
- **工程实现**：[Mem0](https://docs.mem0.ai/overview)、[trpc-agent-go](https://github.com/trpc-group/trpc-agent-go)

## 相关链接

- [`ai/prompt_engineering.md`](prompt_engineering.md)——"长上下文技巧"那一节讲 prompt **内部**排版（锚定、分段标签、`[[id]]` 引用、缓存友好布局），本篇讲 prompt **外部**装配；其余 prompt 模式（CoT / ReAct / 结构化输出）见该文
- [`ai/prompt.md`](prompt.md)——5 元素框架与 Level 1/2/3 层级（CE 的"指令层"基础）
- [`ai/agent.md`](agent.md)——agent 闭环架构（CE 的"动作层"基础）
- [`ai/evals.md`](evals.md)——context 利用率、必要 token 比、cache hit rate 这些指标的测试方法
- [`ai/memory.md`](memory.md)——长期 memory 系统的工程实现（本篇"外部层"的具体落地）
- [`ai/rag.md`](rag.md)——RAG 作为"动态 context"装配的具体做法
- [`ai/tools.md`](tools.md)——tool result 折叠、artifact 模式的具体做法
- [`ai/multi_agent.md`](multi_agent.md)——sub-agent 拓扑、context 隔离的具体做法
