# Engineering Practice（sanitizers / UB / ODR）

## TL;DR

- **Sanitizers 是 C++ 工程里的"必备体检"**：开启成本低（编译期加 `-fsanitize=...`），能抓到的 bug 范围远超静态分析，**比单元测试/valgrind 更早介入**。**日常开发/CI 默认开 ASan + UBSan**，性能敏感场景再单独评估。
- **Undefined Behavior 不是"会崩"，是"编译器可以选择怎么做都行"**。有符号溢出、严格别名、空指针解引用、shift 越界、未初始化读、对象生命周期错乱等都是 UB——它们能编译通过、能跑、且偶尔"看起来对"，是头号隐形炸弹。
- **ODR（One Definition Rule）违反**与一般的"符号找不到/重复"不同：它常常**编译链接都不报**，直到运行时同一个符号在不同翻译单元里身份不同，直接崩在 vptr/thunk 上，且**不可复现**。
- Sanitizers、正确的 UB 认识、ODR 意识三件套，是把 C++ 从"玄学"拉回"工程"的核心抓手。

## 相关链接

- `compiling.md`（`undefined reference` / `duplicate symbol` / ODR 的链接期表现；LTO/PGO 与 sanitizer 的冲突）
- `memory.md`（ASan/LSan 抓的就是内存生命周期问题）
- `multi_thread.md`（TSan 抓的是 data race，与 happens-before 强相关）
- `keywords.md`（UB 与 `const_cast`、`reinterpret_cast`、union 关系紧密）
- `oop.md`（多态、虚表、对象切片等都和 UB/ODR 有关）
- `template_stl.md`（模板的"定义可见性"和隐式实例化是 ODR 的高频踩点）

## 1) 主流 Sanitizers 一览

> 一个共同约定：sanitizer **不是"运行时检测器"那样透明**——它会**改写代码**（在每条内存访问前后插桩、给每条对象加 shadow memory 等）。这意味着 **sanitizer 下能跑的代码** ≠ **无 sanitizer 下能跑的代码**，sanitizer 是用来**找 bug 的**，不是用来上线的产品构建。

| Sanitizer                               | 抓什么                                                                                                   | 什么时候用它                                             | 编译选项                                                                  |
| :-------------------------------------- | :------------------------------------------------------------------------------------------------------- | :------------------------------------------------------- | :------------------------------------------------------------------------ |
| **ASan**（AddressSanitizer）            | 堆/栈/全局 buffer overflow、use-after-free、double-free、stack-buffer-overflow、use-after-return（选项） | 任何怀疑内存越界/UAF 的场景；**默认开**                  | `-fsanitize=address`                                                      |
| **LSan**（LeakSanitizer）               | 内存泄漏（不终止进程的报告见 `memory.md`）                                                               | 想在 ASan 之外单独抓泄漏时                               | `-fsanitize=leak`（也常被 ASan 自动启用）                                 |
| **UBSan**（UndefinedBehaviorSanitizer） | 有符号溢出、空指针解引用、shift 越界、严格别名、对齐错误、整型除零、bool 强转等                          | 默认开；**能抓的 UB 数量是 ASan/TSan/MSan 之和的好几倍** | `-fsanitize=undefined`（可细分，如 `-fsanitize=signed-integer-overflow`） |
| **TSan**（ThreadSanitizer）             | 数据竞争（基于 happens-before）                                                                          | 多线程/共享状态出现"偶发错误"时                          | `-fsanitize=thread`                                                       |
| **MSan**（MemorySanitizer）             | 未初始化内存的读                                                                                         | 输入来自不可信源（用户输入/网络/文件反序列化）           | `-fsanitize=memory`（**仅 Clang**，GCC 不支持）                           |

常见"开多个 sanitizer"的兼容性提示：

- ASan + TSan：**互相不兼容**，不能同时开。
- MSan + ASan：Clang 下**不能同时开**（不同 shadow memory 布局）。
- UBSan + 其它：可以叠加，**强烈推荐** `-fsanitize=undefined,address` 做日常构建。

