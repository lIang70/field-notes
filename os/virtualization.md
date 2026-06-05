# 虚拟化与容器 (Virtualization & Container)

这一篇聚焦两个层级的“隔离/虚拟化”机制：硬件级虚拟化（VM/KVM/QEMU）与操作系统级虚拟化（容器）。  
进程/线程基础看 [`process.md`](process.md)，文件/IO 抽象看 [`io.md`](io.md) 与 [`filesystem.md`](filesystem.md)。

## TL;DR

- **虚拟化 (VM)** 走的是“硬件层模拟”：CPU 指令集、MMU、设备都能被替换，因此**隔离强度高、启动慢、占用大**。
- **容器 (Container)** 走的是“内核对象视图隔离”：多个进程共享同一个内核，只在 namespace/cgroup 这类内核抽象上做切分，因此**轻、密度高、但隔离弱**。
- **VM 偏安全边界，容器偏部署密度**——二者在生产里通常组合使用（VM 上跑容器）。
- KVM + QEMU 是 Linux 上的事实标准：KVM 提供 CPU/MMU 硬件加速，QEMU 做设备模拟与 I/O 转发。

## 1. 硬件虚拟化基础

### 1.1 为什么需要 CPU/MMU 扩展

> 纯软件虚拟化（Trap-and-Emulate）要求所有敏感指令必然触发陷入（trap），但 x86 在设计时并未满足这个条件（存在“非特权敏感指令”），导致早期虚拟化不得不靠二进制翻译。

现代 x86 用 **VT-x (VMX)**、AMD 用 **AMD-V (SVM)** 扩展出两个模式：

- **VMX root / host mode**：VMM (Virtual Machine Monitor) 所在的特权级。
- **VMX non-root / guest mode**：Guest OS 认为自己跑在“裸机”上，可以执行大部分特权指令，遇到敏感操作时由硬件自动陷入 root。

> 引入硬件扩展后，Guest OS 的大部分代码能“原样”跑在 CPU 上，不再需要二进制翻译，性能与可维护性大幅提升。

### 1.2 Trap-and-Emulate 的直觉

1. Guest 跑用户态 → 正常执行。
2. Guest 跑特权指令 → 触发陷入，进入 VMM。
3. VMM 模拟这条指令对虚拟设备/虚拟 MMU 的效果。
4. 切回 Guest 继续执行。

> 这种“每条敏感指令都去 VMM 走一趟”的模型，决定了 I/O 与 MMU 频繁切换的路径是性能瓶颈。

### 1.3 影子页表 vs EPT/NPT

Guest 维护的是“虚拟物理地址 (GVA → GPA)”的页表，宿主要做的是把它翻译到“机器物理地址 (GPA → HPA)”。

- **影子页表 (Shadow Page Table)**：VMM 维护一份合并后的 GVA→HPA 表，每次 Guest 修改 CR3/页表都触发陷入，由 VMM 维护影子页表一致性。开销大、实现复杂。
- **EPT (Intel) / NPT (AMD)**：硬件支持二级页表（Guest 页表 + EPT），MMU 硬件直接走两步翻译。Guest 修改页表时不再全部陷入，只在 EPT miss 时才进入 VMM。

> EPT/NPT 是现代虚拟化性能的关键；没有它，MMU 路径会退化回“每次 page table 写入都 trap”的状态。

### 1.4 I/O 虚拟化与 virtio

- **全设备模拟 (Emulation)**：QEMU 模拟一个完整的网卡/磁盘，Guest 用真实驱动访问；最简单但每条 I/O 都要 trap，吞吐差。
- **半虚拟化 (Paravirt, virtio)**：Guest 用专用 virtio 驱动，通过共享的 ring buffer 与 VMM 通信，减少 trap 次数。  
  常见设备：`virtio-net`、`virtio-blk`、`virtio-scsi`、`virtio-fs` 等。
- **设备直通 (Passthrough, VFIO/IOMMU)**：把物理设备直接绑给某个 Guest，跳过 VMM 模拟。性能接近裸机，但需要 IOMMU 配合并牺牲迁移灵活性。

## 2. KVM + QEMU 的协作

> KVM 是内核模块（`/dev/kvm`），QEMU 是用户态进程。KVM 提供 CPU/MMU 硬件加速，QEMU 做设备模拟与 I/O 路径。

### 2.1 角色分工

- **KVM (Kernel-based VM)**
  - 暴露 `/dev/kvm`，负责 vCPU 调度、VMX 进出、二级页表 (EPT)、中断注入。
  - 本身不模拟任何设备。
- **QEMU**
  - 负责 BIOS/固件、设备模拟（网卡/磁盘/USB/显卡）、virtio 后端。
  - 通过 `ioctl` 与 KVM 交互：创建 vCPU、设置寄存器、提交内存映射。
- **libvirt / 云平台**
  - 上层管理工具，封装 QEMU 命令行/配置文件。

