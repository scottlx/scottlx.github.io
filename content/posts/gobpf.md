---
title: "gobpf不完整使用指南"
date: 2022-10-03T14:00:00+08:00
draft: false
tags: ["ebpf","xdp","高性能网络"]
tags_weight: 66
series: ["bpf系列"]
series_weight: 96
categories: ["操作指南"]
categoryes_weight: 96
toc : true
---

### 编译过程

#### 安装llvm-10,clang-10

apt-install llvm-10 clang-10

#### 下载bpf2go

```shell
go install github.com/cilium/ebpf/cmd/bpf2go@latest
```

修改bpf程序的include

```c
#include "common.h"
```

#### 编译时将bpd的headers包含进来

```shell
GOPACKAGE=main bpf2go -cc clang-10 -cflags '-O2 -g -Wall -Werror' -target bpfel,bpfeb bpf helloworld.bpf.c -- -I /root/ebpf/examples/headers
```

得到大端和小端两个版本的ELF文件，之后在go程序里加载即可。cpu一般都是小端。

### 内核版本要求

经测试一些gobpf的一些syscall不适配较低版本的内核（例如5.8的BPF_LINK_CREATE会报参数错误），建议使用最新版本内核5.19

### bpf_map

用户态程序首先加载bpf maps，再将bpf maps绑定到fd上。elf文件中的realocation table用来将代码中的bpf maps重定向至正确的fd上,用户程序在fd上发起bpf syscall

map的value尽量不要存复合数据结构，若bpf程序和用户态程序共用一个头文件，用户态程序调用bpf.Lookup时由于结构体变量unexported而反射失败

### pinning object

将map挂载到/sys/fs/bpf

```go
ebpf.CollectionOptions{
   Maps: ebpf.MapOptions{
      // Pin the map to the BPF filesystem and configure the
      // library to automatically re-write it in the BPF
      // program so it can be re-used if it already exists or
      // create it if not
      PinPath: pinPath
```

其他用户态程序获取pinned map的fd

```go
ebpf.LoadPinnedMap
```

### packet processing

使用系统/usr/include下的包头解析lib

```c
#include <stddef.h>
#include <linux/bpf.h>
#include <linux/in.h>
#include <linux/if_ether.h>
#include <linux/if_packet.h>
#include <linux/ipv6.h>
#include <linux/icmpv6.h>
```

其中ubuntu缺失asm库，需要安装gcc-multilib

```shell
apt-get install -y gcc-multilib
```



ctx存放dma区报文的指针，同时存放报文的设备号和队列号

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

由于ctx中的指针会发生变动，一般创建两个局部变量来存

```c
void *data_end = (void *)(long)ctx->data_end;
void *data = (void *)(long)ctx->data;
```

为了缩减报文边界检查次数（例如，先检查eth头长度是否合法，再检查ip头长度是否合法，会造成多次检查，影响性能），检查器在加载bpf程序时就会根据针对data_end的if语句进行静态检查。例如某bpf函数内需要读data指针后10个地址的数据，需要进行如下的校验，否则检查器会拒绝加载 XDP 子节代码

```c
if (data + 10 < data_end)
  /* do something with the first 10 bytes of data */
else
  /* skip the packet access */
```



### Example



按照[xdp-project/xdp-tutorial: XDP tutorial (github.com)](https://github.com/xdp-project/xdp-tutorial)移植的gobpf程序（还未完成）：

[go-base/src/ebpf/xdp-ex at main · scottlx/go-base (github.com)](https://github.com/scottlx/go-base/tree/main/src/ebpf/xdp-ex)



### 参考文档

1. [linux/samples/bpf at master · torvalds/linux (github.com)](https://github.com/torvalds/linux/tree/master/samples/bpf)

2. [BPF 进阶笔记（一）：BPF 程序（BPF Prog）类型详解：使用场景、函数签名、执行位置及程序示例 (arthurchiao.art)](http://arthurchiao.art/blog/bpf-advanced-notes-1-zh/)

3. [tc/BPF and XDP/BPF - Hangbin Liu's blog (liuhangbin.netlify.app)](https://liuhangbin.netlify.app/post/ebpf-and-xdp/)

4. [BPF and XDP Reference Guide — Cilium 1.12.2 documentation](https://docs.cilium.io/en/stable/bpf/#:~:text=cBPF is known,before program execution.)
