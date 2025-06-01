---
title: "dpvs icmp session"
date: 2025-02-18T11:11:00+08:00
draft: false
tags: ["dpdk", "dpvs", "高性能网络"]
tags_weight: 66
series: ["dpvs系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
---

icmp 特殊 session

<!-- more -->

原生的 ipvs 仅处理三种类型的 ICMP 报文：ICMP_DEST_UNREACH、ICMP_SOURCE_QUENCH 和 ICMP_TIME_EXCEEDED

对于不是这三种类型的 ICMP，则设置为不相关联(related)的 ICMP，返回 NF_ACCEPT，之后走本机路由流程

dpvs 对 ipvs 进行了一些修改，修改后逻辑如下

## icmp 差错报文流程

- \_\_dp_vs_in

- \_\_dp_vs_in_icmp4 （处理 icmp 差错报文，入参 related 表示找到了关联的 conn）

  若不是 ICMP_DEST_UNREACH，ICMP_SOURCE_QUENCH，ICMP_TIME_EXCEEDED，返回到\_dp_vs_in 走普通 conn 命中流程

  icmp 差错报文，需要将报文头偏移到 icmp 头内部的 ip 头，**根据内部 ip 头查找内部 ip 的 conn**。

  若找到 conn，**表明此 ICMP 报文是由之前客户端的请求报文所触发的，由真实服务器回复的 ICMP 报文**。将 related 置 1

  若未找到则返回 accept，返回到\_dp_vs_in 走普通 conn 命中流程

  - ​ \_\_xmit_inbound_icmp4

  ​ 找 net 和 local 路由，之后走\_\_dp_vs_xmit_icmp4

  - \_\_dp_vs_xmit_icmp4

  ​ 数据区的前 8 个字节恰好覆盖了 TCP 报文或 UDP 报文中的端口号字段（前四个字节）

  inbound 方向根据内部 ip 的 conn 修改数据区目的端口为 conn->dport，源端口改为 conn->localport，

  outbound 方向将目的端口改为 conn->cport，源端口改为 conn->vport

  ​

  client (cport ) <--> (vport)lb(lport) <--> rs(dport)

  ​ 重新计算 icmp 头的 checksum，走 ipv4_output

![报文格式](/img/dpvs/icmp差错报文.png)

**实际应用上的问题**

某个 rs 突然下线，导致有时访问 vip 轮询到了不可达的 rs，rs 侧的网关发送了一个 dest_unreach 的 icmp 包

该 rs 的 conn 还未老化，\_\_dp_vs_in_icmp4 流程根据这个 icmp 的内部差错 ip 头找到了还未老化的 conn，将 icmp 数据区的 port 进行修改发回给 client

但是一般情况，rs 下线后，该 rs 的 conn 会老化消失，内层 conn 未命中，还是走外层 icmp 的 conn 命中流程转给 client。这样内部数据区的端口信息是错的（dport->lport，正确情况是 vport->cport）

## 非差错报文流程

返回\_dp_vs_in 走普通 conn 命中流程

原本 dp_vs_conn_new 流程中，先查找 svc。icmp 的 svc 默认使用端口 0 进行查找。但是 ipvsadm 命令却对端口 0 的 service 添加做了限制，导致无法添加这类 svc。

```c
 svc = dp_vs_service_lookup(iph->af, iph->proto, &iph->daddr, 0, 0,                               mbuf, NULL, &outwall, rte_lcore_id());
```

若未查到走 INET_ACCEPT(也就是继续往下进行走到 ipv4_output_fin2 查到 local 路由，若使用 dpip addr 配上了 vip 或 lip 地址，则会触发本地代答)。

若查到 svc，则进行 conn 的 schedule，之后会走 dp_vs_laddr_bind，但是 dp_vs_laddr_bind 不支持 icmp 协议(可以整改)，最终导致 svc 可以查到但是 conn 无法建立，最后走 INET_DROP。

概括一下：

- - 未命中 svc，走后续 local route，最终本地代答
  - 命中 svc 后若 conn 无法建立，drop
  - 命中 svc 且建立 conn，发往 rs 或 client

### icmp 的 conn

​

```c
_ports[0] = icmp4_id(ich);
_ports[1] = ich->type << 8 | ich->code;
```

Inbound hash 和 outboundhash 的五元组都使用上述这两个 port 进行哈希，并与 conn 进行关联。

具体的 laddr 和 lport 保存在 conn 里面。其中只用到 laddr 做 l3 的 fullnat。由于 icmp 协议没有定义 proto->fnat_in_handler，因此 fnat 时，从 sa_pool 分配到的 lport 对于 icmp 来说没有用。

试想 ping request 和 ping reply 场景：

由于 request 和 reply 的 ich->type 不一样，outboundhash 必定不命中(且 fnat 流程中的 laddr_bind 还会修改一次 outboundhashtuple 的 dport，修改成 sapool 分配的 port，因此也不会命中 outbound hash)。

**一次来回的 ping 会创建两个 conn，且都只命中 inboundhash。**

## 个人认为比较合理的方案

#### ipvsadm

放通 port=0 的 svc 的创建，用户需要 fwd icmp to rs 时，需要添加 icmp 类型的 svc。否则 icmp 会被 vip 或者 lip 代答，不会透传到 rs 或 client

#### icmp 差错报文

保持原状，对找不到关联的 conn 连接的差错报文进行 drop。

但一般情况下若发生差错，关联的 conn 大概率已经老化，此时做 related 的处理意义不大

#### icmp 查询报文

保持原状，建立 icmp conn，来回创建两个 conn。
