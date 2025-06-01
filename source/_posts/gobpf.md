---
title: "gobpf不完整使用指南"
date: 2022-10-03T14:00:00+08:00
draft: false
tags: ["ebpf", "xdp", "高性能网络"]
tags_weight: 66
series: ["bpf系列"]
series_weight: 96
categories: ["操作指南"]
categoryes_weight: 96
---

<!-- more -->

### 编译过程

#### 安装 llvm-10,clang-10

apt-install llvm-10 clang-10

#### 下载 bpf2go

```shell
go install github.com/cilium/ebpf/cmd/bpf2go@latest
```

修改 bpf 程序的 include

```c
#include "common.h"
```

#### 编译时将 bpd 的 headers 包含进来

```shell
GOPACKAGE=main bpf2go -cc clang-10 -cflags '-O2 -g -Wall -Werror' -target bpfel,bpfeb bpf helloworld.bpf.c -- -I /root/ebpf/examples/headers
```

得到大端和小端两个版本的 ELF 文件，之后在 go 程序里加载即可。cpu 一般都是小端。

### 内核版本要求

经测试一些 gobpf 的一些 syscall 不适配较低版本的内核（例如 5.8 的 BPF_LINK_CREATE 会报参数错误），建议使用最新版本内核 5.19

### bpf_map

用户态程序首先加载 bpf maps，再将 bpf maps 绑定到 fd 上。elf 文件中的 realocation table 用来将代码中的 bpf maps 重定向至正确的 fd 上,用户程序在 fd 上发起 bpf syscall

map 的 value 尽量不要存复合数据结构，若 bpf 程序和用户态程序共用一个头文件，用户态程序调用 bpf.Lookup 时由于结构体变量 unexported 而反射失败

### pinning object

将 map 挂载到/sys/fs/bpf

```go
ebpf.CollectionOptions{
   Maps: ebpf.MapOptions{
      // Pin the map to the BPF filesystem and configure the
      // library to automatically re-write it in the BPF
      // program so it can be re-used if it already exists or
      // create it if not
      PinPath: pinPath
```

其他用户态程序获取 pinned map 的 fd

```go
ebpf.LoadPinnedMap
```

### packet processing

使用系统/usr/include 下的包头解析 lib

```c
#include <stddef.h>
#include <linux/bpf.h>
#include <linux/in.h>
#include <linux/if_ether.h>
#include <linux/if_packet.h>
#include <linux/ipv6.h>
#include <linux/icmpv6.h>
```

其中 ubuntu 缺失 asm 库，需要安装 gcc-multilib

```shell
apt-get install -y gcc-multilib
```

ctx 存放 dma 区报文的指针，同时存放报文的设备号和队列号

```c
struct xdp_md {
	__u32 data;
	__u32 data_end;
	__u32 data_meta;
	/* Below access go through struct xdp_rxq_info */
	__u32 ingress_ifindex; /* rxq->dev->ifindex */
	__u32 rx_queue_index;  /* rxq->queue_index  */
};
```

由于 ctx 中的指针会发生变动，一般创建两个局部变量来存

```c
void *data_end = (void *)(long)ctx->data_end;
void *data = (void *)(long)ctx->data;
```

为了缩减报文边界检查次数（例如，先检查 eth 头长度是否合法，再检查 ip 头长度是否合法，会造成多次检查，影响性能），检查器在加载 bpf 程序时就会根据针对 data_end 的 if 语句进行静态检查。例如某 bpf 函数内需要读 data 指针后 10 个地址的数据，需要进行如下的校验，否则检查器会拒绝加载 XDP 子节代码

```c
if (data + 10 < data_end)
  /* do something with the first 10 bytes of data */
else
  /* skip the packet access */
```

### Example

按照[xdp-project/xdp-tutorial: XDP tutorial (github.com)](https://github.com/xdp-project/xdp-tutorial)移植的 gobpf 程序（还未完成）：

[go-base/src/ebpf/xdp-ex at main · scottlx/go-base (github.com)](https://github.com/scottlx/go-base/tree/main/src/ebpf/xdp-ex)

### 参考文档

1. [linux/samples/bpf at master · torvalds/linux (github.com)](https://github.com/torvalds/linux/tree/master/samples/bpf)

2. [BPF 进阶笔记（一）：BPF 程序（BPF Prog）类型详解：使用场景、函数签名、执行位置及程序示例 (arthurchiao.art)](http://arthurchiao.art/blog/bpf-advanced-notes-1-zh/)

3. [tc/BPF and XDP/BPF - Hangbin Liu's blog (liuhangbin.netlify.app)](https://liuhangbin.netlify.app/post/ebpf-and-xdp/)

4. [BPF and XDP Reference Guide — Cilium 1.12.2 documentation](https://docs.cilium.io/en/stable/bpf/#:~:text=cBPF is known,before program execution.)
