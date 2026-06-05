# AI 记忆系统（Memory Systems）

## TL;DR

- LLM 自身没有跨调用的状态，**memory 是把"多轮 / 跨会话的连续性"外置到系统**。这一篇从工程角度拆开：分层、操作、抽取、存储、检索、衰减、更新、评估。
- 横向对比 **8 个方案**：mem0、Letta（MemGPT 后续）、LangMem、Cognee、Zep（Graphiti）、A-MEM、flowcraft、trpc-agent-go，附学术代表 MemoryBank 与综述 _Human-inspired Perspectives: A Survey on AI Memory_。
- 几个反复出现的设计取舍：
  - **ADD-only vs UPDATE/DELETE**：mem0 只追加 + 时间戳/置信度衰减；Graphiti 用 validity window 作废旧事实；传统 RAG 直接覆盖。
  - **单一信号 vs 混合检索**：BM25 / 向量 / 实体 / 图遍历单独都不够；flowcraft 三路 RRF + 时间衰减 + 实体加权是典型工程做法。
  - **in-context block vs vector store**：Letta 的 in-context block（MemGPT 风格）强调模型能直接看到 + 改写；Zep / Cognee 的图谱强调结构化推理。
- 评估别只看"准确率"：LoCoMo / LongMemEval / BEAM 都把**时间推理、知识更新、跨 session 多跳**当关键维度；线上还要监控 token 成本、延迟、过期事实污染。

## 1) 记忆类型分层

> 类比人脑：不同类型的记忆在写入、保持时长、检索方式上完全不同。把"记忆"当成一个东西讨论是大多数设计混乱的根因。

| 层             | 类比                   | 保持时长 | 典型载体                              | 代表方案                                |
| :------------- | :--------------------- | :------- | :------------------------------------ | :-------------------------------------- |
| **Working**    | 工作记忆 / RAM         | 当前会话 | in-context 消息列表、agent scratchpad | LangGraph short-term、Letta core blocks |
| **Episodic**   | 情景记忆（"何时何地"） | 跨会话   | 带时间戳的 episode / 对话流           | Letta recall memory、Zep episodes       |
| **Semantic**   | 语义记忆（"实体事实"） | 长期     | 知识图谱 / facts / entities           | Zep Graphiti、Cognee、mem0 facts        |
| **Procedural** | 程序性记忆（"怎么做"） | 长期     | 工具 / 技能 / 提示模板 / 工作流       | LangMem procedural、Agent Skills        |

> **关键设计问题**：产品需要的是"跨 session 的连续对话"（episodic 主导）还是"长期累积的用户/世界知识"（semantic 主导）？前者 Letta / mem0 更顺手，后者 Zep / Cognee 更顺手。

### Working memory 的边界

- 默认就是 LLM 的上下文窗口，**受 max_tokens 约束**。lost-in-the-middle、锚定、压缩都见 `ai/prompt_engineering.md`。
- 多数框架的"短期记忆"其实是把 working memory **持久化**到 session store（如 LangGraph `InMemoryStore` / `PostgresStore`），方便 client 重建会话。**working memory ≠ session store**——前者是模型的"工作台"，后者是事件的"账本"。

### Semantic memory 的两种形态

- **Vector-only**：embedding + ANN 检索；便宜好上手，但难以做"X 的朋友是谁"这种关系推理。
- **Graph-based**：实体 + 关系 + 边属性（时间、来源、置信度）；可解释、可推理，但 schema 维护与抽取成本高。
- **两者通常并列使用**：vector 找候选 → graph 扩展邻居 → LLM 综合。Cognee 与 Graphiti 都是这种 hybrid 范式。

## 2) 记忆操作（Extract / Store / Retrieve / Update / Forget）

把任何 memory 系统沿这五步拆开看，能立刻发现**它在每一步上的实现深度**——而不同系统的差异，恰恰就体现在**哪几步被省略、哪几步被做厚**。朴素 RAG 可能只实现 store + retrieve；Letta 的 core memory 没有独立的 forget 步骤。

```text
[外部输入] -> extract -> store -> [memory] -> retrieve -> [上下文]
                                       \-> update / forget (后台维护)
```

### 2.1 Extract：把原始输入转成"记忆条目"

| 方案         | 抽取触发                       | 抽取粒度                              | 去重 / 合并                            |
| :----------- | :----------------------------- | :------------------------------------ | :------------------------------------- |
| mem0         | 每次对话后异步（ADD-only）     | atomic fact（短陈述句）               | 写入时 semantic dedup                  |
| Letta        | 每次工具调用（agent 主动）     | in-context block 的字符串段           | 显式 `memory_replace` 工具             |
| LangMem      | hot-path 工具 + 后台 manager   | typed memory（fact / profile / proc） | 后台 consolidation                     |
| Cognee       | `remember()` 同步管线          | chunk / entity / relation             | 内置 graph induction                   |
| Zep Graphiti | episode 入库时增量             | entity / edge（带 validity）          | edge invalidation 而非合并             |
| A-MEM        | 每次新记忆写入                 | 自由结构化 note + tags                | LLM 改写历史 notes（memory evolution） |
| flowcraft    | 写时由 LLM 抽取（vendor 中性） | fact / entity                         | predicate alias normalization          |

