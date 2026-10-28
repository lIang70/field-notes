# l1ang70's field-notes

A personal knowledge base in Markdown — short, opinionated notes written for quick recall, not as tutorials. Topics: Go, Rust, OS internals, AI engineering.

**62 files · 4 areas · last touched 2026-10-28.**

## What this is (and what it isn't)

- **A personal notebook.** Pages are terse, assume the basics, and focus on the counter-intuitive bits and engineering handles.
- **Not a tutorial, not a reference, not a beginner's guide.** Don't expect coverage from first principles.
- **May not be fully accurate.** These are my mental models and shorthand — verify before relying on them.

## License

[CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/) — feel free to reuse with attribution. Full text in [LICENSE](LICENSE).

## Index

### Go (22 files)

Architecture (layering, contract + `impl:` registry, craft ABI, plugin host), engineering (conventions, testing layout, releases, observability, a desktop-agent performance record), build & packaging (dev/release, embed, cross-platform), concurrency (channels, patterns, sync_map, scenarios), language (defer/panic, escape analysis, interface/reflection), runtime (gc, scheduler, allocator, write barrier, netpoll, pprof).

- [layering](go/architecture/layering.md) — four layers, contract + registry + assembly resource
- [craft_abi](go/architecture/craft_abi.md) — platform/sidecar ABI, grants and leases, plugin host
- [conventions](go/engineering/conventions.md) — errdefs, optional interfaces, panic boundaries, double-tree
- [testing](go/engineering/testing.md) — unit/contract/e2e split, platform lanes, fuzz/soak, property gates
- [observability](go/engineering/observability.md) — reason-coded logs, single-source metrics, RUM, trace to dataset
- [release](go/engineering/release.md) — multi-module versioning, changesets, release gate, desktop signing
- [optimization](go/engineering/optimization.md) — desktop agent performance work: baseline, allocation, bounds, serialization, caching
- [packaging](go/build/packaging.md) — dev vs release flags, version injection, embed, cross-platform packaging
- [channels](go/concurrency/channels.md) — channel semantics
- [concurrency_patterns](go/concurrency/concurrency_patterns.md) — common patterns
- [concurrency_scenarios_templates](go/concurrency/concurrency_scenarios_templates.md) — scenarios & templates
- [sync_map](go/concurrency/sync_map.md) — `sync.Map`
- [defer_panic_recover](go/language/defer_panic_recover.md) — defer / panic / recover
- [escape_analysis](go/language/escape_analysis.md) — escape analysis
- [escape_analysis_cases](go/language/escape_analysis_cases.md) — escape analysis cases
- [interface_reflection](go/language/interface_reflection.md) — interface & reflection
- [gc](go/runtime/gc.md) — GC
- [memory_allocator](go/runtime/memory_allocator.md) — memory allocator
- [scheduler](go/runtime/scheduler.md) — goroutine scheduler
- [write_barrier](go/runtime/write_barrier.md) — write barrier
- [netpoll](go/runtime/netpoll.md) — netpoll networking
- [pprof](go/runtime/pprof.md) — reading heap/goroutine/block profiles, paired sampling, misreads

### Rust (13 files)

Language basics (ownership, traits, interior mutability, Option/Result), memory layout and allocation, workspace shape (crate vs module boundaries, features, generated wire code), the unsafe boundary (one crate, `forbid` elsewhere), error taxonomy across the Go/Rust two-tree split, bounded containers, the async/blocking boundary, tokio signalling and generation swaps, and the platform-layer test layout.

- [ownership](rust/language/ownership.md) — move vs borrow, `&mut` as exclusivity, lifetimes only where needed, `Drop` as rollback
- [traits](rust/language/traits.md) — generics vs `dyn`, monomorphization, associated types, orphan rule
- [interior_mutability](rust/language/interior_mutability.md) — Cell/RefCell/OnceLock/atomics, `Send`/`Sync`, which lock
- [option_result](rust/language/option_result.md) — what `?` desugars to, combinators, where `anyhow` stops
- [memory](rust/runtime/memory.md) — where values live, `size_of` table, Vec/Arc costs, drop order, zero-cost boundaries
- [allocator](rust/runtime/allocator.md) — `Layout` / `GlobalAlloc` contract, swapping the global allocator, arena pitfalls
- [workspace](rust/architecture/workspace.md) — two packages, one-way module graph pinned by a test, feature layering, generated code
- [unsafe_boundary](rust/architecture/unsafe_boundary.md) — `forbid(unsafe_code)` and the single crate allowed to break it
- [errors](rust/engineering/errors.md) — 14 categories aligned with Go, why a gRPC status code is lossy on the wire
- [bounds](rust/engineering/bounds.md) — bound + reclaim + observe, prune-by-expiry, the container that has no bound yet
- [async_blocking](rust/engineering/async_blocking.md) — async trait / sync implementation, `spawn_blocking`, the lint gate
- [testing](rust/engineering/testing.md) — reusable conformance suite, structural layering test, `cfg`-scoped platform tests
- [tokio](rust/runtime/tokio.md) — `watch` vs `broadcast`, explicit lag events, generation swap and drain, which mutex