```text
+-----------------------+        +-----------------------+
|       Guest OS        |        |       Host Userspace   |
|  (看到 virtio 设备)   |        |   (QEMU + libvirt)    |
+----------+------------+        +-----------+-----------+
           |  hypercall / mmio                        |
           v                                         v
+----------+--------------------------------------------+
|                  Linux Kernel (KVM 子系统)            |
|   - vCPU 调度  - EPT  - 中断注入  - VFIO/IOMMU        |
+-------------------------------------------------------+
```

### 2.2 一次 I/O 的“加速路径”

以 virtio-net 发送为例：

1. Guest 把描述符放入 virtqueue（共享内存）。
2. Guest 写一个 MMIO 寄存器通知 QEMU。
3. KVM 捕获 MMIO 写入，陷入到 QEMU。
4. QEMU 从共享 ring 取出描述符，把数据送到宿主的 TAP/物理网卡。
5. 完成后通过 irqfd / eventfd 注入虚拟中断到 Guest。

> “零 trap” 的关键是 **virtio** 把控制面和数据面分开：控制面（doorbell）trap 一次，数据面走 DMA 共享内存。

## 3. 容器 vs 虚拟机

### 3.1 隔离维度的本质差异

| 维度     | VM                            | Container                            |
| :------- | :---------------------------- | :----------------------------------- |
| 隔离对象 | 硬件 + 内核 + 用户态全栈      | 共享同一内核，仅切分进程视图         |
| CPU/MMU  | 独立 vCPU + EPT 隔离          | 共享宿主机 MMU（cgroup 限额/亲和性） |
| 设备     | 模拟或直通                    | 直接用宿主驱动                       |
| 启动     | 秒~分钟（BIOS/内核/Init）     | 毫秒~秒（一个进程 + rootfs）         |
| 镜像大小 | GB 级（完整 OS 镜像）         | MB~百 MB（应用 + 必要的 rootfs）     |
| 隔离强度 | 强（内核 bug 不会跨 VM 传播） | 弱（共享内核，逃逸即拿到 root）      |
| 密度     | 数十~数百/主机                | 数千~数万/主机                       |

> 容器不是“更轻的 VM”，而是“更密的进程隔离” —— 它的安全模型与 VM 是不同档位的。

### 3.2 容器两大支柱：namespace 与 cgroup

#### Namespace：让进程“看到”不同的全局视图

> namespace 把“单实例 = 全局唯一”的内核对象（PID、网络、挂载点等）变成“每 namespace 一份”，从而给进程一个独立的“视图”。

| Namespace   | 常见 flag (unshare/clone) | 隔离的内容                       |
| :---------- | :------------------------ | :------------------------------- |
| **PID**     | `CLONE_NEWPID`            | 进程编号（容器内 PID 从 1 开始） |
| **Network** | `CLONE_NEWNET`            | 网络设备/IP/路由/端口            |
| **Mount**   | `CLONE_NEWNS`             | 文件系统挂载点                   |
| **UTS**     | `CLONE_NEWUTS`            | 主机名/域名                      |
| **IPC**     | `CLONE_NEWIPC`            | SysV IPC、POSIX mq               |
| **User**    | `CLONE_NEWUSER`           | UID/GID 映射（root in 容器）     |
| **Cgroup**  | `CLONE_NEWCGROUP`         | cgroup 根目录视图                |
| **Time**    | `CLONE_NEWTIME`           | 启动时间/单调时钟                |

> 这只是一份“典型清单”，不同内核版本会扩展。**User namespace** 经常被独立讨论：它让“容器内 root”实际上只是宿主机上一个普通 UID，是缓解特权容器风险的关键。

#### cgroup：让进程“用到”有限的资源

> cgroup (control group) 是内核对进程组进行**资源配额与统计**的机制，与 namespace 的“视图隔离”互补。

常见 cgroup v2 子系统：

- `cpu`, `cpuset`, `cpu.weight` — CPU 份额与绑核
- `memory`, `memory.max`, `memory.swap.max` — 内存/交换区上限
- `io`, `io.max` — 块设备带宽/IOPS 限额
- `pids`, `pids.max` — 进程数上限（防 fork 炸弹）

> cgroup 不是 namespace 的“强化版”。把配额调小可以让容器 OOM，但**不能让容器 OOM 时不波及同 cgroup 的兄弟进程**——隔离强度有差。

### 3.3 镜像与 rootfs：UnionFS 与分层

容器镜像本质是一个**层叠的 rootfs**：

- 每一层是一个 tar + 一些元数据（环境变量、入口、配置）。
- 运行时通过 **UnionFS (overlayfs/aufs)** 把多层叠成一个统一视图。
- 写时复制 (Copy-on-Write)：只有被改动的文件才占用新空间。

```text
+-----------------------+   <- Container layer (可写, 运行时)
+-----------------------+
|   app v2  (12 MB)     |   <- Image layer
+-----------------------+
|   base libs  (200 MB) |   <- Image layer
+-----------------------+
|   ubuntu base (80 MB) |   <- Image layer
+-----------------------+
```