最小可用编译命令：

```bash
# 日常开发/CI 推荐：debug 符号 + 适度优化 + ASan + UBSan
clang++ -g -O1 -fno-omit-frame-pointer -fsanitize=address,undefined -o prog main.cpp
# 多线程怀疑 data race
clang++ -g -O1 -fsanitize=thread -o prog main.cpp
# GCC 也支持 ASan/UBSan/TSan，但 MSan 仅有 Clang
g++     -g -O1 -fsanitize=address,undefined -o prog main.cpp
```

> **优化级别的工程经验**：sanitizer 配 `-O0` 能编译最快，但**部分 UB 的"优化后表现"在 `-O0` 下抓不到**（如严格别名、未初始化读在 `-O0` 下甚至未必暴露）。**推荐 `-O1` + `-fno-omit-frame-pointer`**，兼顾可读性、栈回溯质量与 sanitizer 覆盖度。

## 2) UBSan 能抓的 UB 速查（带短例）

> **UB = Undefined Behavior**：标准说"行为未定义"，**编译器可以**据此做**任何假设**——包括"假设你不会触发 UB"去激进优化。UB 不一定当场崩，但**一旦编译器按这个假设优化，原本对的代码就可能变错**。

### 2.1 有符号整数溢出

```c++
int x = INT_MAX;
x = x + 1;        // UB（C++20 之后 wrapping 是有定义的；之前的代码仍是 UB）
```

> UBSan 默认**报告但不中止**；想中止可用 `-fno-sanitize-recover=all` 或单点 `-fno-sanitize-recover=signed-integer-overflow`。

### 2.2 空指针解引用

```c++
int* p = nullptr;
* p = 42;         // UB
```

`new T` 失败时**抛 `std::bad_alloc`**，不是返回 `nullptr`——所以 `if (p == nullptr) ...` 这种"防御"对 `new` 是无效的（`memory.md` 已有例子）。**对 raw `malloc` 才有判空的必要**。

### 2.3 整数除零 / 取模零

```c++
int a = 1 / 0;            // UB
int b = 1 % 0;            // UB
```

浮点除零是**有定义的**（产生 inf / NaN），只有整数除零才是 UB。

### 2.4 `shift` 越界 / 负数

```c++
int x = 1 << 32;          // UB：shift amount >= 位宽
int y = 1 << -1;          // UB：负数 shift
int z = INT_MIN >> 1;     // 由实现定义（有符号右移）
```

> C++20 起：左移有符号正数**且**结果在无符号对应类型里能表示，则**有定义**（即 `1 << 31` 在 32-bit int 上是 UB，但同样的 1 在 unsigned 上没问题）。**别在工程里依赖这种细节**，直接 `unsigned` 起步或用位域/掩码。

### 2.5 严格别名（strict aliasing）

```c++
int x = 0;
float* fp = reinterpret_cast<float*>(&x);
float y = *fp;            // UB：通过非 `float` 类型指针读 `float` 对象
```

> 这是 C/C++ 优化最常"咬人"的地方。**安全 reinterpret 内存的常见替代**：`std::memcpy`、`std::bit_cast`（C++20）、`std::launder`（极少数场景）。

### 2.6 对象生命周期错乱（use-after-free / 悬空引用）

```c++
const std::string& s = *new std::string("tmp");
delete &s;
// s 仍指向已经析构的对象，使用 s 是 UB
```

`memory.md` 已有"双重释放 / 释放不匹配 / 悬空引用"等典型场景；**ASan 抓生命周期问题，UBSan 抓"-fno-sanitize=object-size / vptr"**，两者互补。

### 2.7 `bool` 强转 / 非 0/1 赋给 `bool`

```c++
bool b = 2;        // 合法，但条件表达式里的"非 0/1 整数"曾是 UB（部分标准版本）
int x = b + 1;     // b 只能是 0/1，但旧标准下"赋非 0/1 给 bool"是 UB
```