> **关于"atomic fact"这个选择**：粒度不是事实的自然属性，是设计取舍——
>
> - **检索粒度**：粗粒度 fact（"张三是个好人"）召回面广但不准；细粒度 fact（"张三每天早上跑步"）精准但召回窄。
> - **是否需要 update**：细粒度才支持局部更新；粗粒度常需整条覆盖。
> - **dedup 能否工作**：粗粒度重复难判（"张三住北京" vs "张三住在北京"），细粒度重复易判。
>
> mem0 选 atomic 短陈述句，本质是把"事实的结构"压平——换来更简单的 dedup / update / retrieval，代价是**丢失事实间的关系与上下文**。

**抽取哲学的三大分支**（细节见第 3 节）：

- **单次 ADD-only（mem0）**：写入最便宜，矛盾让检索时靠时间戳/置信度解决。
- **多轮 agentic（Letta / LangMem）**：模型主动改写 block 或合并 facts；可控性高但每次都要 LLM 调用。
- **主动 consolidation（Cognee / A-MEM）**：用后台任务做图谱归纳 / note 重写；知识密度高但要解决"何时触发、冲突如何仲裁"。

### 2.2 Store：记忆写到哪里

- **嵌入式 in-memory**：简单、零运维，重启即失；适合 demo / 单进程。
- **向量数据库**：Qdrant / Milvus / Weaviate / pgvector / LanceDB / Chroma。生产默认 pgvector 或 Qdrant。
- **图数据库**：Neo4j、FalkorDB、Kuzu、Amazon Neptune。Graphiti 同时支持这四家。
- **关系型 + 向量插件**：Postgres + pgvector + （可选）pg_graphrag。少一个组件、事务一致。
- **Redis**：trpc-agent-go 默认选项之一；适合已有 Redis 栈、要 TTL / 限流一体化。
- **SQLite-vec / DuckDB-VSS**：本地优先 / 边缘部署。

> 存储是 memory 系统里**最容易被低估**的一环。Zep 的"Context Lake"把所有会话 + 业务数据 + 外部信息**统一进图**再检索，是用存储架构换检索质量。

### 2.3 Update：记忆怎么"演进"

- **直接覆盖**：传统做法，丢历史、难审计、容易丢关键事实。
- **新写 + 旧废（Graphiti）**：旧 edge 进入历史而不是被删除，validity window（`valid_from / valid_to`）一等公民；查询时按时间过滤。
- **新写 + 衰减（mem0）**：旧事实保留但置信度/权重随时间衰减，检索时自然降权。
- **新写 + 合并（LangMem / Cognee）**：后台把多条 facts 归纳成更高级的"concept"。

### 2.4 Forget：主动遗忘

- **硬删除**：GDPR / 隐私要求下必须支持。
- **TTL 过期**：常见于 session memory 与短期 facts。
- **置信度归零 / 软删除**：保留条目但检索时过滤。
- **司法保留（Legal Hold）**：Zep 等企业级方案把"先保留再删除"做成策略对象（policy-driven retention + legal hold）。

> "**遗忘是合规问题，不是技术问题**"——用户能不能删？多久之后自动清理？审计日志放哪？这些**先于技术**回答。

## 3) 抽取策略：什么时候用哪种

> 这是设计上**最容易做错**的一步——它直接决定后续检索、冲突解决与成本结构。

> **Extract 是整个 memory 系统的成本与质量双重瓶颈**。其他四步本质是工程问题（DB 选型、索引、并发、TTL、衰减函数），Extract 是 AI 工程问题（prompt 设计、模型选型、置信度校准、粒度判定）。一次 extract LLM 调用通常比一次 embedding 贵 5–50 倍——系统"看起来只多一步"，账单可能翻倍；retrieve 再花哨，提取时丢的事实也找不回来。一个反直觉点：**底层 LLM 越强，memory 系统的相对收益越低**——模型自己越做得好 implicit 抽取 / 跨轮聚合，外部 Extract 越像在重复劳动。

### mem0：单次 ADD-only 抽取

- 流程：每次新消息触发一次 LLM 抽取，**只追加新 fact**，不做 UPDATE/DELETE。保留历史，矛盾用时间戳 + 检索时的衰减解决。
- 论文结果（LoCoMo 基准）：相对 OpenAI 全上下文方法，**LLM-as-a-Judge 提升 26%、p95 延迟降 91%、token 成本降 >90%**（arxiv 2504.19413）。
- **代价**：同主题可能有几十条历史 facts，存储膨胀；需要靠**检索时的过滤**（按时间 / 置信度 / supersede）来保持召回质量。
- **Pre-filter gate**：论文口径是"每条消息都抽"，实际工程里几乎没人这么做——80% 对话是寒暄 / 确认 / 过渡，全量抽取 = 高成本 + 噪声库。生产系统通常加一道 gate（"这条消息是否含值得长期记忆的信息"），用 LLM 判别、规则匹配或用户明示触发（"记住这个"）。判错代价不对称——漏抽一个事实后续补更贵，多抽十条噪声污染检索但代价有限——所以 gate 倾向"宁可过抽"。

