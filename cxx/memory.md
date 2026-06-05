# Memory

## TL;DR

- 把内存问题拆成两类最有效：**生命周期（lifetime）** 与 **所有权（ownership）**。大多数崩溃/泄漏/UB 都是这两点没说清。
- 工程上优先用 RAII（智能指针/容器）把“释放时机”交给类型系统；需要手动管理时，才去谈 `new/delete`、内存池、对齐等细节。

## 相关链接

- `modern/c++11.md`（RAII、`unique_ptr/shared_ptr`、移动语义）
- `keywords.md`（`const_cast`、`reinterpret_cast` 等与 UB 的关系）
- `multi_thread.md`（并发下的生命周期/数据竞争会放大内存问题）

## C++ 的内存分区

> C++ 中常见的内存分区描述: 栈、堆（更准确说：动态分配区）、全局/静态存储区（通常可再细分为 `.data`/`.bss`）、常量区（只读数据段）、代码区（`.text`）等。

```c++
// 高地址 -> +-----------------+
//          | Stack Segment   |
//        v +-----------------+
//          | Unused Memory   |
//        ^ +-----------------+
//          | Heap Segment    |
//      +---+-----------------+
//      v   | Global Data Seg |
//    data  +-----------------+
//      ^   | Constant Seg    |
//      +---+-----------------+
//          | Code Segment    |
// 低地址 -> +-----------------+
//
```

- **栈**: 在执行函数时, 函数内局部变量的存储单元都可以在栈上创建, 函数执行结束时这些存储单元自动被释放. 栈内存分配运算内置于处理器的指令集中, 效率很高, 但是分配的内存容量有限
- **堆**: 就是那些由 `new` 分配的内存块, 他们的释放编译器不去管, 由我们的应用程序去控制, 一般一个 `new` 就要对应一个 `delete`. 如果程序员没有释放掉, 那么在程序结束后, 操作系统会自动回收
- **自由存储区**：如果说堆是操作系统维护的一块内存, 那么自由存储区就是 C++ 中通过 `new` 和 `delete` 动态分配和释放对象的抽象概念. 需要注意的是, 自由存储区和堆比较像, 但不等价
- **全局/静态存储区**: 全局变量和静态变量具有静态存储期。实现上通常仍会区分已初始化（例如 `.data`）与零初始化（例如 `.bss`）两部分；未显式初始化的对象会被零初始化（例如 `int` 为 0，指针为 `nullptr`）。
- **常量存储区**: 这是一块比较特殊的存储区, 这里面存放的是常量, 不允许修改
- **代码区**: 存放函数体的二进制代码

## 结构体内存对齐问题

- 结构体内成员按照声明顺序存储, 第一个成员地址和整个结构体地址相同.
- 未特殊说明时, 按结构体中 size 最大的成员对齐 (若有 `double` 成员, 按 8 字节对齐).

### `alignas` & `alignof` (C++11)

> 其中 `alignof` 可以计算出类型的对齐方式, `alignas` 可以指定结构体的对齐方式.
> NOTICE: 若 `alignas` 小于自然对齐的最小单位, 则被忽略

```c++
struct Info {
    uint8_t a;
    uint32_t b;
    uint8_t c;
}; // sizeof(Info) == 12, alignof(Info) == 4

struct alignas(8) Info8 {
    uint8_t a;
    uint32_t b;
    uint8_t c;
}; // sizeof(Info) == 16, alignof(Info) == 8, alignas 生效
// +---+---+---+---+---+---+---+---+---+---+---+---+---+---+---+---+
// | a | pad       | b             | c | pad                       |
// +---+---+---+---+---+---+---+---+---+---+---+---+---+---+---+---+
// 0   1   2   3   4   5   6   7   8   9   10  11  12  13  14  15  16

struct alignas(2) Info2 {
    uint8_t a;
    uint32_t b;
    uint8_t c;
}; // sizeof(Info) == 12, alignof(Info) == 4, alignas 失效
```

如果想使用单字节对齐的方式, 使用 `alignas` 是无效的. 应该使用 `#pragma pack(push,1)` 或者使用 `__attribute__((packed))`.

