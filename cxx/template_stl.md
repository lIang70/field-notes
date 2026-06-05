# Template & STL（工程向）

## TL;DR

- 模板相关的编译/链接问题，核心抓手是：**实例化发生在使用点（translation unit）**，以及你是否把“定义”放在了可见位置。
- STL 的工程关键不是背 API，而是：**复杂度预期**、**迭代器/引用失效规则**、**异常安全**、以及“值语义 + RAII”。

## 相关链接

- `compiling.md`（undefined reference / duplicate symbol / ODR）
- `modern/c++11.md`（移动语义、智能指针、lambda）
- `modern/c++17.md`（`if constexpr`、`variant/optional/string_view`）

## 1) 模板实例化与链接期错误（最常见）

### 为什么会有 `undefined reference`（模板版）

典型场景：你把模板定义写在 `.cpp` 里，其他 `.cpp` 只看到声明。

```c++// foo.h
template <class T>
T add(T a, T b);
```

```c++// foo.cpp
template <class T>
T add(T a, T b) { return a + b; }
```

```c++// main.cpp
#include "foo.h"
int main() { return add(1, 2); }
```

这通常会在链接期爆 `undefined reference to add<int>(int,int)`，因为 `main.cpp` 需要 `add<int>` 的定义，但它不在这个翻译单元可见（而 `foo.cpp` 里虽然有模板定义，但除非它也实例化出了 `add<int>`，否则不会产生符号）。

### 工程解法（按推荐顺序）

- **解法 A（最常用）**：把模板定义放到头文件（或 `.hpp/.tpp`）里，让使用点可见。
- **解法 B（显式实例化）**：在某个 `.cpp` 里显式实例化需要的具体类型：

```c++// foo.cpp
template <class T>
T add(T a, T b) { return a + b; }

template int add<int>(int, int); // 显式实例化定义
```

并在头文件里加显式实例化声明（可选，用于减少重复实例化）：

```c++// foo.h
extern template int add<int>(int, int);
```

> 抓手：模板要么“头文件可见”，要么“你保证实例化产物在某处生成并被链接到”。

## 2) ODR（One Definition Rule）与 `inline`

### 头文件里哪些东西会导致 duplicate symbol

- 非 `inline` 的普通函数定义
- 非 `inline` 的全局变量定义（C++17 以前尤其常见）
- 非 `static` 且具有外部链接的对象定义

### 工程解法

- 函数：头文件放声明，定义放 `.cpp`；或确实需要头文件定义就用 `inline`。
- 变量：C++17 起可用 `inline` 变量；否则 `extern` 声明 + 单一定义。

## 3) SFINAE → `enable_if` → Concepts（演进路线）

### SFINAE / `enable_if`（C++11/14）

- 适用：你需要“按类型启用/禁用重载”。
- 成本：错误信息难读、可维护性差。

```c++
template <class T, class = std::enable_if_t<std::is_integral<T>::value>>
void f(T) {}
```

### `if constexpr`（C++17）

- 更适合“同一个模板函数内部按类型分支”。

### Concepts（C++20）

- 最适合“表达接口约束”，让调用点和错误信息更友好（见 `modern/c++20.md`）。

## 4) STL：复杂度与常见容器选型

### 选型速记（工程版）

- `std::vector`：默认首选；连续内存，遍历快，随机访问 O(1)。扩容会导致指针/引用/迭代器失效。
- `std::deque`：两端 push/pop 稳定；不连续；部分操作下失效规则不同于 vector。
- `std::list`：节点分配，遍历慢；迭代器稳定（删除本元素除外）；一般不推荐除非确实需要稳定迭代器且频繁 splice。
- `std::unordered_map`：平均 O(1) 查找；rehash 会导致迭代器失效；hash/负载因子会影响性能。
- `std::map`：有序、O(log n)；迭代器稳定性好；常用于需要有序遍历/范围查询的场景。

> 经验法则：能用 `vector` 就用 `vector`；需要 key 查找再考虑 `unordered_map/map`；别用 `list` 去“优化插入删除”，先用 profile 证明瓶颈。

## 5) 迭代器/引用失效（高频踩坑）

### `vector`

- `push_back/emplace_back` 可能触发扩容：导致 **所有指针/引用/迭代器失效**。
- `erase`：从擦除点到末尾的迭代器/引用都会失效（元素搬移）。

### `unordered_map`

- `rehash/reserve` 以及某些插入触发 rehash：迭代器通常会失效。
- 指向元素的引用/指针是否稳定依赖实现与标准细节，不建议写依赖此行为的代码。

工程抓手：

- 容器外持有指针/引用时，优先存 **索引/键** 或使用 `shared_ptr`（所有权明确）而不是存 raw pointer。

## 6) 异常安全（最小可用框架）

### 三个等级

- **基本保证**：异常抛出后对象仍可用、无资源泄漏，但状态可能改变。
- **强保证**：异常抛出后对象状态不变（要么成功要么无影响）。
- **不抛异常**：操作保证不抛（例如标记为 `noexcept`）。

### 工程建议

- 资源全部交给 RAII（容器/智能指针/锁守卫），避免手写 `try/catch` 做清理。
- 对“移动操作”标 `noexcept` 往往能让容器更愿意走 move（尤其是 reallocate 场景），避免退化为 copy。
