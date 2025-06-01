---
title: "初识srv6"
date: 2022-10-01T09:18:21+08:00
draft: false
tags: ["nfv", "sdn", "vpp", "srv6"]
tags_weight: 66
series: ["vpp系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
---

翻译自 SRv6 Network Programming
draft-filsfils-spring-srv6-network-programming-07

<!-- more -->

### SRH

Segment Routing Header

SRH 在一个报文中可以有多个

### NH

ipv6 next-header field

Srv6 的 Routing Header 的 type 是 4，IP6 header 的 NH 字段是 43

### SID

编排链节点的 ID，srv6 节点的 SID table 里面保存自己在各个编排链内的 SID。local SID 可以是设备外部接口（不会是内部接口）的 ipv6 地址。例如已经在外部接口配置了地址 A 和地址 B，内部 loopback 配置了地址 C。地址 A 和地址 B 会默认被加入到 SID Table。

地址 B 可以是路由不可达的，为什么？

可以将地址 A 理解成全局 segments，地址 B 为本地 segments。只要报文在发送时加入了 SID list<A,B>，A 在 B 的前面，只要 A 对外路由可达，报文就会被送到 A，然后在本地进行下一步的处理（发往本地的 B）

(SA,DA) (S3, S2, S1; SL)

S1 是第一跳，S3 是最后一跳。SL 剩下几跳，也可理解为下一个 SID 节点的下标。例如 SL=0， 表示 SRH[0]=S3，下一个 SID 处理节点的 ip 地址是 S3

### SID 格式

SID Table 中并不是以 Ip 的形式保存 SID 的

LOC:FUNCT:ARGS::

### function

每个 SID 可以绑定多个 function。function 与 SID 的绑定关系存在 SID Table 中。这个特性决定了 SRV6 的高度可编程性。

function 太多，不一一列出，总结下规律

带有 D 的，表示 Decapsulation，如果 SL==0（已经是最后一跳）且 NH!=SRH(没有嵌套另一个 SRH)，且 SRH 的 ENH（下一层 header 类别）符合 function 的定义(例如 DT6，ENH 必须是 41(ipv6 encapsulation))，则剥去 SRH

带有 T 的，表示 table，查对应的 fib 表

带有 X 的，表示 cross-connect，往邻接表对应的 Ip 地址发（直接拿 mac）

带 V 的，表示 Vlan，往对应 Vlan 发（改 Vlan 头部）

带 B 的，表示 bond，insert 在老的 SRH 和 Ipv6 Header 之间新插入一个 SRH，将 DA 改为新的 SRH 的第一个 segment；encap 则是在最外面新插入一个 ipv6 头部，新 ipv6 头部 SA 是内部 ipv6 头部的 SA，DA 是新 ipv6 头部下的 SRH 的第一跳
