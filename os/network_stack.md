# Linux 网络栈 (Network Stack)

这一篇聚焦 Linux 内核网络数据通路：包怎么从 NIC 进到用户态 socket，怎么在协议栈各层流转，又是怎么“减少拷贝/减少系统调用”地把吞吐拉满。  
I/O 多路复用的 epoll/io_uring 部分与 [`io.md`](io.md) 互补阅读；CPU 调度/中断亲和性放在 [`process.md`](process.md)。

## TL;DR

- 数据通路主线：`NIC → 驱动 → NAPI → ip_rcv → TCP/UDP → socket → 用户态`。
- **零拷贝**的演进：`read+write` → `mmap` → `sendfile` → `splice` → `MSG_ZEROCOPY`，每一代都在减少一次**用户态 ↔ 内核态**的拷贝。
- **协议栈 offload** (TSO/GSO/LRO…) 把分段/校验和/重组下放到网卡/驱动；正确打开是性能，错配是“反咬一口”的延迟与丢包。
- **多路复用**从 `select/poll` 走到 `epoll`，再走到 `io_uring`：核心问题是“怎么用尽量少的 syscall 让一个线程等很多 FD”。
- **eBPF/XDP** 给了在“网卡刚收到包”和“TC 分类器”位置运行自定义程序的钩子，是现代可观测/安全/负载均衡的关键。

## 1. 收包主线

### 1.1 NIC → 驱动：硬中断 + DMA

```text
  +-------+      DMA       +-------+      硬中断     +-------+
  |  NIC  | -------------> |  Ring | -------------> |  CPU  |
  +-------+    写入描述符   +-------+   触发 IRQ     +-------+
                                                          |
                                                          v
                                                  +----------------+
                                                  |  驱动中断处理  |
                                                  |  NAPI 调度     |
                                                  +----------------+
```

- 网卡把包通过 DMA 写到预分配的 **ring buffer** 描述符指向的内存。
- 写完后向 CPU 触发 **硬中断**。
- 驱动在中断处理里只做最少的工作（确认/记账），然后调度 **NAPI**。

### 1.2 NAPI：中断 + 轮询混合

> NAPI (New API) 的设计动机是**中断太多会爆 CPU**：当大量包到达时，反复触发硬中断本身就成了瓶颈。

- 第一个包 → 硬中断唤醒 softirq。
- 后续包 → 关闭硬中断，**轮询 ring buffer** 取包，直到空或配额用尽，再开中断。
- **优点**：高吞吐时减少中断开销，CPU 用来跑业务而不是被中断“打断-打断-打断”。
- **反咬**：轮询会一直占用 CPU，低流量时没有包时仍会消耗调度时间（需配合 `gro_flush_timeout`/`busy_poll` 等调参）。

### 1.3 协议栈入口：ip_rcv 与软中断上下文

- `ip_rcv`：在 softirq 上下文里走 IP 层（TTL、校验和、分片、netfilter 钩子）。
- 关键约束：**softirq 不能睡眠**，因此协议栈里所有“阻塞”调用都得在用户态 recv 路径（process context）里发生——这也是为什么很多长路径会先入队，再唤醒用户线程。

### 1.4 传输层：TCP/UDP

- **TCP**：状态机、拥塞控制、滑动窗口、重传定时器、TSQ/TSO 协同。状态由 `struct sock` 维护。
- **UDP**：基本无状态，内核只做端口分发、校验和、可选 GSO。
- 两者最后都把数据“挂”到 socket 接收队列上，等待用户态 `read/recvmsg`。

### 1.5 到达用户态

- 用户线程阻塞在 `epoll_wait` 上，网卡收包 → 协议栈入队 → `epoll_wait` 返回 → 用户态 `read`。
- 关键不变量：**协议栈里不允许睡眠**；任何需要睡眠的逻辑（`skb` 分配失败等）都会把包丢回队列，等下次软中断再处理。

## 2. 零拷贝路径

### 2.1 传统 read + write 的拷贝次数