### Letta / LangMem：多轮 agentic

- 模型本身**被授权读写记忆**：通过工具（`memory_replace`、`core_memory_append`、`store`）主动维护 in-context blocks。
- 优点：可控、**模型知道有什么记忆**（block 是 prompt 一部分）、可即时修正。
- 代价：每次写都要 LLM 调用，且模型写错时**没有"硬契约"保护**——Letta 把 block 设成**有大小上限**的字符串并提供 `read_only` 标记。

### A-MEM：主动 consolidation（Zettelkasten 风格）

- 论文 _A-MEM: Agentic Memory for LLM Agents_（arxiv 2502.12110，NeurIPS 2025）。
- 关键创新：写入新记忆时，**LLM 同时改写历史 notes 的 description / tags / links**（"memory evolution"）。知识网络自组织，**没有人工 schema** 也能形成层级；代价是写入开销大、行为不可预测。

### Cognee：图归纳

- 内部 pipeline：`add` + `cognify` + `improve`，把 chunk 转成 entity / relation 写入图谱。适合"大量历史文档 / 多源数据"场景。

### 选哪个

> - **对话型产品 + 用户偏好** → ADD-only（mem0）或 Zep 的 invalidation 范式。
> - **需要模型自己组织思路**（角色扮演 / 长期项目协作）→ agentic（Letta / LangMem）。
> - **大量历史文档 / 知识库** → graph + vector（Cognee / Graphiti）。
> - **研究型 / 实验性系统** → A-MEM 思路（成本不是首要约束）。

> 另一个常被忽略的维度是**事实的显隐程度**——它直接决定 atomic 抽取能否 work：
>
> - **显式事实**（"我叫张三"、"我住北京"）→ atomic 短陈述句够用，mem0 / Zep 顺手。
> - **大量隐式事实**（"这个夏天北京太热" → 隐含"用户在北京"；"我们公司最近在裁员" → 隐含"用户在职"）→ atomic 抽取会**系统性漏抽**，需要 RAG + 总结 + agentic 改写的组合。
> - **情绪 / 偏好反转**（"这个方案我不太满意，但先这样吧"）→ 隐式 + 时间敏感，ADD-only 系统**几乎无能为力**——没有原子 fact 能"代表"用户的真实态度。

## 4) 存储与索引后端

| 后端类型                    | 代表                                             | 适合什么                    |
| :-------------------------- | :----------------------------------------------- | :-------------------------- |
| **in-memory**               | LangGraph `InMemoryStore`、trpc-agent-go default | demo / 原型                 |
| **向量 DB**                 | Qdrant、Milvus、Weaviate、Chroma                 | 单跳语义检索                |
| **Postgres + pgvector**     | pgvector + pg_graphrag（可选）                   | 中等规模 + 已有 Postgres 栈 |
| **图 DB**                   | Neo4j、FalkorDB、Kuzu、Neptune                   | 实体关系密集场景            |
| **Redis**                   | trpc-agent-go Redis memory                       | session / 短期记忆          |
| **SQLite-vec / DuckDB-VSS** | sqlite-vec、duckdb-vss                           | 本地优先 / 边缘             |
| **混合湖**                  | Zep Context Lake、Cognee Cloud                   | 企业级、需要 SLA            |

> **一个反直觉点**：向量 DB 的"召回率"在大规模（亿级）下会掉，而**图谱在跨实体推理上比纯向量好得多**。生产系统不要迷信"向量就够了"。Zep 的"Context Lake"把会话 + 业务数据 + 外部信息**统一进图**再检索，是用存储架构换检索质量。

## 5) 检索：语义 / BM25 / 实体 / 图遍历 / 混合

> 单独任何一路都不够。**为什么需要 hybrid**——三个失败模式：
>
> 1. 纯向量：用户说"我去年那个项目"，embedding 找不到"去年"的时间信号。
> 2. 纯 BM25：用户说"那个我们聊过的供应商"，关键词检索不到。
> 3. 纯图遍历：需要先有"起点节点"，而自然语言 query 通常没有。

### 5.1 经典三路融合（flowcraft `sdk/recall`）

flowcraft 把这件事讲得很清楚：BM25、vector、entity 三路并行 → RRF (K=60) 融合 → 再叠加 **entity-overlap boost、supersede decay、time decay**：

```text
query
  ├─► BM25 lane ────┐
  ├─► vector lane ──┼─► RRF (K=60) ─► entity boost + supersede decay + time decay ─► final ranking
  └─► entity lane ──┘
```

- **RRF（Reciprocal Rank Fusion）**：把每路给每个文档的排名倒数求和再重排。**优点是免调权**——只需要定 K（典型 60），不需要每路有自己的相似度归一化。
- **Entity-overlap boost / Supersede decay / Time decay**：分别对应"共享实体加权 / 被新事实替代的旧记录降权 / 旧事实降权"，工程上常用 `exp(-Δt / τ)`。

