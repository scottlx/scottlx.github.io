---
title: "使用libbpf-bootstrap写内核探测工具"
date: 2025-09-15T12:00:00+08:00
draft: false
tags: ["ebpf","linux"]
tags_weight: 66
series: ["ebpf系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
---

使用libbpf-bootstrap写内核探测工具

<!-- more -->

之前有一篇文章中提到可以用 bpftrace 配合 bt 脚本对内核进行一系列探测。此方法面向对内核相对了解的开发人员，不太容易作为运维工具提供给客户以及 SRE。

[libbpf-bootstrap](https://github.com/libbpf/libbpf-bootstrap) 作为 libbpf 的工作流，可以让开发者略过初始化，编译等环节，直接开始编写 BPF 业务代码。它不像 BCC 或者 Cilium 将 Clang 和 BPF 字符串代码打包到用户态程序中，在运行时编译，而是依赖于 **[BPF CO-RE](https://nakryiko.com/posts/bpf-portability-and-co-re/)** (Compile Once – Run Everywhere) 和内核 **[BTF](https://nakryiko.com/posts/btf-dedup/)** (BPF Type Format) 支持，可以做到一次编译、多内核版本运行。

框架的详细使用方式可以参考这篇文章：[Building BPF applications with libbpf-bootstrap](https://nakryiko.com/posts/libbpf-bootstrap/)

下面介绍一下基于此框架构建的内核观测工具 `kk` (kernel-tracker) 的设计与实现。

### 一、工具设计思路

`kk` 的核心设计目标是成为一个**模块化、易于扩展、对运维友好的内核观测工具集**。它旨在将复杂的内核事件探测封装成一个个独立的、功能明确的“模式”（Mode），用户只需通过简单的命令行参数即可启用特定的观测能力，而无需编写 BPF 代码或深入了解内核细节。

#### 1. 模块化的“模式”设计

`kk` 的功能不是单一的，而是由多个独立的子功能模块构成，我们称之为“模式”（Mode）。例如：

- `monitor` 模式：用于监控特定任务的系统调用、中断、缺页等事件的延迟分布。
- `offcpu` 模式：用于分析任务为何脱离 CPU 运行（sleep/block），并找出导致长时间等待的内核堆栈。
- `net` 模式：专注于网络栈的观测，如追踪 ping 延迟、监控网络设备带宽、分析 TCP 连接事件、定位丢包原因等。
- `kvm`, `mem` 等：针对特定子系统的深度观测。

这种设计的优势在于：

- **功能隔离**：每个模式的代码逻辑（包括参数解析、BPF 程序加载、数据处理和展示）都是独立的，易于开发和维护。
- **按需加载**：用户只关心某个特定问题时，工具仅加载与该模式相关的 BPF 程序，最小化对系统性能的影响。
- **易于扩展**：添加一个新的观测功能，只需实现 `mode_template` 接口并创建一个新的模式即可，对现有代码无侵入。

#### 2. `mode_template` 接口抽象

为了实现上述的模块化设计，代码中抽象出了一个核心结构体 `mode_template`，它定义了一个“模式”所必须具备的所有组件：

```c
struct mode_template {
    const char *name;          // 模式名称，如 "net", "offcpu"
    const char *description;   // 模式的功能描述
    struct argp argp;          // 该模式专属的命令行参数解析器
    void (*init)(void);        // 初始化函数
    bool (*check)(void);       // 参数合法性检查函数
    void (*header)(void);      // 在开始追踪前打印的头部信息
    void (*run)(void);         // 主运行循环，负责数据处理与展示
    void (*dump)(void);        // 结束时的数据汇总与打印
    bool (*load)(bool, const char *); // 决定是否加载某个BPF程序或Map
};
```

整个 `kk` 工具围绕一个全局的 `modes` 数组运转，通过 `-m <mode_name>` 参数选择不同的 `mode_template` 实例，从而切换到不同的功能。

#### 3. BPF 程序和 Map 的动态选择

`kk` 的 BPF 代码 (`kk.bpf.c`) 包含所有模式可能用到的探针。如果一次性加载所有 BPF 程序，会造成巨大的性能开销和资源浪费。

`kk` 通过 `mode_template` 中的 `load` 函数巧妙地解决了这个问题。在 BPF 对象加载（`kk_bpf__load`）之前，用户态程序会遍历 skeleton 中定义的所有 BPF 程序和 Map，并调用当前模式的 `load` 函数来判断：**“我这个模式需要加载你这个 BPF 程序/Map 吗？”**

```c
// kk.c
static void program_select(void) {
    // ...
	for (i = 0; i < s->prog_cnt; i++) {
		// 调用当前模式的 load 函数判断是否需要加载
		autoload = modes[track_mode]->load(false, s->progs[i].name);
		bpf_program__set_autoload(*s->progs[i].prog, autoload);
    }
}
```

例如，在 `net` 模式下，`net_load` 函数会返回 `true` 对于名为 `"netdev..."`, `"tcp..."` 或 `"drop..."` 的 BPF 程序，而对于 `"kvm..."` 或 `"offcpu..."` 则返回 `false`。这样就实现了 BPF 程序的按需加载。

### 二、代码框架解析

`kk` 的代码主要分为三个部分：`kk.c`（用户态主程序）、`kk.h`（共享头文件）和 `kk.bpf.c`（内核态 BPF 程序）。

#### 1. 用户态框架 (`kk.c`)

- **`main()` 函数**:

  1. `default_init()`: 设置信号处理（Ctrl+C 退出）、libbpf 日志回调，并调用所有模式的 `init` 函数进行默认值设置。

  2. `args_init()`: **两步式参数解析**。首先解析 `-m <mode>` 确定主模式，然后根据选定的模式，用其专属的 `argp` 解析器解析后续参数。这是实现 `git` 风格子命令的关键。

  3. `run_init()`: 初始化 BPF。
- `kk_bpf__open()`: 打开 BPF skeleton 对象。
     - `map_select()` 和 `program_select()`: **核心所在**，根据当前模式动态设置哪些 BPF maps 和 programs 需要被加载。
     - `kk_bpf__load()`: 加载 BPF 对象到内核，此时只有被设置为 `autoload` 的部分会真正加载。
     - `kernel_proxy_run()`: 通过临时 attach 一个 BPF 程序到 `cgroup_file_open` 并触发 `open()` 系统调用，来从内核安全地获取 `cgroup id` 等信息。
     
4. `kk_bpf__attach()`: 挂载所有已加载的 BPF 程序到对应的内核探针点。
  
5. `dump_header()`: 打印用户配置和追踪提示信息。
  
6. `run_loop()`: 进入主循环，不断调用当前模式的 `run()` 函数，直到用户按下 Ctrl+C 或达到超时时间。
  
7. `modes[track_mode]->dump()`: 循环结束后，调用当前模式的 `dump()` 函数打印最终的统计报告。
  
8. `kk_bpf__destroy()`: 卸载并清理所有 BPF 资源。

- **数据交互**:

  - **轮询 Maps**: 对于统计类数据（如 `histogram`），`run()` 或 `dump()` 函数会通过 `bpf_map_lookup_elem` 和 `bpf_map_get_next_key` 遍历 BPF Map 获取数据。
  - **Ring Buffer**: 对于事件流类数据（如 `ping` 事件、TCP 状态变更），`run()` 函数会调用 `ring_buffer__poll()`。当 BPF 程序通过 `bpf_ringbuf_output` 提交数据时，用户态注册的回调函数 `ringbuf_wrapper_handler` 会被触发，进而调用特定于模式的事件处理函数（如 `netdev_event_handler`）。

#### 2. 内核态框架 (`kk.bpf.c`)

- **Maps 定义**: 定义了多种 BPF Map，用于内核态数据存储和与用户态通信。
  - `percpu_map`: `BPF_MAP_TYPE_PERCPU_ARRAY` 类型，用于存放临时数据，每个 CPU 一份，避免了并发访问时的锁竞争，性能极高。例如，`task_stack` 临时对象就存放在这里。
  - `histogram` 系列 Maps: 全局变量，用于存储延迟分布等统计数据。BPF 程序使用原子操作（如 `__sync_fetch_and_add`）来更新它们。
  - `ringbuf_map`: `BPF_MAP_TYPE_RINGBUF` 类型，高效的无锁队列，用于将事件数据从内核态实时流式传输到用户态。
  - `task_map`, `stack_map` 等: `LRU_HASH` 类型，用于存储需要跨探针传递的状态信息，例如 `offcpu` 模式下记录任务切出时的状态。
- **BPF 探针**:
  - 每个 BPF 程序都使用 `SEC(...)`宏定义了其类型（kprobe, kretprobe, tracepoint）和要挂载的内核函数或事件点。
  - **过滤逻辑**: 大部分探针函数的第一步都是通过 `track_task()` 或类似的函数进行过滤，判断当前事件是否由用户指定的目标（PID/TID/Cgroup）触发，如果不是则直接返回，将性能影响降到最低。
  - **数据采集**: 探针函数利用 BPF 核心读取（`BPF_CORE_READ`）和 BPF 辅助函数（如 `bpf_ktime_get_ns`, `bpf_get_stack`）从内核结构体中采集所需数据。
  - **数据存储**: 采集到的数据或存入 Hash/Array Map 进行聚合，或推入 Ring Buffer 发送给用户态。

### 三、网络部分工具实现细节 (`net` 模式)

多种子模式通过 `user_conf.net.mode` 控制。

#### 1. 带宽监控 (`NET_MODE_BW`)

- 内核态:

  - 主要挂载 `kprobe/__dev_queue_xmit` 探针，这是网络包从协议栈向下发送到网络设备驱动的必经之路。
- 在 `netdev_xmit_bw_account` 函数中，通过 `BPF_CORE_READ(skb, dev)` 获取网络设备 `struct net_device` 的指针，并以此为 key 更新 `netdev_map`。
  - `__sync_fetch_and_add` 原子地累加 `truesize`（skb 的真实大小）到 `xmit_bytes` 字段。
  
- 用户态:

  - `netdev_bw_run` 函数定期（默认为1秒）遍历 `netdev_map` 和 `netdev_aux_map`（辅助 map，用于存储上一次的计数值）。
- 计算两次读取之间的差值，除以间隔时间，得到速率。
  - 使用 `bw_in_human` 函数将速率格式化为 KB/s, MB/s 等易读形式并打印。

#### 2. Ping 追踪 (`NET_MODE_PING`)

- 内核态:

  - 挂载 `kprobe/__dev_queue_xmit` (发送) 和 `tp/net/netif_receive_skb` (接收) 两个探针。
- `netdev_skb_filter` 函数会解析 SKB（`struct sk_buff`），判断是否为用户指定的 IP 包。
  - `netdev_ping_account` 函数进一步解析 IP 头，检查协议是否为 ICMP，以及 ICMP 类型是否为 `ECHO` 或 `ECHOREPLY`。
  - 如果匹配，则填充一个 `netdev_event` 结构体，包含时间戳、ICMP 序列号、方向（收/发）、目标 IP 等信息，并通过 `bpf_ringbuf_reserve`/`bpf_ringbuf_submit` 发送到 Ring Buffer。
  - **Geneve 隧道解析**: `netdev_skb_geneve_l23` 函数展示了更高级的功能，它能解析 UDP 封装的 Geneve 隧道，并提取内层网络包进行分析。
  
- 用户态:

  - `netdev_ping_run` 函数轮询 Ring Buffer。
- `netdev_event_handler` 回调函数接收到 `netdev_event` 后，格式化并打印出类似 `ping` 命令的实时追踪日志。

#### 3. 丢包分析 (`NET_MODE_DROP`)

- 内核态:

  - 核心探针是 `kprobe/kfree_skb_reason`，这是内核中丢弃网络包（SKB）的统一入口。`reason` 参数指明了丢包的原因。

  - `drop_kfree_skb_reason` 函数被触发后，首先通过 `link_ip` 获取调用栈信息，然后解析 SKB 获取五元组信息。

  - `skb_drop`函数检查该包是否符合用户过滤条件。如果符合：
  
  1. 将包含五元组、丢包原因 `reason` 和堆栈地址 `ip` 的 `tcp_event` 事件推送到 Ring Buffer。
  2. 调用 `sum_stack(ctx, 1)`，以当前内核堆栈为 key，在 `sum_stack_map` 中累加丢包次数。

- 用户态:

  - `sock_tcp_run` 轮询 Ring Buffer，`tcp_event_handler` 打印每个丢包事件的详细信息，包括五元组、原因码和函数地址。`drop_reason` 数组将原因码转换为可读字符串。
- 在程序结束时，`net_dump` 调用 `dump_sum_stack`，它会遍历 `sum_stack_map`，并按丢包次数排序，打印出导致丢包最多的内核调用堆栈。这对于定位疑难丢包问题极其有用。

#### 4. TCP 连接追踪 (`NET_MODE_SOCKET_TCP`)

- 内核态:

  - 挂载多个 TCP 相关探针：
  - `kprobe/tcp_set_state`: 追踪 TCP 状态机的变化。
    - `kprobe/tcp_v4_do_rcv`, `kprobe/__tcp_transmit_skb`: 追踪 TCP 收发包，用于统计带宽。
    - `tp/tcp/tcp_retransmit_skb`: 追踪 TCP 重传事件。
  - 当事件触发且符合过滤条件时，BPF 程序会填充 `tcp_event` 结构体，并通过 Ring Buffer 发送给用户态。
  
- 用户态:

  - `tcp_event_handler` 接收并解析 `tcp_event`，根据 `type` 字段打印出不同的信息，如 "ESTABLISHED -> FIN_WAIT1", "RETRANSMIT", "BW R(1.2MB/s) X(500KB/s)" 等，提供了一个连接生命周期的全景视图。

### 四、工具使用方法

`kk` 提供了类似 `git` 的命令行接口，使用非常方便。

1. **查看所有支持的模式**:

   ```shell
   bash$ sudo ./kk -l
   Kernel Hack Modes:
   monitor: Show delay histogram of different event or other statistics
   offcpu : Show stack trace or other statistics during which tasks are off-cpu
   oncpu  : Show stack trace or other statistics during which tasks are on-cpu
   bdev   : Show statistics of block device
   kvm    : Show statistics of kvm module
   mem    : Show stack trace or other statistics of memory subsystem
   ftrace : Show delay histogram of functions pairs
   net    : Track and collect information of networking stack
   print  : Just for kprobe some functions and print some log into trace_pipe
   ```

2. **查看特定模式的帮助**:

   ```shell
   bash$ sudo ./kk -m net -?
   ```

3. **使用示例**:

   - **示例1：追踪 ping 延迟** 追踪所有发往 `8.8.8.8` 的 ping 包，并显示收发事件。

     ```
     bash$ sudo ./kk -m net -e -a 8.8.8.8
     KK Configuration:
     ...
     INET Tuple : fuzzy 8080000:0
     Geneve Port: 0
     Output abbr: [Recv(R) Xmit(X) Geneve(G)] Ping(P) Reply(R)
     KK starts to track, use CTRL + C to stop
     [         0] [X-]   8.  8.  8.  8  P 1 ubuntu-2004-de (246-3)
     [    232975] [R-]   8.  8.  8.  8  R 1 ubuntu-2004-de (246-3)
     [   1000185] [X-]   8.  8.  8.  8  P 2 ubuntu-2004-de (246-3)
     [   1232870] [R-]   8.  8.  8.  8  R 2 ubuntu-2004-de (246-3)
     ```

   - **示例2：分析 off-CPU 堆栈** 分析 PID 为 12345 的进程为何进入不可中断睡眠（`D` 状态）超过 100 毫秒，并打印出当时的内核调用栈。

     ```sh
     bash$ sudo ./kk -m offcpu -p 12345 -s D -T 100
     ...
     TASK PID 12345 COMM my_app DELAY 153 ms
     ---------------------------
         0 + __schedule
       241 + schedule
       ... + io_schedule
       ... + sync_page
     ---------------------------
     ```

   - **示例3：监控系统调用延迟** 监控 PID 为 12345 的进程 `read` 和 `write` 系统调用的延迟分布直方图。

     ```shell
     bash$ sudo ./kk -m monitor -p 12345 read write
     ...
     ^C
     Total duration of tracking is 10 seconds
     [read] avg 5 us
          MIN -> MAX      : PCT%      TOTAL 
            0 -> 63       :  95%         1900 
           64 -> 127      :   4%           80
          128 -> 255      :   1%           20
     [write] avg 2 us
          MIN -> MAX      : PCT%      TOTAL
            0 -> 63       : 100%         5000
     ```

   - **示例4：定位网络丢包** 追踪发往 `192.168.1.100:80` 的 TCP 包的丢包事件，并在结束时打印导致丢包最多的内核堆栈。

     ```shell
     bash$ sudo ./kk -m net -d -A 192.168.1.100 -P 80 -t -s
     ...
     ^C
     Total duration of tracking is 20 seconds
     SKB Drop Sum 120
     ---------------------------
        0 + kfree_skb_reason
      ... + nf_hook_slow
      ... + nf_drop
      ... + br_drop
      ... + br_handle_frame_finish
     ---------------------------
     SKB Drop Sum 5
     ---------------------------
        0 + kfree_skb_reason
      ... + tcp_v4_rcv
      ... + __skb_checksum_complete
     ---------------------------
     ```



### repository

[scottlx/kernel-tracker](https://github.com/scottlx/kernel-tracker)