```text
Disk ─DMA─> Kernel Page Cache ─copy─> User Buffer
        ─copy─> Kernel Socket Buffer ─DMA─> NIC
        (4 次拷贝: 2 次 DMA, 2 次 CPU 拷贝)
```

### 2.2 各种零拷贝方案对比

| 方案           | 拷贝次数 (内核视角)     | 适用场景                       | 限制                                     |
| :------------- | :---------------------- | :----------------------------- | :--------------------------------------- |
| `read + write` | 4 (2 DMA + 2 CPU)       | 通用                           | 拷贝多、上下文切换多                     |
| `mmap + write` | 3 (2 DMA + 1 CPU)       | 读多、有修改需求               | 缺页/地址映射开销，写仍要 copy 到 socket |
| `sendfile`     | 2 DMA (+ 0~1 CPU)       | 文件 → socket 的纯转发         | 只能 out_fd 是 socket，源不能改          |
| `splice`       | 2 DMA (+ 0 CPU)         | 任意 fd ↔ pipe / pipe ↔ socket | 至少一端必须是 pipe                      |
| `vmsplice`     | 0 拷贝到 pipe，但有 pin | 用户内存 → pipe                | 内存被 pin 住，量大时易爆                |
| `MSG_ZEROCOPY` | TCP 发送零拷贝          | 发送大块到 TCP socket          | 仅 TCP；需注册表项、首次延迟较高         |

#### sendfile 直觉

```c
ssize_t sendfile(int out_fd, int in_fd, off_t *offset, size_t count);
// in:  文件 fd  (只读)
// out: socket fd (可写)
// 行为：内核把 in_fd 的 page cache 引用/数据直接搬到 socket 的发送队列。
```

> 如果网卡支持 **SG-DMA (Scatter-Gather DMA)**，`sendfile` 可以做到 **0 次 CPU 拷贝**：内核只构造一个描述符链表，网卡直接从 page cache 抓数据。`ethtool -k <nic> | grep scatter-gather` 可以查。

#### MSG_ZEROCOPY 直觉

```c
sendmsg(fd, &msg, MSG_ZEROCOPY);
// 行为：内核把用户态 buffer 引用挂到发送队列，网卡 DMA 直接读。
// 完成时通过 error queue 通知 (recvmsg + MSG_ERRQUEUE)。
```

> 限制：仅 TCP；首次发送有注册开销；适合大块、稳定的发送模式（音视频流、RPC 大请求体）。

#### splice 直觉

```c
ssize_t splice(int fd_in, loff_t *off_in,
               int fd_out, loff_t *off_out,
               size_t len, unsigned int flags);
```

> splice 是“管道视角”的零拷贝：在 pipe 的两端 splice，绕过用户态。常见用法：`file → pipe → socket`（受限于其中一端必须是 pipe）。

### 2.3 零拷贝不是“越用越快”

- **首次建立**有注册/映射成本（`MSG_ZEROCOPY` 注册 user page；`sendfile` 预读）。
- **小包**下零拷贝不一定比 copy 快（描述符构造 + 跨 cache 命中）。
- **NUMA**：跨节点零拷贝的内存亲和性可能更差。
- **生命周期管理**：零拷贝通常意味着“把物理页暴露给设备”，出错时延后回收更难追责。

## 3. 协议栈 offload

> **offload** 是把本来由 CPU 干的事情下放到网卡/驱动。设计目标：CPU 越来越快但网口带宽增速更快，必须让 CPU 不再成为瓶颈。

### 3.1 常见 offload 维度

| Offload                      | 干啥的                        | 谁做         | 备注                           |
| :--------------------------- | :---------------------------- | :----------- | :----------------------------- |
| **TSO**                      | TCP Segmentation Offload      | 网卡         | 内核只发大段，网卡切 MSS       |
| **GSO**                      | Generic Segmentation Offload  | 内核 softirq | 软卸载，TSO 不可用时退而求其次 |
| **LRO/GRO**                  | Large/Generic Receive Offload | 内核/网卡    | 合并多个小包为大包再上交       |
| **RSS**                      | Receive Side Scaling          | 网卡         | 多队列哈希分发到不同 CPU       |
| **XPS**                      | Transmit Packet Steering      | 内核         | 发送端绑核/绑队列              |
| **CSO/CO**                   | Checksum Offload              | 网卡         | TCP/UDP 校验和硬件算           |
| **L2/L3 forwarding offload** | 转发路径硬件化                | 网卡 (高端)  | 路由器/网关设备                |