> **RRF 不是唯一选择**。Zep / Graphiti 内部还叠加 **graph traversal**：从 entity 节点出发做 N 跳，捞出"和 query 实体相关但字面不相似"的事实。LLM summarization 作为最后一步。

### 5.2 语义检索的常见坑

- **Embedding 模型**：通用 embedding 对"专有名词 / 时间 / 数字"不敏感；考虑 `bge-m3`、`e5-large`、`text-embedding-3-large`，必要时做 **hybrid 关键词增强**（BM25 + 稠密）。
- **Chunk 大小**：典型 256–512 token，带 50 token overlap。
- **HyDE / Query Rewrite**：query 与文档不在同一分布时，先让 LLM 把 query 改写成"假想答案"再检索。
- **Temporal reasoning**：query 含"去年 / 上次 / 在……之前"时**单靠语义检索抓不到**——需要 facts 带时间戳并加时间过滤（Zep 的 invalidation 就是为这个设计的）。

### 5.3 何时上 cross-encoder reranker

- top-50 → top-5 的精排明显影响下游答案质量时（事实性问答、引用密集任务）。
- 折中：**二阶段**——bi-encoder 召回 top-50，cross-encoder rerank 到 top-5。
- flowcraft 的"rerank"是用规则 + LLM 综合，不是 learned cross-encoder；learned reranker 适合"语义鸿沟大、规则不够"的场景。

## 6) 重排与衰减

> 任何记忆系统都要回答"哪些记忆该被降权、哪些该被忘掉"——否则 memory 越长越糟。

| 机制                    | 谁在用                         | 作用                              | 风险                             |
| :---------------------- | :----------------------------- | :-------------------------------- | :------------------------------- |
| **时间衰减**            | mem0、flowcraft                | 旧事实自然降权                    | "时间敏感但仍相关"的旧事实被误杀 |
| **Supersede / Invalid** | flowcraft、Zep Graphiti        | 被新事实替代的旧事实降权          | 需要"新旧"判定，可能判错         |
| **Entity boost**        | flowcraft                      | 共享实体的记忆升权                | 实体抽取错误会污染排序           |
| **访问频率**            | Zep 等                         | 被反复命中的记忆保留更久          | "被问得多"≠"更重要"              |
| **置信度**              | mem0、mem0-graph               | LLM 抽取时给 confidence，检索加权 | LLM 校准差时置信度不可信         |
| **TTL**                 | trpc-agent-go Redis、LangGraph | 到期硬删除                        | 不适合"理论上还可能有用"的记忆   |
| **Learned reranker**    | 生产级企业系统                 | cross-encoder 精排                | 训练数据 / 推理成本高            |

> **衰减的工程核心问题**：衰减函数与超参要写在**评测里**，否则上线后没人能回答"为什么这条记忆不见了"。

## 7) 更新策略：ADD-only 哲学的代价与收益

### 7.1 三种范式

| 范式                | 代表                | 怎么"改"                            | 历史可追溯 | 适合什么                     |
| :------------------ | :------------------ | :---------------------------------- | :--------: | :--------------------------- |
| **ADD-only + 衰减** | mem0                | 只追加，旧事实靠时间/置信度降权     |     强     | 用户偏好、消费历史           |
| **Invalidation**    | Zep Graphiti        | 旧 edge 设 `valid_to`，新 edge 生效 |     强     | 知识有时间维度的事实         |
| **In-place UPDATE** | 传统 DB / LangGraph | 直接覆盖                            |     弱     | 明确知道错了且不再回看的字段 |

### 7.2 关键事实怎么避免被覆盖 + 隐私

- **硬契约 + idempotency key**：写入时用 `(user_id, fact_type, fact_key)` 判重，避免同一事实被反复新增。
- **双向写 + 后台合并**：热路径只 append；后台 job 定期 dedup / 合并（LangMem 的 background manager）。
- **关键事实显式标记**：用户身份、安全相关、医学禁忌等条目设 `pinned=true`，**任何降权机制都不能动**。
- **审计 + 撤销**：保留"加 / 改 / 废"完整时间线，方便用户回看和撤销。
- **隐私与遗忘权**：任何面向消费者的系统都要回答"用户能不能查 / 改 / 删？删除是硬删还是软删？保留审计吗？"。Zep 提供 principal/resource/action 风格的 ABAC + retention policy + legal hold；典型工程清单——用户端"查看我的记忆"页面；硬删 + 异步清理下游 embedding / 图谱 / 备份；保留 N 天"软删"支持恢复；每次读写打日志至少 X 天。

## 8) 评估：LoCoMo / LongMemEval / BEAM

> 评测记忆系统比评测 LLM 能力**更难**——因为答案的正确性经常是"在正确时间想起正确事实"，而不是单跳事实问答。

### 8.1 LoCoMo（Long Conversation Memory）

- 长对话（约 300 轮）作为背景，问题分布在 **single-hop / temporal / multi-hop / open-domain** 四类。
- 用法：拿一个 memory 系统喂完整对话，再回答 benchmark 问题，比 F1 / BLEU / LLM-as-a-Judge。
- 是 mem0、Zep 公开 benchmark 的事实标准。