```c++
#if defined(__GNUC__) || defined(__GNUG__)
    #define ONEBYTE_ALIGN __attribute__((packed))
#elif defined(_MSC_VER)
    #define ONEBYTE_ALIGN
    #pragma pack(push,1)
#endif

struct Info {
    uint8_t a;
    uint32_t b;
    uint8_t c;
} ONEBYTE_ALIGN; // sizeof(Info) == 6, alignof(Info) == 1

#if defined(__GNUC__) || defined(__GNUG__)
    #undef ONEBYTE_ALIGN
#elif defined(_MSC_VER)
    #pragma pack(pop)
    #undef ONEBYTE_ALIGN
#endif
```

## 堆和栈的区别

- 管理方式不同:
  - 栈资源由编译器/系统自动管理, 无需手工控制
  - 堆中资源由程序员控制 (容易产生 memory leak)
- 内存管理机制不同: (这一块理解一下链表和队列的区别, 不连续空间和连续空间的区别)
  - 只要栈的剩余空间大于所申请空间, 系统为程序提供内存, 否则报异常提示栈溢出.
  - 系统有一个记录空闲内存地址的链表, 当系统收到程序申请时, 遍历该链表, 寻找第一个空间大于申请空间的堆结点, 删除空闲结点链表中的该结点, 并将该结点空间分配给程序. (大多数系统会在这块内存空间首地址记录本次分配的大小，这样 `delete` 才能正确释放本内存空间, 另外系统会将多余的部分重新放入空闲链表中)
- 申请大小限制不同:
  - 栈顶和栈底是之前预设好的, 栈是向栈底扩展, 大小固定. (Linux 下栈大小默认是 4M, 通过 `ulimit -a` 查看, 由 `ulimit -s` 修改; Windows 下栈大小是 2 or 1 M, 在编译时确定, VC 中可设置)
  - 堆是不连续的内存区域 (因为系统是用链表来存储空闲内存地址, 自然不是连续的), 堆大小受限于计算机系统中有效的虚拟内存 (32-bit 系统理论上是 4G), 所以堆的空间比较灵活, 比较大
- 申请效率不同:
  - 栈是其系统提供的数据结构, 计算机在底层对栈提供支持, 分配专门寄存器存放栈地址, 栈操作有专门指令; 在分配上类似于数据结构上的一个先进后出的栈, 进出一一对应, 不会产生碎片
  - 堆由 C/C++ 函数库提供, 机制很复杂; 频繁的 `new`/`delete` 会造成大量碎片, 使程序效率降低; 所以堆的效率比栈低很多;
- 分配方式不同:
  - 栈有静态分配和动态分配, 静态分配由编译器完成 (如局部变量分配), 动态分配由 `alloca` 函数分配, 但栈的动态分配的资源由编译器进行释放, 无需程序员实现; 栈向下, 向低地址方向增长
  - 堆都是动态分配 (没有静态分配的堆); 堆向上, 向高地址方向增长

## `new` / `delete` 与 `malloc` / `free`

- 相同点:
  - 都可用于内存的动态申请和释放
- 不同点:
  - 前者是 C++ 运算符，后者是 C/C++ 语言标准库函数
  - `new` 自动计算要分配的空间大小, `malloc` 需要手工计算
  - `new` / `delete` 是类型安全的; `malloc` / `free` 不是, 返回的是 `void` 类型指针 (必须进行类型转换)
  - `new` 调用名为 `operator new` 的标准库函数分配足够空间并**调用相关对象的构造函数**; `delete` 对指针所指对象运行适当的析构函数, 然后通过调用名为 `operator delete` 的标准库函数释放该对象所用内存.
  - `malloc` 仅仅分配内存空间, `free` 仅仅回收空间, 不具备调用构造函数和析构函数功能, 用 `malloc` 分配空间存储类的对象存在风险
  - 后者需要库文件支持, 前者不用
  - `new/delete` 的底层实现**不要求**基于 `malloc/free`（实现可能使用别的分配器/系统调用）。混用（`new` 配 `free`、`malloc` 配 `delete`）属于未定义行为；即使“看起来没报错”也不正确
  - `malloc` 和 `free` 是标准库函数, 支持覆盖; `new` 和 `delete` 是运算符, 支持重载