> 还有 **HW NAT**、**IPsec offload**、**RDMA** 等；列在这里只是为了提醒“offload 是个面，不是单点技术”。

### 3.2 为什么 offload 会“反咬”

> 关掉某些 offload 反而更快/更稳——这种反直觉的现象是诊断网络问题时的常见入口。

- **GRO/TSO 与 iptables/NAT 冲突**：合并/分段在 netfilter 之后/之前的位置不同，会让规则的“包计数”对不上，或让 conntrack 误判。
- **TSO 与 tc/qdisc 互动**：qdisc 的限速、整形在分段前看到的可能是“超大包”，导致 burst 行为。
- **LRO 在虚拟化/路由场景丢包**：LRO 合并后做了 GRO 不会做的“修改包内容”，重传时 LRO 无法精确拆分，引入丢包/乱序。
- **校验和 offload 在抓包场景“骗你”**：tcpdump 看到的可能是伪校验和/0，因为硬件还没算；不要用抓包判断“发送端有没有算校验和”。
- **跨 NUMA 节点的中断亲和**：RSS 配错时，CPU 0 的软中断会把数据搬到远端内存，再交给 CPU 5 的用户线程。

> 经验法则：**遇到延迟抖动、p99 飙升、netfilter 计数异常，先把 TSO/GRO 状态和中断亲和看一遍**。`ethtool -k` / `ethtool -c` / `cat /proc/irq/<N>/smp_affinity`。

## 4. 多路复用：C10K → C100K → io_uring

### 4.1 问题的演进

- **C10K**（1999-2000 年代）：单机 1 万并发连接。
  - 解决：`epoll` (Linux) / `kqueue` (BSD) / IOCP (Windows) 把 O(N) 轮询降到 O(1) 事件通知。
- **C100K → C1000K**：单机 10 万~100 万连接。
  - 解决：内核协议栈优化（`SO_REUSEPORT`、epoll 的 `EPOLLEXCLUSIVE`、`skb` 共享）+ 用户态协议栈 (DPDK/Seastar 等) + 协程。
- **现代**：单机千万级连接；游戏/IoT/长连接推送。

### 4.2 演进路径上的关键 API

| 阶段         | 典型 API   | 痛点                                     |
| :----------- | :--------- | :--------------------------------------- |
| 早期         | `select`   | FD 集合每次拷贝；1024 上限；O(N) 扫描    |
| 中期         | `poll`     | 无 FD 上限；仍是 O(N) 扫描               |
| 现代         | `epoll`    | O(1) 事件回调；LT/ET 两种模式            |
| 异步 I/O     | `io_uring` | submit/complete 队列；零系统调用 syscall |
| 用户态协议栈 | DPDK/XDP   | 绕过内核协议栈（也绕过部分隔离）         |

### 4.3 io_uring 在“网络”位置

> io_uring 是**通用**的异步 I/O 框架：网络、文件、poll 都在它的视野里，是 epoll 之后的下一代。

- 提交队列 (SQR) 与完成队列 (CQR) 是用户态 ↔ 内核态的共享 ring。
- 对**网络**：支持 `IORING_OP_ACCEPT`、`IORING_OP_CONNECT`、`IORING_OP_SEND`、`IORING_OP_RECV`、`IORING_OP_SENDMSG` 等。
- 优势：可以**一次提交多个操作**而不阻塞；支持 `IOSQE_IO_LINK` 链接多个 op；可注册 user buffer 减少 pin/unpin。
- 限制：部分操作仍有同步语义（如 `accept`）；`io_uring` 自身有“用户态可绕过 seccomp”的攻击面争论（早期），现代内核已加入权限检查。