### 8.2 LongMemEval（ICLR 2025）

- 5 大能力维度：信息抽取、多 session 推理、时间推理、知识更新、abstention（拒答）。
- 数据集 500 题嵌入可扩展长 session。
- 关键发现：商业 chat assistants 与长上下文 LLM 在**跨 session 信息保持**上掉 30% 以上准确率。
- 三阶段框架：**Indexing → Retrieval → Reading**；优化方向：session 分解、fact-augmented key expansion、time-aware query expansion。

### 8.3 BEAM

- 同样聚焦长期记忆，但更强调"行为可观测"——agent 是否真的在多轮后做出**与历史一致**的决策。适合评估 agent 整体（model + memory + tools）。

### 8.4 评测设计要点

- **多样性**：覆盖 happy path、矛盾、撤回、时间模糊、跨语言。
- **难度梯度**：从单跳事实到 5 跳推理都要有。
- **污染控制 + 金标 hygiene**：基准集在训练截止后公开、与训练集去重；多评审 + rubric；不靠单一 LLM-as-Judge。
- **能力 vs 任务分离**：能区分"模型能力差"还是"记忆系统差"——理想是同一组对话喂给"无记忆基线 / 全上下文 / 你的 memory 系统"。

> 一个反直觉点：**模型能力越强，memory 系统的相对收益越低**。当底层 LLM 强到能把对话压缩成内部表征时，外部 memory 的边际价值下降。所以 memory 评测要**把基线模型锁住**。

## 9) 常见坑

| 坑                     | 现象 / 后果                        | 关键修法                                                                                |
| :--------------------- | :--------------------------------- | :-------------------------------------------------------------------------------------- |
| **Token 成本失控**     | 抽取+检索+rerank+注入层层加 token  | 抽取批量化；top-K 收紧；先压 facts 再存；长期记忆只放可复用硬事实                       |
| **过期事实污染**       | 旧偏好/情绪/被纠正信息反复召回     | 抽取时强制 `valid_from/valid_to` 或 `confidence`；检索时时间过滤；自我修正通道          |
| **跨 session 一致性**  | 多设备/多客户端/并发写冲突         | session 内幂等 + 跨 session 单写者（leader election）或乐观锁；consolidation job 走后台 |
| **提示注入穿越到记忆** | 记忆被当指令利用                   | 记忆按"外部材料"对待，不提优先级；抽取时对"像指令"内容做白/黑名单                       |
| **评测-生产脱节**      | benchmark 召回率与线上体验不一致   | 见 `ai/evals.md` 的"回归 / Shadow 评测"——线上真实 query 抽样做金标                      |
| **能存 ≠ 能用**        | 10 万 facts 也可能检索不到关键事实 | 召回率 / 拒答率 / 错答率作为线上指标，定期抽样人工评估                                  |

## 10) 方案横向对比

> 按你的任务形状选最匹配的，而不是按"哪个最热门"。

| 方案               | 抽取方式                | 存储后端                              | 检索                                | 图谱 | 多层 | 典型场景                     |
| :----------------- | :---------------------- | :------------------------------------ | :---------------------------------- | :--: | :--: | :--------------------------- |
| **mem0**           | 单次 ADD-only 异步      | vector + graph（可选）                | semantic + 衰减；graph 变体做图扩展 | 选配 |  否  | 用户偏好、跨 session 事实    |
| **Letta**          | agentic（模型主动读写） | Postgres + 向量后端                   | 工具调用 + in-context block         |  否  |  是  | 长期角色 / 项目协作 / 个性化 |
| **LangMem**        | hot-path + 后台 manager | LangGraph Store（in-memory / PG）     | semantic + 工具调用                 |  否  |  是  | LangGraph 生态应用           |
| **Cognee**         | `remember` 同步管线     | 关系库 + 向量 + 图（Neo4j / Kuzu）    | vector + graph traversal            |  是  |  是  | 大量历史文档 / 企业知识库    |
| **Zep / Graphiti** | 增量 episode 入库       | 时序图（Neo4j / FalkorDB / Kuzu）     | semantic + BM25 + graph traversal   |  是  |  是  | 实时 + 时间推理 + 企业级     |
| **A-MEM**          | 写入时 LLM 改写历史     | vector + 自由 notes                   | semantic + link traversal           |  弱  |  是  | 研究 / 自组织知识            |
| **flowcraft**      | 写入时 LLM 抽取         | `retrieval.Index`（memory/sqlite/pg） | 三路（BM25+vector+entity）+ RRF     |  否  |  否  | 嵌入式 / 边缘 / Go 生态      |
| **trpc-agent-go**  | CRUD 工具调用           | in-memory / Redis                     | 工具调用 / 关键词                   |  否  |  是  | Go 生态 Agent 框架           |

论文与综述补充：