> C++17 起已**不再**是 UB（值会被规整为 0/1），但老代码里这种 UB 经常让 optimizer 做出"疯狂"假设。

### 2.8 对齐错误的访问

```c++
alignas(8) int x;
int* p = &x;
*reinterpret_cast<int*>(reinterpret_cast<char*>(&x) + 1) = 0;  // UB：未对齐访问
```

> 多数 x86 上未对齐访问**只是慢**，ARM/SPARC 上**直接 trap**。协议解析 / packed struct 经常踩，**见 `memory.md`**。

## 3) ASan / TSan / MSan 典型表现

- **ASan** 报 `heap-buffer-overflow` / `heap-use-after-free` / `stack-buffer-overflow` / `double-free` / `global-buffer-overflow`，含完整栈回溯 + 被越界对象的大小与影子内存地址。
- **TSan** 报 `data race ... write at 0x... by thread T1 / read at 0x... by thread T2`，并标出两边栈。**前提是同步原语必须用 C++ 标准库**（`std::mutex/std::atomic`）——TSan 不认识自造锁/操作系统原语（除非用 `__tsan_acquire/__tsan_release` 显式标注）。
- **MSan** 报 `use-of-uninitialized-value`，**只有 Clang 有**。**与 ASan 互斥**，要单开。

> **跑 sanitizer 时常见的"误报"**：自定义内存池/线程局部池/汇编实现的同步原语，常被 sanitizer 误读。常见解法：用 sanitizer-friendly 的替代实现跑测试，或在特定文件上用 `__attribute__((no_sanitize("address")))` 局部关闭（务必加注释说明原因）。

## 4) ODR（One Definition Rule）——比"链接错误"更危险的那一类

> ODR 的核心要求：**每个"非内联函数/非模板变量"在整个程序中只能有一个定义**；**class/function/template 在每个翻译单元中的"定义"必须 token-by-token 一致**（含 `inline` 修饰、`constexpr` 标记、模板参数等）。

违反 ODR 的后果：

- **编译器多半不报**：同一签名、同一类型，两份定义长得一致，链接器不比对 token 内容。
- **链接器多半不报**：链接器只管"符号唯一"，不管"内容是否相同"。
- **运行时崩**：vptr 错位、thunk 选错、inline 函数在不同 TU 的优化等级下被不同地展开 → 同一对象在 A 模块里是 `Dog`、在 B 模块里是 `Cat`，调用虚函数直接段错误。

### 4.1 头文件里"看起来是定义"的常见 ODR 雷区

- **非 `inline` 的普通函数定义**放在 `.h` 并被多个 `.cpp` 包含 → 多份定义（典型 `duplicate symbol` 错误，但加了 `static` / `inline` 后表现又不同）。
- **C++17 之前**非 `inline` 的全局变量定义放在头文件里 → 多份定义（C++17 的 `inline` 变量解决的就是这个）。
- **类模板 / 变量模板的隐式实例化**：模板在每个使用点都生成"候选"定义，ODR 要求它们必须 token-by-token 一致。详见 `template_stl.md`。
- **`inline` 函数 / 变量的定义在多个 TU 中可见**是 ODR **允许**的，但**所有 TU 中的定义必须完全一致**（含 `inline` 关键字本身、`constexpr`/`consteval` 标记等）。

### 4.2 跨 SO/DLL 的同符号冲突（最坑的一类）

```text
app.so  -- depends on -->  libA.so
app.so  -- depends on -->  libB.so
libA.so -- depends on -->  libCommon.so (v1)
libB.so -- depends on -->  libCommon.so (v2)
```