> “分层” 决定了镜像的复用与缓存粒度：Docker build cache、OCI Distribution 拉取都是按层传输的。

## 4. 容器生态与 OCI 规范

### 4.1 Docker / containerd / runc 的分工

| 组件           | 角色                                         | 语言/位置           |
| :------------- | :------------------------------------------- | :------------------ |
| **Docker**     | 一站式 CLI + daemon，构建/推送/运行          | Go (Moby 项目)      |
| **containerd** | 工业级容器运行时，管理镜像/网络/容器生命周期 | Go，Kubernetes 默认 |
| **CRI-O**      | Kubernetes 专用轻量 CRI 实现                 | Go                  |
| **runc**       | 真正执行 `clone/exec` 等系统调用的运行时     | Go (OCI 参考实现)   |
| **crun**       | runc 的 C 重写版本，更小更快                 | C                   |
| **gVisor**     | 在用户态“半模拟”内核，强隔离的 sandbox       | Go                  |
| **Kata**       | 容器跑在轻量 VM 内（VM-as-Container）        | 多语言              |

### 4.2 OCI 规范三件套

> OCI (Open Container Initiative) 把容器生态拆成三个独立的规范，避免“一家独大、绑定死”。

- **OCI Runtime Spec**：规定 `config.json` 与 `runtime` 的行为契约（runc/crun 都是它的实现）。
- **OCI Image Spec**：规定镜像清单 (`index/manifest`) 与层 (`layer`)、配置 (`config`) 的格式。
- **OCI Distribution Spec**：规定镜像仓库的 pull/push HTTP API。

```bash
# 一个典型的 OCI runtime 调用
runc run mycontainer
# 等价于：
#   1) 读取 config.json（namespace、cgroup、rootfs、seccomp）
#   2) clone() 出子进程，配置 namespace
#   3) pivot_root 到 rootfs
#   4) execve() 启动 entrypoint
```

## 5. 容器安全的攻击面（点到为止）

> 容器共享内核，因此**任何内核漏洞都潜在影响所有容器**。下面列出几条最常见的“逃逸”路径与对应缓解——这是一份“知道要看哪里”的清单，不是 exploit 教程。

### 5.1 常见逃逸路径

- **特权容器 (`--privileged`)**：关闭大量 capability，挂载 `/dev`、关闭 AppArmor/seccomp。容器内能直接访问宿主机设备，等价于“容器内 root ≈ 宿主机 root”。
- **危险的 host 模式**：
  - `--net=host`：直接拿到宿主机网络栈，能嗅探/劫持宿主机流量。
  - `--pid=host`：能看到宿主机所有进程，攻击面直接拉到全机。
  - `--ipc=host` / `--uts=host`：破坏 IPC 与主机名隔离。
- **危险挂载**：把 `/`、`/proc`、`/sys`、`/dev`、`/var/run/docker.sock` 等敏感路径挂进容器，等于把控制权交出去。
- **内核漏洞**：脏管道 (Dirty Pipe)、Dirty COW、overlayfs 提权等都是共享内核下的高危类。
- **容器内应用漏洞 → 容器逃逸**：比如特权进程 (root) 拿到的 syscall 能力，配合 seccomp 未禁止的 `mount`/`pivot_root` 等。

### 5.2 基础缓解（顺序不分先后）

- **永远不**用 `--privileged`；按需添加 capability（`--cap-drop=ALL --cap-add=...`）。
- 强制 User Namespace（容器内 root ≠ 宿主机 root）。
- 启用 seccomp 与 AppArmor/SELinux profile。
- `no-new-privileges` 安全标志位（防 setuid 提权）。
- `read_only` rootfs + 显式声明的 writable 卷。
- 资源限额 (cgroup) 防止资源耗尽型 DoS。
- 镜像扫描、最小化基础镜像、签名校验 (cosign/Notary)。

> 安全是一个分层防御：namespace 解决“看不到”，cgroup 解决“用不完”，seccomp/AppArmor 解决“做不到”，capability drop 解决“默认不给”。**只做其中一项，都算不上安全。**

## 常见误区

- “**容器比 VM 安全**”：错。共享内核意味着逃逸面是**整个内核 ABI**，而 VM 至少还有硬件 MMU 隔离这一层。
- “**用了 Docker 镜像扫描就安全了**”：扫描发现的是已知 CVE，但**配置错误**（特权、挂载）通常占安全事件的更大比例。
- “**只要不 `--privileged` 就隔离**”：错。User namespace 没启用、敏感路径挂载、seccomp 缺失，每一项都能绕过“看起来很安全”的配置。

## 相关阅读

- [`process.md`](process.md)：进程基础、调度器与上下文切换
- [`io.md`](io.md)：I/O 多路复用与零拷贝
- [`memory.md`](memory.md)：虚拟内存与缺页异常
- [`filesystem.md`](filesystem.md)：VFS/挂载点
- [`concurrency.md`](concurrency.md)：cgroup/资源限制在并发场景的副作用