- **mem0** 论文 arxiv 2504.19413；**MemGPT / Letta** 论文 arxiv 2310.08560。
- **A-MEM**（Zettelkasten 风格）：arxiv 2502.12110，NeurIPS 2025。
- **LongMemEval**（ICLR 2025，arxiv 2410.10813）：见第 8 节。
- **MemoryBank**（Tsinghua）：_MemoryBank: Enhancing LLMs with Long-Term Memory_，引入 Ebbinghaus 遗忘曲线 + SILO 机制，**人类记忆衰减函数**移植到 LLM 记忆的代表工作。
- **_Human-inspired Perspectives: A Survey on AI Memory_**（arxiv 2504.15965）：按 **object / form / time** 三维八象限分类，是目前最系统的 memory 系统综述。
- **_A Survey on the Memory Mechanism of LLM-based Agents_**（2024 末）：偏 agent 视角，把 memory 拆成**环境观察 → 短期 → 长期 → 外部工具**四层。

## 11) 工程建议（落地顺序）

> memory 系统是"先简单后复杂"的典型——别一上来就上图谱 / GraphRAG。

1. **先做最小可用**：单次 LLM 抽取 + 向量 DB + top-K 检索。**先看你的 query 分布是否真有"长程依赖"需求**。
2. **加 hybrid 检索**：BM25 + 向量 + 实体三路 + RRF，规则层做时间衰减。
3. **加 evaluation harness**：用 LoCoMo / LongMemEval 子集做回归基线 + 自己的 shadow set。
4. **加 governance**：TTL、隐私删除、审计日志；任何面向消费者的服务不能跳过。
5. **考虑上 graph / invalidation**：当出现"用户偏好反转" / "跨多源实体关系" / "时间推理"需求时再上。
6. **别忘了 prompt 侧的配合**：见 `ai/prompt_engineering.md`——记忆的"使用方式"和"注入方式"是 prompt 工程的一部分。

## 12) 何时不要上 memory

> 警惕"为 memory 而 memory"。**单会话短任务、数据敏感但无审计、底层 LLM 已极强且成本不敏感**这三种情况，无状态或全上下文 prompt 可能比精心设计的 memory 系统更省心。Zep 与 Letta 都强调 memory ≠ 替代 RAG，而是**互补**。

## 13) 准确率与纠错：extract 之后的工程

> Extract 是不可逆的一步——抽错了就找不回来。**真正决定系统质量的不是"抽得多准"，而是"对抽错有多鲁棒"**。把工程投入放在事后纠错通道上，回报比"事前抽对"高得多。

### 13.1 纠错的四类机制

**A. 用户侧纠错**——把"被记住的内容"显式化

- ChatGPT memory / Notion AI / Replika / Cursor / Claude Code 都有记忆面板，可看、可编辑、可批量删。
- 工程要点：不要每次抽完都问用户（会烦死）——一般 onboarding 一次性 + 周期性 re-confirm；删除要硬删 + 软删双通道（合规需要 audit log）。

**B. 检索侧纠错**——不让坏 fact 浮上来

- 核心思想：**不修 fact，让坏 fact 自然沉底**。
- 具体做法：置信度 / 时间 / 反馈信号加权；cross-source consistency（多源印证升权、孤证降权）；访问反馈（用户重问同样问题 → 上次抽错了）；可解释 drop（标"低置信"给用户判断）。
- **这是最被低估也最经济的纠错方式**——mem0 是代表。

**C. 版本化 + 回滚**——Zep 风格的"事实版本控制"

- `History()` 保留完整版本链，新版本写入时旧版本打 `valid_to` 而非删除；`Reinforce` / `Penalize` 调权重；`Forget` 支持硬删 / 软删 / 归档。
- 适用：合规要求高 / 高 stakes 场景（医疗 / 法律 / 客服）；代价是存储和写入成本。

**D. 后台重写**——consolidation job

- LangMem manager：定时跑 consolidation，合并相似 facts、归纳出更高级 concept。
- A-MEM：每条新记忆写入时 LLM 同时改写历史 notes。
- Cognee：可重跑 `cognify()` 重新图归纳。
- 适用：长生命周期 / 知识密度高；**不适合实时对话**——太慢。

### 13.2 准确率"怎么保证"——这个提法本身需要修正

工程上几个反直觉：

**① LLM 自评置信度不能当硬过滤**

- "I'm 90% sure" ≠ 90% 准确（LLM 校准差）。
- 只能做软排序权重（"这堆里这几个排前面"），不能做硬过滤（"confidence<0.5 全删"）——后者会**丢真事实**（LLM 经常对自己抽对的 fact 给低分）。

**② 多 pass / 集成抽取收益递减**

- 不同 prompt / 不同模型抽两次对比，理论上能检测不稳定。
- 实际上：**两次都错的概率 ≈ 一次错的概率**（同分布偏差）；成本翻倍，收益不到 10%。
- **更划算的替代**：用 retrieve 阶段的反馈信号校准。

**③ Grounding check 只对显式事实有效**

- "用户 1990 年出生" → 原文能找到 ✓。
- "用户在北京"（隐含自"这个夏天北京太热"）→ 原文找不到 ✗。
- 系统性惩罚隐式事实 = 把隐式抽取能力砍掉。
- 真要做 grounding：只对**显式 + 高 stakes**子集做。