```c
struct io_uring_sqe *sqe = io_uring_get_sqe(&ring);
io_uring_prep_recv(sqe, fd, buf, len, 0);
sqe->user_data = 1;          // 提交时打个 tag
io_uring_submit(&ring);

// 之后在事件循环里 harvest：
struct io_uring_cqe *cqe;
io_uring_wait_cqe(&ring, &cqe);
if (cqe->res < 0) /* 错误 */;
io_uring_cqe_seen(&ring, cqe);
```

## 5. eBPF / XDP

> eBPF (extended Berkeley Packet Filter) 让你在内核的“指定钩子点”跑一段受限的字节码，**不修改内核代码、不加载内核模块**。XDP (eXpress Data Path) 是 eBPF 在“网卡刚收到包”这个最早期位置的实现。

### 5.1 eBPF 能做什么

- **可观测性**：`bpftrace` / `bcc` 工具集，抓 syscall、调度事件、网络事件。
- **网络**：XDP 在 NIC 驱动层（甚至驱动前）丢包/改包/重定向。
- **安全**：seccomp + LSM (BPF LSM) 做策略拦截。
- **跟踪**：kprobe/tracepoint/uprobe，跟函数级热点。

### 5.2 网络栈里的关键钩子点

```text
  NIC ──► [XDP] ──► 驱动 ──► [TC ingress clsact]
                              ──► [netfilter PREROUTING] ──► ip_rcv
                              ──► ... 协议栈 ...
                              ──► [netfilter POSTROUTING] ──► qdisc
                              ──► [TC egress clsact] ──► driver ──► NIC
```

- **XDP**：
  - 位置最早（在驱动 `NAPI` 之前）。
  - 可以 `XDP_PASS / XDP_DROP / XDP_TX / XDP_REDIRECT`。
  - **不能**访问完整的 socket 上下文；只能看到包头。
  - 高性能：DDoS 抗攻击、负载均衡入口、协议无关处理。
- **TC (Traffic Control)**：
  - ingress/egress 两端都能挂。
  - 比 XDP 慢一些，但有完整协议栈上下文。
- **socket 层**：`bpf` 也能挂到 socket 上做 filter（`SO_ATTACH_BPF`），传统上是 `tcpdump` 的底层。

### 5.3 XDP 的限制

- **看不到 socket 状态**（不知道目的连接）。
- **不解析 conntrack**（无 conntrack 时改包要自己重算校验和）。
- **受限于 BPF 验证器**（指令数、循环、栈深度），复杂逻辑装不下。
- **驱动兼容**：并非所有 NIC 都支持 native XDP，常见 intel/MLX5 OK，部分驱动只能走 generic XDP（性能差很多）。

> XDP 的“最佳位置”是**抗 DDoS 入口**与**高性能负载均衡**：能在最早处把垃圾流量干掉，把流量直接 redirect 到用户态 socket (AF_XDP)。

## 常见误区

- “**sendfile 一定比 read+write 快**”：在小文件/冷缓存下不一定；首次需要预热 page cache。
- “**TSO 打开就万事大义**”：在 iptables/nftables 重负载或 QoS 严格场景下要重新评估。
- “**epoll 一定能扛 10 万连接**”：单机 10 万连接只是开始，**内存**（socket buffer、fd 表）和**协议栈调参**才是瓶颈。
- “**XDP 能替代 iptables**”：能力范围不同；XDP 看不到 conntrack，做不到 NAT 之类的有状态逻辑。
- “**零拷贝一定省 CPU**”：注册/校验/回收的开销在短连接场景可能反咬；按业务测。

## 相关阅读

- [`io.md`](io.md)：五种 I/O 模型、epoll LT/ET、零拷贝基础
- [`process.md`](process.md)：上下文切换与多核调度影响网络线程设计
- [`concurrency.md`](concurrency.md)：协程/线程模型与 epoll 的搭配
- [`virtualization.md`](virtualization.md)：virtio-net / AF_XDP 与虚拟化网络的交叉
