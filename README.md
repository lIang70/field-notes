# l1ang70's field-notes

A personal knowledge base in Markdown — short, opinionated notes written for quick recall, not as tutorials. Topics: C++, Go, OS internals, AI engineering.

**51 files · 4 areas · last touched 2026-06-08.**

## What this is (and what it isn't)

- **A personal notebook.** Pages are terse, assume the basics, and focus on the counter-intuitive bits and engineering handles.
- **Not a tutorial, not a reference, not a beginner's guide.** Don't expect coverage from first principles.
- **May not be fully accurate.** These are my mental models and shorthand — verify before relying on them.

## License

[CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/) — feel free to reuse with attribution. Full text in [LICENSE](LICENSE).

## Index

### C++ (13 files)

Keywords, language basics, memory/ownership/RAII, compiling, oop, templates/STL, multithreading, performance, sanitizers/UB/ODR, modern (c++11/14/17/20).

- `cxx/keywords.md` — keyword reference
- `cxx/language_features.md` — language basics
- `cxx/memory.md` — lifetime, ownership, RAII
- `cxx/compiling.md` — compiling & linking
- `cxx/oop.md` — object-oriented
- `cxx/template_stl.md` — templates & STL
- `cxx/multi_thread.md` — multithreading
- `cxx/performance.md` — performance analysis (perf / benchmark)
- `cxx/engineering_practice.md` — sanitizers, UB, ODR
- `cxx/modern/c++11.md` — C++11
- `cxx/modern/c++14.md` — C++14
- `cxx/modern/c++17.md` — C++17
- `cxx/modern/c++20.md` — C++20

### Go (13 files)

Concurrency (channels, patterns, sync_map, scenarios), language (defer/panic, escape analysis, interface/reflection), runtime (gc, scheduler, allocator, write barrier, netpoll).

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

### OS (9 files)

Process/scheduling, memory, concurrency primitives, io, filesystem, payload formats, virt/containers, network stack, scheduler comparison (CFS / UMS / ULE / GMP / tokio).

- `os/memory.md` — memory management
- `os/process.md` — process & scheduling
- `os/concurrency.md` — concurrency primitives
- `os/filesystem.md` — filesystem
- `os/io.md` — I/O
- `os/payload.md` — payload formats
- `os/virtualization.md` — virtualization / containers
- `os/network_stack.md` — network stack
- `os/scheduler_comparison.md` — scheduler comparison (CFS / UMS / ULE / GMP / tokio)

### AI (16 files)

Agent fundamentals, prompt (design/engineering), evals, memory systems, RAG, tools/function calling/MCP, multi-agent, context engineering, and a new `harness/` subdirectory for the runtime/operational layer (sandbox, control loop, retry, state, observability, I/O, lifecycle).

- `ai/agent.md` — agent fundamentals
- `ai/prompt.md` — prompt design
- `ai/prompt_engineering.md` — prompt engineering patterns & anti-patterns
- `ai/evals.md` — evaluation methodology
- `ai/memory.md` — memory systems (mem0, Letta, LangMem, Cognee, Zep, …)
- `ai/rag.md` — RAG / knowledge retrieval
- `ai/tools.md` — tool use / function calling / MCP
- `ai/multi_agent.md` — multi-agent orchestration
- `ai/context_engineering.md` — context engineering
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
