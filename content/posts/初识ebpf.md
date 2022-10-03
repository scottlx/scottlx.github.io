---
title: "初始ebpf"
date: 2022-10-03T14:00:00+08:00
draft: false
tags: ["ebpf","xdp","高性能网络"]
tags_weight: 66
series: ["bpf系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
---









摘自

[eBPF 用户空间虚拟机实现相关 | Blog (forsworns.github.io)](https://forsworns.github.io/zh/blogs/20210329/)

[[译\] Cilium：BPF 和 XDP 参考指南（2021） (arthurchiao.art)](http://arthurchiao.art/blog/cilium-bpf-xdp-reference-guide-zh/)

### hook point

可以插入bpf代码的位置

```c
enum bpf_prog_type {
    BPF_PROG_TYPE_UNSPEC,
    BPF_PROG_TYPE_SOCKET_FILTER,
    BPF_PROG_TYPE_KPROBE,
    BPF_PROG_TYPE_SCHED_CLS,
    BPF_PROG_TYPE_SCHED_ACT,
    BPF_PROG_TYPE_TRACEPOINT,
    BPF_PROG_TYPE_XDP,
    BPF_PROG_TYPE_PERF_EVENT,
    BPF_PROG_TYPE_CGROUP_SKB,
    BPF_PROG_TYPE_CGROUP_SOCK,
    BPF_PROG_TYPE_LWT_IN,
    BPF_PROG_TYPE_LWT_OUT,
    BPF_PROG_TYPE_LWT_XMIT,
    BPF_PROG_TYPE_SOCK_OPS,
    BPF_PROG_TYPE_SK_SKB,
};
```

### 程序类型

| **bpf_prog_type**               | **BPF prog** 入口参数（R1)                                  | **程序类型**                                                 |
| ------------------------------- | ----------------------------------------------------------- | ------------------------------------------------------------ |
| **BPF_PROG_TYPE_SOCKET_FILTER** | **struct __sk_buff**                                        | 用于过滤进出口网络报文，功能上和 cBPF 类似。                 |
| **BPF_PROG_TYPE_KPROBE**        | **struct** **pt_regs**                                      | 用于 kprobe 功能的 BPF 代码。                                |
| **BPF_PROG_TYPE_TRACEPOINT**    | 这类 BPF 的参数比较特殊，根据 tracepoint 位置的不同而不同。 | 用于在各个 tracepoint 节点运行。                             |
| **BPF_PROG_TYPE_XDP**           | **struct** **xdp_md**                                       | 用于控制 XDP(eXtreme Data Path)的 BPF 代码。                 |
| **BPF_PROG_TYPE_PERF_EVENT**    | **struct bpf_perf_event_data**                              | 用于定义 perf event 发生时回调的 BPF 代码。                  |
| **BPF_PROG_TYPE_CGROUP_SKB**    | **struct __sk_buff**                                        | 用于在 network cgroup 中运行的 BPF 代码。功能上和 Socket_Filter 近似。具体用法可以参考范例 test_cgrp2_attach。 |
| **BPF_PROG_TYPE_CGROUP_SOCK**   | **struct bpf_sock**                                         | 另一个用于在 network cgroup 中运行的 BPF 代码，范例 test_cgrp2_sock2 中就展示了一个利用 BPF 来控制 host 和 netns 间通信的例子。 |

 BPF 程序类型就是由 BPF side 的代码的函数参数确定的，比如写了一个函数，参数是 `struct __sk_buff` 类型的，它就是一个 **BPF_PROG_TYPE_SOCKET_FILTER** 类型的 BPF 程序

### XDP

express datapath

是一个bpf hook点，位于网络驱动收包的最开始，协议栈还没有提取出包的metadata，因此适合做包过滤，例如DDOS防护。cillium的prefilter在此处实现

### tc ingress/egress

三层协议栈之前的hook点，报文经过了一些初始处理。适合做本地收包处理，比如l3/l4策略或者转发。tc hook可以与xdp hook进行结合，经过这两层过滤后可以认定大部分报文都是合法的且可以发给host

容器场景下一般用的veth pair，需要将bpf程序挂到host侧的veth的tc ingress hook上。再配合上host业务网卡的bpf程序，可以实现host级别和容器级别的policy



![Location of netfilter and eBPF hooks.](https://www.researchgate.net/profile/Sebastiano-Miano/publication/333998355/figure/fig1/AS:773670205128705@1561468759221/Location-of-netfilter-and-eBPF-hooks.png)

### socket operations

绑在root cgroup上监听tcp状态

### Socket send/recv

监听send/recv 系统调用，可以查看message并根据message执行丢弃或转发动作

![See the source image](https://s34378.pcdn.co/wp-content/uploads/2020/06/bpf-kernel-hooks.png)



## bgp架构

bpf提供了：

- RISC指令集 （一套虚拟机）
- kv存储的map
- 与内核交互的helper functions
- 调用其他bpf程序的接口
- 一个虚拟的文件系统，用来加载网卡，程序以及上述的map

bpf程序的编译后端是llvm，因此用c编译bpf程序需要用clang编译成llvm中间代码。加载bfp程序后，内核即时编译器会动态生成opcodes，实现指令注入的效果。即使注入指令有以下优点：

- 报文不需要内核态用户态的切换
- 根据业务场景需求灵活修改数据面代码
- 更新程序不需要重启内核
- 多平台兼容，不需要第三方模块
- 复用内核的资源（驱动，协议栈等），但由于程序经过内核校验，不会将内核搞崩



代码模型是事件驱动模型。例如报文处理的bpd程序在收包时被唤醒，kprobe程序在代码调用到某个系统调用地址时被唤醒

### 指令集与寄存器

11个64位寄存器（r0~r10），一个PC，512个字节的栈空间

r0存放函数的返回值，也可以存放bpf程序的退出值

r1-r5是临时寄存器，可以泄露到bpf栈中，也可以用来存放helper functions的入参。泄露spilling指的是寄存器的值移动到bpf栈中，填充filling指的是bpf栈中的数据移动至寄存器中。其中r1指向上下文，例如包处理时指向skb。这么做的原因是由于限制了寄存器数量，寄存器不够用

r6-r9是callee函数用的通用寄存器

r10存放FP，只读

每个bpf程序限制4096条指令，5.1版本内核限制100万条指令。虽然指令集支持jumps指令，但是内核校验器不允许循环，且跳到另一个bpf程序的限制为33次，因此代码需要展开循环

指令集中所有指令的长度固定为64bit，目前共实现了87条指令。指令有大端小端的区分，由linux/bpf.h定义

[eBPF Instruction Set — The Linux Kernel documentation](https://www.kernel.org/doc/html/latest/bpf/instruction-set.html)



| 32 bits (MSB) | 16 bits | 4 bits          | 4 bits               | 8 bits (LSB) |
| ------------- | ------- | --------------- | -------------------- | ------------ |
| immediate     | offset  | source register | destination register | opcode       |

与通用的指令集一样，bpf指令的opcode也分为load/save（LD，ST），process（alu）和branch（jmp）。opcode字段的地3位决定了指令类型（cbpf，ebpf），目前大部分架构的内核eBPF JIT编译器支持自动将cbpf指令转为ebpf

### helper functions

提供了与内核的接口，retrieve / push data from / to the kernel。可用的helper function取决于bpf程序锁挂载的位置。挂到socket层可用的helper是挂到tc层可用的helper的一个子集

格式统一

```c
u64 fn(u64 r1, u64 r2, u64 r3, u64 r4, u64 r5)
```

只能调用内核支持的helper函数。内核将helper抽象成以下的宏,目的是为了与cBPF兼容

BPF_CALL_0() to BPF_CALL_5()

### Maps

高性能kv存储，可以作为多个bpf程序的共享全局变量，用户态程序使用fd访问。分为通用map和非通用map。关于map的种类和用法参考

[BPF 进阶笔记（二）：BPF Map 类型详解：使用场景、程序示例 (arthurchiao.art)](http://arthurchiao.art/blog/bpf-advanced-notes-2-zh/)

代码中map的初始化

```c
struct {
    __uint(type, BPF_MAP_TYPE_HASH);  // BPF map 类型
    __type(key, __be32);              // 目的 IP 地址
    __type(value, struct pair);       // 包数和字节数
    __uint(max_entries, 1024);        // 最大 entry 数量
} hash_map SEC(".maps");              // 放在ELF的maps区
```

### object pinining

内核中的bpf程序，bpf map只能通过文件描述符或inode的方式传递到用户态。bpf程序和bpf map可以绑定到虚拟文件系统里/sys/fs/bpf/, 从而实现多个bpf程序共享bpf map

### tail call

只允许相同类型的bpf程序进行尾调用。调用后不会返回到原来函数，不改变栈顶指针，直接复用相同的栈帧。x86的5.10版本内核才支持尾调用和普通调用混合使用。

```c
#ifndef __stringify
# define __stringify(X)   #X
#endif

#ifndef __section
# define __section(NAME)                  \
   __attribute__((section(NAME), used))
#endif

#ifndef __section_tail
# define __section_tail(ID, KEY)          \
   __section(__stringify(ID) "/" __stringify(KEY))
#endif

#ifndef BPF_FUNC
# define BPF_FUNC(NAME, ...)              \
   (*NAME)(__VA_ARGS__) = (void *)BPF_FUNC_##NAME
#endif

#define BPF_JMP_MAP_ID   1

static void BPF_FUNC(tail_call, struct __sk_buff *skb, void *map,
                     uint32_t index);

struct bpf_elf_map jmp_map __section("maps") = {
    .type           = BPF_MAP_TYPE_PROG_ARRAY,
    .id             = BPF_JMP_MAP_ID,
    .size_key       = sizeof(uint32_t),
    .size_value     = sizeof(uint32_t),
    .pinning        = PIN_GLOBAL_NS,
    .max_elem       = 1,
};

__section_tail(BPF_JMP_MAP_ID, 0)
int looper(struct __sk_buff *skb)
{
    printk("skb cb: %u\n", skb->cb[0]++);
    tail_call(skb, &jmp_map, 0);
    return TC_ACT_OK;
}

__section("prog")
int entry(struct __sk_buff *skb)
{
    skb->cb[0] = 0;
    tail_call(skb, &jmp_map, 0);
    return TC_ACT_OK;
}

char __license[] __section("license") = "GPL";
```

使用方法：

1. 由于不能使用全局变量，定义一个BPF_MAP_TYPE_PROG_ARRAY用来注册尾调用函数
2. 给尾调用函数打标记：`__section(__stringify(ID) "/" #KEY`
3. 传入同类型bpf程序的参数(skb)，注册的map，调用函数的key `tail_call(skb, &jmp_map, 0);`