### `new`

1. Plain `new`: 常用的 `new`, 在C++中定义如下:

```c++
void* operator new(std::size_t) throw(std::bad_alloc);
void operator delete(void *) throw();
```

因此, plain `new` 在空间分配失败的情况下, 抛出异常 `std::bad_alloc` 而不是返回 `NULL`, 因此通过判断返回值是否为 `NULL` 是徒劳的, 举个例子:

```c++
#include <iostream>
#include <string>
using namespace std;
int main() {
    try {
        char *p = new char[10e11];
        delete p;
    } catch (const std::bad_alloc &ex) {
        cout << ex.what() << endl;
    }
    return 0;
}
// 执行结果：bad allocation
```

1. nothrow `new`: 在空间分配失败的情况下是不抛出异常, 而是返回 `NULL`, 定义如下：

```c++
void * operator new(std::size_t,const std::nothrow_t&) throw();
void operator delete(void*) throw();
```

举个例子:

```c++
#include <iostream>
#include <string>
using namespace std;

int main() {
    char *p = new(nothrow) char[10e11];
    if (p == NULL) {
        cout << "alloc failed" << endl;
    }
    delete p;
    return 0;
}
// 运行结果：alloc failed
```

1. placement `new`: 允许在一块已经分配成功的内存上重新构造对象或对象数组, placement `new` 不用担心内存分配失败, 因为它根本不分配内存, 它做的唯一一件事情就是调用对象的构造函数. 定义如下:

```c++
void* operator new(size_t,void*);
void operator delete(void*,void*);
```

使用时需要注意两点:

- 其主要用途就是反复使用一块较大的动态分配的内存来构造不同类型的对象或者他们的数组
- 其构造起来的对象数组, 要显式的调用他们的析构函数来销毁 (析构函数并不释放对象的内存), 千万不要使用`delete`, 这是因为 placement `new` 构造起来的对象或数组大小并不一定等于原来分配的内存大小, 使用`delete` 会造成内存泄漏或者 double free.

举个例子:

```c++
#include <iostream>
#include <string>
using namespace std;
class ADT {
    int i;
    int j;
public:
    ADT() {
        i = 10;
        j = 100;
        cout << "ADT construct i=" << i << ", j="<< j <<endl;
    }
    ~ADT() {
        cout << "ADT destruct" << endl;
    }
};
int main() {
    char *p = new(nothrow) char[sizeof ADT + 1];
    if (p == NULL) {
        cout << "alloc failed" << endl;
    }
    ADT *q = new(p) ADT;  // placement new: 不必担心失败, 只要 p 所指对象的的空间足够 ADT 创建即可
    // delete q; // 错误! 不能在此处调用 delete q;
    q->ADT::~ADT(); // 显示调用析构函数
    delete[] p;
    return 0;
}
// 输出结果：
// ADT construct i=10, j=100
// ADT destruct
```

### `delete` vs `delete[]`

1. 动态数组管理:
   - `new` 一个数组时，`[]` 中必须是一个整数, 但是不一定是常量整数, 普通数组必须是一个常量整数;
   - 返回的并不是数组类型, 而是一个元素类型的指针;
   - `delete[]` 时, 数组中的元素按*逆序*的顺序进行销毁;
   - `delete` 将对象析构和内存释放组合在一起的; `new` 的机制是将内存分配和对象构造组合在一起

2. `allocator` 将内存管理和对象管理两部分分开进行. `allocator` 申请一部分内存, 不进行初始化对象, 只有当需要的时候才进行初始化操作.
3. `delete[]` 需要知道数组元素个数以便逐个析构；“元素个数如何保存/如何找到”属于实现细节，不应假设固定为“多 4 字节”。

### `malloc` 与 `free` 的实现原理

- 在标准 C 库中, 提供了 `malloc`/`free` 函数分配释放内存, 这两个函数底层是由 `brk`, `mmap`, `munmap` 这些系统调用实现的:
  - `brk` 是将「堆顶」指针向高地址移动, 获得新的内存空间;
  - `mmap` 是在进程的虚拟地址空间中 (堆和栈中间, 称为文件映射区域的地方) 找一块空闲的虚拟内存.
  - 以上这两种方式分配的都是虚拟内存, 没有分配物理内存. 在第一次访问已分配的虚拟地址空间的时候, 发生缺页中断, 操作系统负责分配物理内存, 然后建立虚拟内存和物理内存之间的映射关系;