### OS (10 files)

Process/scheduling, memory, concurrency primitives, io, filesystem, payload formats, virt/containers, network stack, scheduler comparison (CFS / UMS / ULE / GMP / tokio), and sandbox/isolation mechanisms (bwrap / seatbelt / AppContainer / WFP / cgroups).

- [memory](os/memory.md) — memory management
- [process](os/process.md) — process & scheduling
- [concurrency](os/concurrency.md) — concurrency primitives
- [filesystem](os/filesystem.md) — filesystem
- [io](os/io.md) — I/O
- [payload](os/payload.md) — system load and observability metrics (CPU / memory / disk / network / QPS thresholds and tooling)
- [virtualization](os/virtualization.md) — virtualization / containers
- [network_stack](os/network_stack.md) — network stack
- [scheduler_comparison](os/scheduler_comparison.md) — scheduler comparison (CFS / UMS / ULE / GMP / tokio)
- [sandbox](os/sandbox.md) — sandbox & process isolation mechanisms (bwrap / seatbelt / AppContainer / WFP, pty, cgroups, deny reasons)

### AI (17 files)

Agent fundamentals, prompt engineering (skeleton / patterns / anti-patterns / injection defense), evals, memory systems, RAG, tools/function calling/MCP, multi-agent, context engineering, self-hosted inference serving, speech/audio pipelines, and a `harness/` subdirectory for the runtime/operational layer (sandbox, control loop, retry, state, observability, I/O, lifecycle).

- [agent](ai/agent.md) — agent fundamentals
- [prompt_engineering](ai/prompt_engineering.md) — prompt skeleton, patterns, anti-patterns, injection defense (the old `prompt.md` was merged in)
- [evals](ai/evals.md) — evaluation methodology
- [memory](ai/memory.md) — memory systems (mem0, Letta, LangMem, Cognee, Zep, …)
- [rag](ai/rag.md) — RAG / knowledge retrieval
- [tools](ai/tools.md) — tool use / function calling / MCP
- [multi_agent](ai/multi_agent.md) — multi-agent orchestration
- [context_engineering](ai/context_engineering.md) — context engineering
- [inference_serving](ai/inference_serving.md) — self-hosted inference serving: capacity ledger, startup self-checks, resource sync, CI boundary
- [speech_audio](ai/speech_audio.md) — realtime voice chain (RTC) and offline audio pipelines (VAD / diarization / ASR / voiceprint / DER)
- `ai/harness/` — runtime/operational layer: sandbox, control loop, resilience, state, observability, I/O, lifecycle

**AI harness (runtime layer, 7 files)**:

- [sandbox](ai/harness/sandbox.md) — sandbox & permission enforcement
- [control_loop](ai/harness/control_loop.md) — control loop implementation & stopping rules
- [resilience](ai/harness/resilience.md) — retry / timeout / compaction triggers
- [state](ai/harness/state.md) — persistence & checkpoint
- [observability](ai/harness/observability.md) — logging / trace / cost
- [io](ai/harness/io.md) — streaming / interrupt / HITL / progress
- [lifecycle](ai/harness/lifecycle.md) — process spawn / isolation / termination

## How to use

- **Browse**: the rendered site at <https://liang70.github.io/field-notes/> — cards per area, full-text search, dark mode
- **Browse (repo)**: scroll the Index above (every path is a link)
- **Search**: `rg "keyword"` from the repo root, or the site's search box (`/`)
- **Build / preview locally**: `make serve` → <http://127.0.0.1:8000> (MkDocs Material, config in `mkdocs.yml`)
