---
title: "dpvs icmp session"
date: 2025-02-18T11:11:00+08:00
draft: false
tags: ["dpdk","dpvs","高性能网络"]
tags_weight: 66
series: ["dpvs系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
---


# dpvs icmp session



原生的ipvs仅处理三种类型的ICMP报文：ICMP_DEST_UNREACH、ICMP_SOURCE_QUENCH和ICMP_TIME_EXCEEDED

对于不是这三种类型的ICMP，则设置为不相关联(related)的ICMP，返回NF_ACCEPT，之后走本机路由流程

dpvs对ipvs进行了一些修改，修改后逻辑如下



## icmp差错报文流程



- __dp_vs_in

- __dp_vs_in_icmp4 （处理icmp差错报文，入参related表示找到了关联的conn）

    若不是ICMP_DEST_UNREACH，ICMP_SOURCE_QUENCH，ICMP_TIME_EXCEEDED，返回到_dp_vs_in走普通conn命中流程

  icmp差错报文，需要将报文头偏移到icmp头内部的ip头，**根据内部ip头查找内部ip的conn**。

  若找到conn，**表明此ICMP报文是由之前客户端的请求报文所触发的，由真实服务器回复的ICMP报文**。将related置1

   若未找到则返回accept，返回到_dp_vs_in走普通conn命中流程
  - ​    __xmit_inbound_icmp4

  ​    找net和local路由，之后走__dp_vs_xmit_icmp4

  -  __dp_vs_xmit_icmp4

  ​      数据区的前8个字节恰好覆盖了TCP报文或UDP报文中的端口号字段（前四个字节）

  inbound方向根据内部ip的conn修改数据区目的端口为conn->dport，源端口改为conn->localport，

  outbound方向将目的端口改为conn->cport，源端口改为conn->vport

  ​       

  client (cport )   <-->   (vport)lb(lport)   <-->    rs(dport)

  ​      重新计算icmp头的checksum，走ipv4_output



![报文格式](https://i-blog.csdnimg.cn/blog_migrate/4687e28af632425a7d0f6d66487d954b.png#pic_center)

**实际应用上的问题**

某个rs突然下线，导致有时访问vip轮询到了不可达的rs，rs侧的网关发送了一个dest_unreach的icmp包

该rs的conn还未老化，__dp_vs_in_icmp4流程根据这个icmp的内部差错ip头找到了还未老化的conn，将icmp数据区的port进行修改发回给client

但是一般情况，rs下线后，该rs的conn会老化消失，内层conn未命中，还是走外层icmp的conn命中流程转给client。这样内部数据区的端口信息是错的（dport->lport，正确情况是vport->cport）



## 非差错报文流程

返回_dp_vs_in走普通conn命中流程



原本dp_vs_conn_new流程中，先查找svc。icmp的svc默认使用端口0进行查找。但是ipvsadm命令却对端口0的service添加做了限制，导致无法添加这类svc。

```c
 svc = dp_vs_service_lookup(iph->af, iph->proto, &iph->daddr, 0, 0,                               mbuf, NULL, &outwall, rte_lcore_id());
```

若未查到走INET_ACCEPT(也就是继续往下进行走到ipv4_output_fin2查到local路由，若使用dpip addr配上了vip或lip地址，则会触发本地代答)。

若查到svc，则进行conn的schedule，之后会走dp_vs_laddr_bind，但是dp_vs_laddr_bind不支持icmp协议(可以整改)，最终导致svc可以查到但是conn无法建立，最后走INET_DROP。

概括一下：

- - 未命中svc，走后续local route，最终本地代答
  - 命中svc后若conn无法建立，drop
  - 命中svc且建立conn，发往rs或client



### icmp的conn



​        

```c
_ports[0] = icmp4_id(ich);
_ports[1] = ich->type << 8 | ich->code;
```



Inbound hash和outboundhash的五元组都使用上述这两个port进行哈希，并与conn进行关联。

具体的laddr和lport保存在conn里面。其中只用到laddr做l3的fullnat。由于icmp协议没有定义proto->fnat_in_handler，因此fnat时，从sa_pool分配到的lport对于icmp来说没有用。



试想ping request和ping reply场景：

由于request和reply的ich->type不一样，outboundhash必定不命中(且fnat流程中的laddr_bind还会修改一次outboundhashtuple的dport，修改成sapool分配的port，因此也不会命中outbound hash)。

**一次来回的ping会创建两个conn，且都只命中inboundhash。**



## 个人认为比较合理的方案



#### ipvsadm

放通port=0的svc的创建，用户需要fwd icmp to rs时，需要添加icmp类型的svc。否则icmp会被vip或者lip代答，不会透传到rs或client



#### icmp差错报文

保持原状，对找不到关联的conn连接的差错报文进行drop。

但一般情况下若发生差错，关联的conn大概率已经老化，此时做related的处理意义不大



#### icmp查询报文

保持原状，建立icmp conn，来回创建两个conn。

