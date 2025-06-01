---
title: "bpftrace注入kfunc"
date: 2025-05-30T13:53:00+08:00
draft: false
tags: ["ebpf", "linux"]
tags_weight: 66
series: ["ebpf系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
---

[Working with kernel probes | bpftrace](https://bpftrace.org/hol/kernel-probes) 笔记

<!-- more -->

### 四种探针类型

#### **kprobe**

- **定义**：动态内核探针，允许在**任意内核函数**的入口（`kprobe`）或退出（`kretprobe`）插入断点。
- 特点：
  - **动态性**：无需修改内核代码，运行时动态注入。
  - **灵活性**：可跟踪几乎所有内核函数（包括未导出的符号）。
  - **开销**：较高（需修改指令、处理陷阱），可能影响性能。
  - **稳定性**：内核函数可能随版本变化，导致跟踪点失效。
- **用途**：调试、性能分析、动态跟踪未预设的事件。

---

#### **kfunc**

- **定义**：eBPF 程序可调用的**内核函数**，由内核显式导出供安全调用。
- 特点：
  - **安全性**：仅允许调用内核标记为`BTF_ID`的特定函数（通过 BPF Type Format, BTF）。
  - **性能**：直接调用内核函数，比 eBPF Helper 更高效。
  - **依赖 eBPF**：需通过 eBPF 验证器确保安全性。
- **用途**：允许 eBPF 程序安全访问内核内部数据结构或功能（如操作链表、修改特定字段）。

---

#### **tracepoint**

- **定义**：内核静态跟踪点，由开发者**预置在代码中**的稳定事件接口。
- 特点：
  - **静态性**：需内核开发者预先定义，位置和参数格式固定。
  - **稳定性**：接口向后兼容，适合生产环境。
  - **结构化数据**：参数以明确结构体传递（如`trace_sched_switch`）。
  - **低开销**：相比 kprobe，性能影响较小。
- **用途**：监控系统调用、调度事件等预定义内核事件。

---

#### ** rawtracepoint**

- **定义**：直接访问 tracepoint 的**原始参数**，跳过内核封装层。
- 特点：
  - **底层访问**：直接读取寄存器或原始参数，无需解析结构体。
  - **性能优势**：比常规 tracepoint 更高效（减少封装开销）。
  - **不稳定性**：参数格式可能随内核变化，需手动适配。
  - **依赖 eBPF**：常用于 eBPF 程序（如`BPF_PROG_TYPE_RAW_TRACEPOINT`类型）。
- **用途**：需要极致性能的场景（如高频事件跟踪），同时接受潜在兼容性风险。

### btf 支持

[BPF 类型格式 (BTF) — Linux 内核文档 - Linux 内核](https://linuxkernel.org.cn/doc/html/latest/bpf/btf.html)

系统若有 btf 支持，结构体格式就可以通过 btf 传给工具，用户无需去查阅内核源码

```shell
sudo bpftrace --info |& grep -i btf
```

### kfunc probe

调试 ebpf 代码时，遇到 icmp request 接收了，但是内核协议栈不返回 icmp reply

下面给出上述问题的排查思路

查看`/proc/net/snmp`发现有**InHdrErrors**

或者执行

```shell
netstat -s
```

发现 Ip 协议栈有 invalid headers 的报错

查看 icmp\_开头的 kfunc 点

```shell
bpftrace -lv 'kfunc:icmp_*'
```

输出

```
...
kfunc:icmp_rcv
    struct sk_buff * skb
    int retval
kfunc:icmp_redirect
    struct sk_buff * skb
    bool retval
kfunc:icmp_reply
    struct icmp_bxm * icmp_param
    struct sk_buff * skb
...
```

查看 sock 结构体定义

```shell
sudo bpftrace -lv 'struct sk_buff'
```

样例输出

```c
struct sk_buff {
        union {
                struct {
                        struct sk_buff *next;
                        struct sk_buff *prev;
                        union {
                                struct net_device *dev;
                                long unsigned int dev_scratch;
                        };
                };
                struct rb_node rbnode;
                struct list_head list;
                struct llist_node ll_node;
        };
        union {
                struct sock *sk;
                int ip_defrag_offset;
        };
        union {
                ktime_t tstamp;
                u64 skb_mstamp_ns;
        };
        char cb[48];
        union {
                struct {
                        long unsigned int _skb_refdst;
                        void (*destructor)(struct sk_buff *);
                };
...
```

查看 icmp_rcv 是否触发，直接打印 skb

```shell
bpftrace -e 'kfunc:icmp_rcv{print(*args->skb)}'
```

编写打桩脚本，过滤异常报文(ip_header 不合法的报文)

```shell
vi ip_rcv_core.bt
```

```c
#include <linux/icmp.h>

#include <linux/ip.h>


kfunc:ip_rcv_core {

  $skb = (struct sk_buff *)args->skb;

  $iph = (struct iphdr *)$skb->data;


  if ($iph->ihl < 5 || $iph->version !=4) {
     print(*$iph);
  }

}
```

执行 trace

```shell
bpftrace -e ip_rcv_core.bt
```

发现有不合法报文，并可以打印出字节，发现是以太头

最后发现三层 tunnel 设备的 ebpf 代码中，如果直接将以太报文 redirect 给二层设备是不可行的。

三层 tunnel 设备需要发送报文给二层设备，需要时 raw ip 报文才可被正常接收

```c
// 三层tunnel设备redirect给二层设备时，不走二层协议栈，直接进入ip协议栈。
static struct sk_buff *ip_rcv_core(struct sk_buff *skb, struct net *net)
{
	const struct iphdr *iph;
	int drop_reason;
	u32 len;

	/* When the interface is in promisc. mode, drop all the crap
	 * that it receives, do not try to analyse it.
	 */
	if (skb->pkt_type == PACKET_OTHERHOST) {
		dev_core_stats_rx_otherhost_dropped_inc(skb->dev);
		drop_reason = SKB_DROP_REASON_OTHERHOST;
		goto drop;
	}

	__IP_UPD_PO_STATS(net, IPSTATS_MIB_IN, skb->len);

	skb = skb_share_check(skb, GFP_ATOMIC);
	if (!skb) {
		__IP_INC_STATS(net, IPSTATS_MIB_INDISCARDS);
		goto out;
	}

	drop_reason = SKB_DROP_REASON_NOT_SPECIFIED;
	if (!pskb_may_pull(skb, sizeof(struct iphdr)))
		goto inhdr_error;

	iph = ip_hdr(skb);

	/*
	 *	RFC1122: 3.2.1.2 MUST silently discard any IP frame that fails the checksum.
	 *
	 *	Is the datagram acceptable?
	 *
	 *	1.	Length at least the size of an ip header
	 *	2.	Version of 4
	 *	3.	Checksums correctly. [Speed optimisation for later, skip loopback checksums]
	 *	4.	Doesn't have a bogus length
	 */

	if (iph->ihl < 5 || iph->version != 4)
		goto inhdr_error;  // 丢包处
```

> 参考

[3. Working with kernel probes | bpftrace](https://bpftrace.org/hol/kernel-probes)
