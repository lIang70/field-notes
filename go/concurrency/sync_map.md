# sync.Map Implementation

`sync.Map` 是 Go 官方提供的并发安全 Map，专为 **读多写少** (Read-Heavy) 场景优化。

## 核心设计思路

通过 **空间换时间**，维护两个 Map：

1. **read map**: 原子操作 (Atomic)，无锁读取。
2. **dirty map**: 普通 Map，需要互斥锁 (Mutex) 保护。

## 结构体

```go
type Map struct {
    mu Mutex
    read atomic.Value // 存储 readOnly 结构体
    dirty map[interface{}]*entry
    misses int // 记录 read map 穿透次数
}

type readOnly struct {
    m       map[interface{}]*entry
    amended bool // true 表示 dirty 中包含 read 中没有的数据
}
```

## 操作流程

### 1. Load (读)

1. 尝试从 `read` map 中读取。
2. 如果没找到，且 `amended` 为 true (表示 dirty 有新数据)：
   - 加锁 `mu`。
   - 二次检查 `read` (Double Check)。
   - 如果还不在 `read`，从 `dirty` 中读取。
   - **Misses 计数**: `misses++`。如果 `misses >= len(dirty)`，触发 **Promotion (晋升)**：将 `dirty` 提升为 `read`，清空 `dirty`，重置 `misses`。

### 2. Store (写/更新)

1. 如果 key 已经在 `read` 中：
   - 尝试通过 atomic CAS 更新值 (无锁)。
2. 如果 key 不在 `read` 中 (或 CAS 失败表示该条目已被标记删除)：
   - 加锁 `mu`。
   - 如果 key 在 `read` 中 (并发导致)：更新 entry。
   - 如果 key 在 `dirty` 中：直接更新。
   - 如果 key 都不在：
     - 如果 `dirty` 为 nil，从 `read` 复制未删除的数据创建新 `dirty`。
     - 将新 key 写入 `dirty`。
     - 设置 `read.amended = true`。

### 3. Delete (删)

- **延迟删除**: 删除操作只是将 entry 的指针置为 `nil` (或特殊标记 `expunged`)。
- 真正物理删除发生在 `dirty` 提升为 `read` 时，或者从 `read` 复制到 `dirty` 时（已删除的不会被复制）。

## 适用场景

- **缓存**: Key 集合稳定，读取频繁。
- **不适用**: 频繁插入新 Key 的场景（会导致频繁的 dirty -> read 晋升和数据复制，性能不如 `RWMutex + map`）。