**④ 评估-生产脱节是结构性的**

- LoCoMo / LongMemEval 测的是 300 轮长对话 + 复杂 query；生产里 80% 是短 session、简单 query。
- **真实世界需要自己的 shadow set**——线上 query 抽样 + 人工金标。
- 没有 shadow set 就没有 extract 准确率的真实信号。

### 13.3 实战清单（按优先级）

| 优先级 | 措施                                  | 解决什么      | 成本 |
| :----: | :------------------------------------ | :------------ | :--- |
| **P0** | Pre-filter gate（80% 寒暄不抽）       | 噪声 / 成本   | 低   |
| **P0** | 结构化 prompt + JSON schema           | 格式 / 解析   | 低   |
| **P0** | 置信度软排序（不用硬过滤）            | 检索质量      | 低   |
| **P1** | 记忆面板（用户可看 / 改 / 删）        | 长期准确性    | 中   |
| **P1** | 时间衰减 + cross-source 一致性        | 旧事实 / 孤证 | 中   |
| **P2** | 后台 consolidation（合并 / 归纳）     | 知识密度      | 中高 |
| **P2** | Grounding check（限显式 + 高 stakes） | hallucination | 高   |
| **P3** | 多 pass 集成抽取                      | 稳定性        | 高   |
| **P3** | 完整版本化（Zep 风格）                | 审计 / 回滚   | 高   |
| **P3** | 检索失败时动态补抽                    | 漏抽补救      | 高   |

> **关键判断**：**P0 + P1 已经能覆盖 80% 的生产场景**。P2 / P3 是研究型 / 高 stakes 才需要。

### 13.4 工业 extract 准确率的诚实版

- 业界 extract 准确率（端到端问答 F1）大致在 **70–85%**，具体取决于场景。
- 没有任何项目能稳定突破 90%——瓶颈是三个解不掉的问题：**LLM 校准差 / 隐式事实系统性漏抽 / 长上下文漂移**。
- **差异化的关键不在"抽得多准"，在"抽错了怎么补救"**——这是和直觉相反的工程判断。

## 16) 框架设计：可插拔的 Save + Recall

> 一个通用 memory 框架的核心价值是**把 Save 和 Recall 拆成可插拔的 stage**——而不是押注某一具体范式。Memory 的"对"高度场景化，单一 opinion 必然卡住一部分用户。框架要做的是**稳定 interface + 默认实现 + plumbing**，让 80% 用户不用想、20% 用户能换。

### 16.1 为什么必须可插拔

回看前 15 节的所有讨论：

- Extract 有用，但不是所有场景都要——有些场景纯 raw 就够
- Atomic fact vs episode vs summary，三种粒度都各有适用场景
- 3-tier 分层、raw+metadata、懒抽取，三种范式都各有 trade-off
- 一旦 query 分布变了，最优架构也跟着变

**结论**：**没有任何一种"默认架构"能覆盖所有场景**。通用框架的赌注**不应该押在"哪种范式最好"**，而应该押在**"哪种接口最稳"**。

### 16.2 架构形态：两条 Pipeline

```
SAVE 方向（消息进来 → 写入记忆）：
[Message] → Gate → Extract → Embed → Enrich → Store
              ↑        ↑        ↑        ↑        ↑
            可换      可换      可换      可换      可换

RECALL 方向（query 进来 → 取回记忆）：
[Query] → Parse → Retrieve → Rerank → Filter → Format
            ↑         ↑         ↑        ↑         ↑
          可换       可换       可换      可换      可换
```

两条 pipeline 共享：scope（用户 / agent / session）、observability（trace + cost + latency）、**raw archive**（永远是 source of truth）。

### 16.3 Stage Interface 的设计原则

每个 stage 应该是一个**带 next 调用的 middleware**——而不是孤立的 function：

```text
process(ctx, next) -> result
  - ctx 携带：上游 stage 的输出、scope、配置
  - next 调用：触发下游 stage
  - 可改 ctx.additions / ctx.candidates
  - 可短路（不调 next，提前返回）
  - 可 fallback（next 抛错时降级）
```

**为什么是 middleware 而不是独立函数**：

- 短路 / skip / fallback 是天然需求
- 可观测性插入点统一
- 错误处理边界清晰

**几个关键决策**：

1. **Context 是 mutable**——stage 之间通过 `ctx.additions` / `ctx.candidates` 共享数据（和 Beam / Koa / Express 一致）。
2. **Type 兼容靠约定 + 文档**——每个 stage 声明它依赖 ctx 的什么字段、产出什么字段；不强制 schema（避免过度设计）。
3. **Async-first**——LLM 调用、向量检索都是 async。
4. **可短路**——Gate 判 false 整个 Save 提前结束；Filter 后没候选整个 Recall 提前结束。
5. **每个 stage 单独可观测**——latency、token cost、输入输出大小都打点。

### 16.4 默认要提供的 stage

**Save 方向**：