- 分配内存时:
  - `malloc` 小于 128K 的内存, 使用 `brk` 分配内存, 将「堆顶」指针往高地址推;
  - `malloc` 大于 128K 的内存, 使用 `mmap` 分配内存, 在堆和栈之间找一块空闲内存分配;
  - `brk` 分配的内存需要等到高地址内存释放以后才能释放, 而 `mmap` 分配的内存可以单独释放. 当最高地址空间的空闲内存超过 128K (可由`M_TRIM_THRESHOLD` 选项调节) 时, 执行内存紧缩操作 (trim). 在上一个步骤 `free` 的时候, 发现最高地址空闲内存超过 128K, 于是内存紧缩.

- `malloc` 是从堆里面申请内存, 也就是说函数返回的指针是指向堆里面的一块内存.
- 操作系统中有一个记录空闲内存地址的链表. 当操作系统收到程序的申请时, 就会遍历该链表, 然后就寻找第一个空间大于所申请空间的堆结点, 然后就将该结点从空闲结点链表中删除, 并将该结点的空间分配给程序.
- `free` 之后的流程:
  - 首先被 `ptmalloc` 使用双链表保存起来, 当用户下一次申请内存的时候, 会尝试从这些内存中寻找合适的返回, 这样就避免了频繁的系统调用, 占用过多的系统资源
  - 同时 `ptmalloc` 也会尝试**对小块内存进行合并**, 避免过多的内存碎片

## 类型安全

> `类型安全 ~= 内存安全`: 类型安全的代码不会试图访问自己没被授权的内存区域.

- 评价编程语言: 其根据在于该门编程语言是否提供保障类型安全的机制
- 评价某个程序: 判别的标准在于该程序是否隐含类型错误

## 内存泄漏

> 一般我们常说的内存泄漏是指*堆内存*的泄漏. 堆内存是指程序从堆中分配的, 大小任意的 (内存块的大小可以在程序运行期决定) 内存块, 使用完后必须显式释放的内存. 应用程序般使用 `malloc`, `realloc`, `new` 等函数从堆中分配到块内存, 使用完后, 程序必须负责相应的调用 `free` 或 `delete` 释放该内存块; 否则, 这块内存就不能被再次使用, 我们就说这块内存泄漏了

### 检查工具

- Linux 下可以使用 Valgrind, ASan 工具
- Windows 下可以使用 CRT 库
- 在不终止程序的情况下, 实时检测 C++ 内存泄漏并输出报告, 可以使用 LeakSanitizer (LSan, ASan 的子工具)
  - LSan 是 ASan 的一部分, 专门用于检测内存泄漏, 支持在程序运行时输出泄漏报告而不终止进程
  - 步骤 1: 编译时启用 LSan

    ```bash
    # 使用 GCC 或 Clang 编译
    g++ -fsanitize=leak -g your_program.cpp -o your_program # -fsanitize=leak 仅启用 LSan (比 ASan 轻量), 但也可以直接用 -fsanitize=address (ASan + LSan)
    ```

  - 步骤 2: 运行程序并实时输出泄漏

    ```bash
    # 设置环境变量，让 LSan 在检测到泄漏时打印报告但不终止程序
    export LSAN_OPTIONS="report_objects=1:suppressions=leak_suppressions.txt:print_suppressions=0:halt_on_error=0"
    ./your_program
    ```

    **关键选项**:
    - `report_objects=1`: 显示泄漏的内存对象详情
    - `halt_on_error=0`: 检测到泄漏后不终止程序 (默认是 `1`, 会终止)
    - `suppressions=leak_suppressions.txt`: 忽略某些已知泄漏 (可选)

  - 步骤 3: 手动触发泄漏报告: 如果希望在程序运行期间主动输出泄漏报告 (如收到信号时), 可以在代码中插入:

    ```c++
    #include <sanitizer/lsan_interface.h>

    // 在需要检测的地方调用
    __lsan_do_leak_check();  // 主动触发泄漏检测并打印报告
    ```