- **符号层面**：`libA` 和 `libB` 在自己眼里各用各的"同符号"。**链接器不报**（名字一致且仅一份被选择）。
- **类型 / ODR 层面**：若 `libCommon` 的 v1/v2 对同一 class 的布局、虚函数、成员函数实现**有差异**（哪怕只是字段顺序调整），那么通过 `libA` 创建的对象传到 `libB` 时，`libB` 里的 vptr/类型信息是 v2 视角的——直接 UB / 段错误。
- **不是 sanitizer 能直接抓的**：要在**所有依赖方**用同一份 `libCommon`（vendor/soname 对齐），或者把易变类型做成 ABI 稳定的 C 接口。

> 同一问题在 Windows 上叫 **"两个不同的 CRT/DLL"**（MSVC 不同版本的 `msvcrt.dll`）：内存从一个 DLL `new` 出来、在另一个 DLL `delete`，不一定崩，但**理论上是 UB**，ASan/LSan 能抓到"被不同模块分配/释放"。

### 4.3 ODR 违反的工程抓手

- 头文件里的"可被多个 TU 看到"的定义：函数必须 `inline` 或只放声明；变量（C++17 前）必须 `extern` 声明 + 唯一定义。
- 类模板实现不要"半头半源"（声明在头、定义在 `.cpp`）而不显式实例化——`template_stl.md` 给了完整解法。
- 跨 SO/DLL 暴露 C++ 类型时，**所有依赖方必须用同一份**（同一 vendor/同一 major 版本），否则只能暴露 C 接口。
- 编译器选项不一致（`-fno-rtti` vs 默认；`-fno-exceptions` vs 默认；`-std=c++17` vs `-std=c++20`）会让同一 class 的**类型信息/异常表**不同——这本身不是 ODR 违反，但常常**和 ODR 违反叠加**出现"运行时崩在虚函数调用"的症状。

## 5) 与编译/链接相关的工程坑（交叉视角）

> 这一节只点出"工程上容易翻车但 `compiling.md` 没展开"的几条，**不重复该文已覆盖的细节**。

- **Release 构建未开 sanitizer**：开发/CI 用 ASan+UBSan，发布构建不带——**很多 UB 只在 `-O2/-O3` 下暴露**，release 路径下第一次跑出诡异问题就难复现了。**解法：fuzz/回归测试长期跑 sanitizer 版本**。
- **`-fsanitize=address` 改变了栈布局**（红色 zone）：和某些依赖栈地址的代码（协程上下文切换、setjmp/longjmp 校验栈指针）冲突。**遇到时再用 `__attribute__((no_sanitize_address))` 局部关闭**，并加注释。
- **TSan 与 `inline assembly` / 自旋自实现锁**：见上文，TSan 看不见自己写的同步，要标注或换成标准库。
- **PGO + sanitizer 一起开**：可以，但 PGO 的 profile 文件在 sanitizer 下与正常构建**字节布局不同**，profile 不可重用——**分两条编译线**。
- **LTO + ASan**：现代 Clang/GCC 已经支持，但**链接时间显著增加**且 DWARF 体积爆炸，调试体验下降。
- **静态库（`.a`）/动态库（`.so`/`.dll`）混用不同 `-std=`**：C++ ABI 在不同标准下细节有差，混用会带来"同一符号两个不同实现"的 ODR 风险。

## 6) 工程建议（CI/日常构建配方）

> 推荐的层级（从最廉价到最昂贵，挑你能负担的层级）：

1. **本地开发**：默认开 ASan + UBSan（`-g -O1 -fno-omit-frame-pointer -fsanitize=address,undefined`），CI 一致。
2. **CI**：再加 TSan 跑多线程回归测试集（**ASan + TSan 不能同进程同时开**，要分两套 binary）。
3. **可选 CI**：MSan 跑反序列化/解析路径（Clang only），抓"未初始化读"——这一类是 ASan/UBSan 都不管的。
4. **Release / 性能构建**：关 sanitizer，**保留 PGO 收集 + `-fprofile-use`** 流程；性能数据在 sanitizer 下不真实。
5. **重大重构/上线前**：在多组输入下跑 `perf record` + 火焰图（见 `performance.md`），确保无 ODR/UB 引入的"看起来对"。