| Stage       | 默认实现                 | 可换方向                                   |
| :---------- | :----------------------- | :----------------------------------------- |
| **Gate**    | LLM 判别 + 规则 fallback | 规则 only / 用户明示 / 跳过                |
| **Extract** | atomic fact（mem0 风格） | entity / episode / summary / 跳过          |
| **Embed**   | OpenAI text-embedding-3  | bge-m3 / 本地模型 / 多 embedding           |
| **Enrich**  | 时间戳 + 来源 + scope    | 自定义 metadata                            |
| **Dedupe**  | semantic dedup at write  | 显式 merge / invalidation / 跳过           |
| **Store**   | pgvector                 | Qdrant / Milvus / Postgres + graph / Redis |

**Recall 方向**：

| Stage        | 默认实现              | 可换方向                                           |
| :----------- | :-------------------- | :------------------------------------------------- |
| **Parse**    | query 改写 + 实体抽取 | 跳过 / 只做时间过滤                                |
| **Retrieve** | vector only           | BM25 / hybrid RRF / graph traversal / multi-source |
| **Rerank**   | 默认无                | cross-encoder / LLM rerank / 规则                  |
| **Filter**   | 时间 + 置信度         | supersede / 访问频率 / 用户偏好                    |
| **Format**   | top-K 拼接            | 选段 / summary / 结构化                            |
| **Augment**  | 默认无                | 加 raw source pointer / 加 cross-ref               |

**关键**：**每个 stage 至少给一个"开箱即用"实现**——用户用框架第一步是"什么都不换就能跑"。

### 16.5 框架应该承担的"非业务"职责

这些**框架做、用户不写**：

- **Async runtime**——stage 调度、并发、超时
- **Observability**——每个 stage 的 trace、latency、cost
- **Caching**——可配置，按 stage 维度
- **Error handling**——stage 失败 fallback / skip / 重试
- **Type coercion**——上游 stage 输出不匹配时降级
- **Cost tracking**——token 累计、跨 stage 分摊
- **Raw archive**——永远保留 raw，可以回查

**这些"非业务"职责**是框架的核心价值——用户写 stage 逻辑，框架做 plumbing。

### 16.6 现成参考

设计 Save+Recall pipeline 时，下面这些系统值得**逐个研究**：

| 系统               | 取经点                                               | 不取经的点                      |
| :----------------- | :--------------------------------------------------- | :------------------------------ |
| **flowcraft**      | pipeline stages 显式可组合；Go 风格的 interface 设计 | 它的"3 路 + RRF"是 opinion      |
| **Haystack**       | Pipeline 类；component 间用 dataclass 传数据         | 它是文档 RAG，不针对 memory     |
| **DSPy**           | Module 抽象 + Signature 概念                         | 偏 prompt 优化，pipeline 概念弱 |
| **Apache Beam**    | Windowing / watermark / 错误处理                     | 是数据流，不针对 LLM 场景       |
| **Koa middleware** | 简洁的 `(ctx, next) =>` 模式                         | 是 HTTP，不是异步 LLM pipeline  |
| **LangGraph**      | graph-based 编排、状态管理                           | 是 graph，不是 linear pipeline  |

**推荐组合**：从 **flowcraft + Haystack + Koa** 三者各取所需——flowcraft 的 stage 粒度，Haystack 的 component 抽象，Koa 的 middleware pattern。

### 16.7 反模式（不要做的事）

1. **不要在框架里 hardcode retention / decay 策略**——不同场景差太多；提供 hook，让用户实现。
2. **不要把 raw 设计成可选**——raw 永远是 source of truth，可丢弃应该是显式 opt-in。
3. **不要隐藏 cost**——每个 stage 的 token / latency 必须暴露。
4. **不要假装"无 opinion"**——即使最灵活的框架也有 opinion；明确说出来。
5. **不要试图做"AI 自动选 stage"**——meta-learning 听起来美，实际不可控、不透明。
6. **不要把 framework 强耦合到具体 vector / graph DB**——抽象要稳，DB 是可换的。
7. **不要把 Save 和 Recall 绑成同一个 pipeline**——对称性只是表面的，配置、stage、cost 都不一样。

### 16.8 落地的最小可验证版本

如果你正在设计这样的框架，**最小可验证版本（MVP）**：

1. **Save 的 3 个 stage**：Gate + Extract (atomic fact) + Store (pgvector)
2. **Recall 的 3 个 stage**：Retrieve (vector) + Filter (time) + Format (top-K)
3. **Observability**——每 stage 一次 trace、cost、latency
4. **一个 escape hatch**——用户能加自定义 stage
5. **一个真实场景**——跑通一两个典型 query

**这一步通了，框架的 80% 价值就建立了**。剩下的 stage 是"加 feature"。

## 相关链接

- `ai/agent.md`（Agent 整体架构与失败模式，memory 是其子模块）
- `ai/prompt.md`、`ai/prompt_engineering.md`（长上下文、注入防护、记忆在 prompt 中的注入方式）
- `ai/evals.md`（评测驱动开发，memory 系统也要进回归集）