## 对象复用 && 零拷贝

- 对象复用
  - 本质是一种设计模式: 享元 (Flyweight) 模式
  - 将对象存储到 "对象池" 中实现对象的重复利用, 这样可以避免多次创建重复对象的开销, 节约系统资源
- 零拷贝
  - 一种避免 CPU 将数据从一块存储拷贝到另外一块存储的技术
  - 可以减少数据拷贝和共享总线操作的次数

在 C++ 中, `vector` 的成员函数 `emplace_back` 很好地体现了**原地构造**（避免一次不必要的临时对象/拷贝/移动）：

- 它与 `push_back` 函数一样可以将一个元素插入容器尾部
- 区别在于: `emplace_back` 直接在容器尾部构造元素；而 `push_back(Person(...))` 往往会先构造一个临时对象，再移动/拷贝进容器（具体是否发生移动/拷贝与优化、重载选择、是否触发扩容有关）

```c++
#include <vector>
#include <string>
#include <iostream>
using namespace std;

struct Person {
    string name;
    int age;
    // 初始构造函数
    Person(string p_name, int p_age)
        : name(std::move(p_name)), age(p_age) {
        cout << "I have been constructed" << endl;
    }

    // 拷贝构造函数
    Person(const Person& other)
        : name(other.name), age(other.age) {
        cout << "I have been copy constructed" << endl;
    }

    // 转移构造函数
    Person(Person&& other)
        : name(std::move(other.name)), age(other.age) {
        cout << "I have been moved" << endl;
    }
};

int main() {
    vector<Person> e;
    cout << "emplace_back:" <<endl;
    e.emplace_back("Jane", 23); // 不用构造类对象

    vector<Person> p;
    cout << "push_back:"<<endl;
    p.push_back(Person("Mike",36));
    return 0;
}

// 输出结果:
// emplace_back:
// I have been constructed
// push_back:
// I have been constructed
// I have been moved
```

## `this`

### 底层实现

1. 成员函数调用时的隐式参数
   - 对于成员函数:

   ```c++
   obj.func(arg);
   ```

   - 编译器会将其转换为:

   ```c++
   func(&obj, arg);  // 隐式传入 this 指针
   ```

2. 成员变量访问的地址计算
   - 成员变量在内存中是 **连续存储** 的, 偏移量在编译时确定

   - `this->x` 实际访问的是 `*(this + offset_of_x)`, 其中 `offset_of_x` 是 `x` 的偏移量

3. 内存布局示例:

   ```c++
   class MyClass {
       int a;    // 偏移量 0
       char b;   // 偏移量 4 (假设 int 占 4 字节)
       double c; // 偏移量 8 (考虑对齐)
   };
   ```

   - 访问 `c` 时:

   ```c++
   this->c;  // 等价于 *(double*)((char*)this + 8)
   ```

## 内存池实现

内存池是一种内存分配方式. 通常我们习惯直接使用 `new`, `malloc` 等申请内存, 这样做的缺点在于: **由于所申请内存块的大小不定, 当频繁使用时会造成大量的内存碎片并进而降低性能**. 内存池则是在真正使用内存之前, 先申请分配一定数量的, 大小相等 (一般情况下) 的内存块留作备用. 当有新的内存需求时, 就从内存池中分出一部分内存块, 若内存块不够再继续申请新的内存. 这样做的一个显著优点是尽量避免了内存碎片, 使得内存分配效率得到提升.

"STL源码剖析" 中的内存池实现机制:

> `allocate` 包装 `malloc`, `deallocate` 包装 `free`

一般是一次 20\*2 个的申请, **先用一半, 留着一半**, 为什么也没个说法 (好像是 C++ 委员会成员认为 20 是个比较好的数字, 既不大也不小)

