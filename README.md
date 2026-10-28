# l1ang70's field-notes

A personal knowledge base in Markdown — short, opinionated notes written for quick recall, not as tutorials. Topics: Go, Rust, OS internals, AI engineering.

**62 files · 4 areas · last touched 2026-10-27.**

## What this is (and what it isn't)

- **A personal notebook.** Pages are terse, assume the basics, and focus on the counter-intuitive bits and engineering handles.
- **Not a tutorial, not a reference, not a beginner's guide.** Don't expect coverage from first principles.
- **May not be fully accurate.** These are my mental models and shorthand — verify before relying on them.

## License

[CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/) — feel free to reuse with attribution. Full text in [LICENSE](LICENSE).

## Index

### Go (22 files)

Architecture (layering, contract + `impl:` registry, craft ABI, plugin host), engineering (conventions, testing layout, releases, observability, a desktop-agent performance record), build & packaging (dev/release, embed, cross-platform), concurrency (channels, patterns, sync_map, scenarios), language (defer/panic, escape analysis, interface/reflection), runtime (gc, scheduler, allocator, write barrier, netpoll, pprof).

- `go/architecture/layering.md` — four layers, contract + registry + assembly resource
- `go/architecture/craft_abi.md` — platform/sidecar ABI, grants and leases, plugin host
- `go/engineering/conventions.md` — errdefs, optional interfaces, panic boundaries, double-tree
- `go/engineering/testing.md` — unit/contract/e2e split, platform lanes, fuzz/soak, property gates
- `go/engineering/observability.md` — reason-coded logs, single-source metrics, RUM, trace to dataset
- `go/engineering/release.md` — multi-module versioning, changesets, release gate, desktop signing
- `go/engineering/optimization.md` — desktop agent performance work: baseline, allocation, bounds, serialization, caching
- `go/build/packaging.md` — dev vs release flags, version injection, embed, cross-platform packaging
- `go/concurrency/channels.md` — channel semantics
- `go/concurrency/concurrency_patterns.md` — common patterns
- `go/concurrency/concurrency_scenarios_templates.md` — scenarios & templates
- `go/concurrency/sync_map.md` — `sync.Map`
- `go/language/defer_panic_recover.md` — defer / panic / recover
- `go/language/escape_analysis.md` — escape analysis
- `go/language/escape_analysis_cases.md` — escape analysis cases
- `go/language/interface_reflection.md` — interface & reflection
- `go/runtime/gc.md` — GC
- `go/runtime/memory_allocator.md` — memory allocator
- `go/runtime/scheduler.md` — goroutine scheduler
- `go/runtime/write_barrier.md` — write barrier
- `go/runtime/netpoll.md` — netpoll networking
- `go/runtime/pprof.md` — reading heap/goroutine/block profiles, paired sampling, misreads

### Rust (13 files)

Language basics (ownership, traits, interior mutability, Option/Result), memory layout and allocation, workspace shape (crate vs module boundaries, features, generated wire code), the unsafe boundary (one crate, `forbid` elsewhere), error taxonomy across the Go/Rust two-tree split, bounded containers, the async/blocking boundary, tokio signalling and generation swaps, and the platform-layer test layout.

- `rust/language/ownership.md` — move vs borrow, `&mut` as exclusivity, lifetimes only where needed, `Drop` as rollback
- `rust/language/traits.md` — generics vs `dyn`, monomorphization, associated types, orphan rule
- `rust/language/interior_mutability.md` — Cell/RefCell/OnceLock/atomics, `Send`/`Sync`, which lock
- `rust/language/option_result.md` — what `?` desugars to, combinators, where `anyhow` stops
- `rust/runtime/memory.md` — where values live, `size_of` table, Vec/Arc costs, drop order, zero-cost boundaries
- `rust/runtime/allocator.md` — `Layout` / `GlobalAlloc` contract, swapping the global allocator, arena pitfalls
- `rust/architecture/workspace.md` — two packages, one-way module graph pinned by a test, feature layering, generated code
- `rust/architecture/unsafe_boundary.md` — `forbid(unsafe_code)` and the single crate allowed to break it
- `rust/engineering/errors.md` — 14 categories aligned with Go, why a gRPC status code is lossy on the wire
- `rust/engineering/bounds.md` — bound + reclaim + observe, prune-by-expiry, the container that has no bound yet
- `rust/engineering/async_blocking.md` — async trait / sync implementation, `spawn_blocking`, the lint gate
- `rust/engineering/testing.md` — reusable conformance suite, structural layering test, `cfg`-scoped platform tests
- `rust/runtime/tokio.md` — `watch` vs `broadcast`, explicit lag events, generation swap and drain, which mutex

### OS (10 files)

Process/scheduling, memory, concurrency primitives, io, filesystem, payload formats, virt/containers, network stack, scheduler comparison (CFS / UMS / ULE / GMP / tokio), and sandbox/isolation mechanisms (bwrap / seatbelt / AppContainer / WFP / cgroups).

- `os/memory.md` — memory management
- `os/process.md` — process & scheduling
- `os/concurrency.md` — concurrency primitives
- `os/filesystem.md` — filesystem
- `os/io.md` — I/O
- `os/payload.md` — system load and observability metrics (CPU / memory / disk / network / QPS thresholds and tooling)
- `os/virtualization.md` — virtualization / containers
- `os/network_stack.md` — network stack
- `os/scheduler_comparison.md` — scheduler comparison (CFS / UMS / ULE / GMP / tokio)
- `os/sandbox.md` — sandbox & process isolation mechanisms (bwrap / seatbelt / AppContainer / WFP, pty, cgroups, deny reasons)

### AI (17 files)

Agent fundamentals, prompt engineering (skeleton / patterns / anti-patterns / injection defense), evals, memory systems, RAG, tools/function calling/MCP, multi-agent, context engineering, self-hosted inference serving, speech/audio pipelines, and a `harness/` subdirectory for the runtime/operational layer (sandbox, control loop, retry, state, observability, I/O, lifecycle).

- `ai/agent.md` — agent fundamentals
- `ai/prompt_engineering.md` — prompt skeleton, patterns, anti-patterns, injection defense (the old `prompt.md` was merged in)
- `ai/evals.md` — evaluation methodology
- `ai/memory.md` — memory systems (mem0, Letta, LangMem, Cognee, Zep, …)
- `ai/rag.md` — RAG / knowledge retrieval
- `ai/tools.md` — tool use / function calling / MCP
- `ai/multi_agent.md` — multi-agent orchestration
- `ai/context_engineering.md` — context engineering
- `ai/inference_serving.md` — self-hosted inference serving: capacity ledger, startup self-checks, resource sync, CI boundary
- `ai/speech_audio.md` — realtime voice chain (RTC) and offline audio pipelines (VAD / diarization / ASR / voiceprint / DER)
- `ai/harness/` — runtime/operational layer: sandbox, control loop, resilience, state, observability, I/O, lifecycle

**AI harness (runtime layer, 7 files)**:

- `ai/harness/sandbox.md` — sandbox & permission enforcement
- `ai/harness/control_loop.md` — control loop implementation & stopping rules
- `ai/harness/resilience.md` — retry / timeout / compaction triggers
- `ai/harness/state.md` — persistence & checkpoint
- `ai/harness/observability.md` — logging / trace / cost
- `ai/harness/io.md` — streaming / interrupt / HITL / progress
- `ai/harness/lifecycle.md` — process spawn / isolation / termination

## How to use

- **Browse**: scroll the Index above
- **Search**: `rg "keyword"` from the repo root (ripgrep)
