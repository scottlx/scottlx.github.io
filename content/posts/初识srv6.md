---
title: "初识srv6"
date: 2022-10-01T09:18:21+08:00
draft: false
tags: ["nfv","sdn","vpp","srv6"]
tags_weight: 66
series: ["vpp系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
toc : true
---

翻译自SRv6 Network Programming
draft-filsfils-spring-srv6-network-programming-07

### SRH 

Segment Routing Header

SRH在一个报文中可以有多个

### NH

ipv6 next-header field

Srv6的Routing Header的type是4，IP6 header的NH字段是43

### SID

编排链节点的ID，srv6节点的SID table里面保存自己在各个编排链内的SID。local SID可以是设备外部接口（不会是内部接口）的ipv6地址。例如已经在外部接口配置了地址A和地址B，内部loopback配置了地址C。地址A和地址B会默认被加入到SID Table。

地址B可以是路由不可达的，为什么？

可以将地址A理解成全局segments，地址B为本地segments。只要报文在发送时加入了SID list<A,B>，A在B的前面，只要A对外路由可达，报文就会被送到A，然后在本地进行下一步的处理（发往本地的B）

(SA,DA) (S3, S2, S1; SL)

S1是第一跳，S3是最后一跳。SL剩下几跳，也可理解为下一个SID节点的下标。例如SL=0， 表示SRH[0]=S3，下一个SID处理节点的ip地址是S3

### SID格式

SID Table中并不是以Ip的形式保存SID的

LOC:FUNCT:ARGS::

### function

每个SID可以绑定多个function。function与SID的绑定关系存在SID Table中。这个特性决定了SRV6的高度可编程性。

function太多，不一一列出，总结下规律

带有D的，表示Decapsulation，如果SL==0（已经是最后一跳）且NH!=SRH(没有嵌套另一个SRH)，且SRH的ENH（下一层header类别）符合function的定义(例如DT6，ENH必须是41(ipv6 encapsulation))，则剥去SRH

带有T的，表示table，查对应的fib表

带有X的，表示cross-connect，往邻接表对应的Ip地址发（直接拿mac）

带V的，表示Vlan，往对应Vlan发（改Vlan头部）

带B的，表示bond，insert在老的SRH和Ipv6 Header之间新插入一个SRH，将DA改为新的SRH的第一个segment；encap则是在最外面新插入一个ipv6头部，新ipv6头部SA是内部ipv6头部的SA，DA是新ipv6头部下的SRH的第一跳