1. 首先客户端会调用 `malloc` 配置一定数量的区块 (固定大小的内存块, 通常为8的倍数), 假设 40 个 32 bytes 的区块, 其中 20 个区块 (一半) 给程序实际使用, 1 个区块交出, 另外 19 个处于维护状态; 剩余20个留给内存池, 此时一共有 (20 \* 32 bytes)
2. 客户端之后有内存需求, 想申请 (20 _64 bytes) 的空间, 这时内存池只有 (20_ 32 bytes), 就先将 (10 \* 64 bytes) 个区块返回, 1个区块交出, 另外 9 个处于维护状态, 此时内存池空空如也.
3. 接下来如果客户端还有内存需求, 就必须再调用 `malloc` 配置空间, 此时新申请的区块数量会增加一个随着配置次数越来越大的附加量, 同样一半提供程序使用, 另一半留给内存池. 申请内存的时候用永远是先看内存池有无剩余, 有的话就用上, 然后挂在 0-15 号某一条链表上, 要不然就重新申请
4. 如果整个堆的空间都不够了, 就会在原先已经分配区块中寻找能满足当前需求的区块数量, 能满足就返回, 不能满足就向客户端报 `bad_alloc` 异常

`allocator` 就是用来分配内存的, 最重要的两个函数是 `allocate` 和 `deallocate`, 就是用来申请内存和回收内存的, 外部 (一般指容器) 调用的时候只需要知道这些就够了

**内部实现**: 目前的所有编译器都是直接调用的 `::operator new()` 和 `::operator delete()`, 说白了就是和直接使用 `new` 运算符的效果是一样的, 所以老师说它们都没做任何特殊处理

**其实最开始 GC2.9 之前**

- `new` 和 `operator new` 的区别: `new` 是个运算符, 编辑器会调用 `operator new(0)`

- `operator new()` 里面有调用 `malloc` 的操作, 那同样的 `operator delete()` 里面有调用的 `free` 的操作

**GC2.9** 下的 `alloc` 函数的一个比较好的分配器的实现规则如下:

- 维护一条 0-15 号的一共 16 条链表, 其中 0 号表示 8 bytes , 1 号表示 16 bytes, 2 号表示 24 bytes... 而 15 号表示 16 \* 8 = 128 bytes

- 如果在申请内存时, 申请内存的大小并不是 8 的倍数 (比如 2, 4, 7, 9, 18 这样不是 8 的倍数), 那就找刚好能满足内存大小的链表. 比如想申请 12 个大小, 那就按照 16 来处理, 也就是找 1 号链表了, 想申请 20, 距离它最近的就是 24 了, 那就找 2 号链表

- 只许比所要申请的内容大, 不许小

**但是现在GC4.9及其之后**也还有 `alloc` 函数, 只不过已经变成 `_pool_alloc` 这个名字了, 名字已经改了, 也不再是默认的了

你需要自己手动去指定它可以自己指定, 比如

```c++
vector<string, __gnu_cxx::pool_alloc<string>> vec;
```

这样来使用它, 等于兜兜转转又回到以前那种对 `malloc` 和 `free` 的包装形式了

## 常见坑（工程视角）

- **野指针（uninitialized pointer）**：指针未初始化，值不确定；读/写/解引用都可能直接 UB。
- **空指针（null pointer）**：值为 `nullptr`（或 C 的 `NULL/0`）；对其解引用是 UB。
- **悬空引用/悬空指针**：引用了已经析构的对象（返回局部引用、容器扩容导致指针/引用失效等）。
- **双重释放 / 释放不匹配**：`new` 配 `free`、`malloc` 配 `delete`、`new[]` 配 `delete` 等都是 UB。
- **对象部分构造/异常安全**：构造过程中抛异常时，只有已构造完成的子对象会析构；资源获取尽量放到 RAII 成员里。
- **对齐/packed**：`#pragma pack(1)` 可能导致未对齐访问（部分架构会崩/性能大幅下降）；协议/网络结构体要谨慎。

## 工程抓手（定位内存问题）

- 优先用 Sanitizer：
  - ASan：`clang++ -fsanitize=address -g -O1 ...`
  - UBSan：`clang++ -fsanitize=undefined -g -O1 ...`
  - LSan：见本文已有示例（也可直接用 `-fsanitize=address` 联合启用）
- 如果能复现：优先把问题缩小到一个最小可复现用例，再结合崩溃栈/ASan 报告定位到“释放点/使用点”。

