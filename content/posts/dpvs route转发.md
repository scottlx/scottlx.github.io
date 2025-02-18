---
title: "dpvs route转发"
date: 2025-2-18T11:11:00+08:00
draft: false
tags: ["dpdk","dpvs","高性能网络"]
tags_weight: 66
series: ["dpvs系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
---


# dpvs route转发

ipv4_rcv_fin 是路由转发逻辑， INET_HOOKPRE_ROUTING 中走完hook逻辑后，根据dpvs返回值走ipv4_rcv_fin 


## 路由表结构体

```c
struct route_entry {
    uint8_t netmask;
    short metric;
    uint32_t flag;
    unsigned long mtu;
    struct list_head list;
    struct in_addr dest; //cf->dst.in
    struct in_addr gw;// 下一跳地址，0说明是直连路由，下一跳地址就是报文自己的目的地址，对应配置的cf->via.in
    struct in_addr src; // cf->src.in， 源地址策略路由匹配
    struct netif_port *port;  // 出接口
    rte_atomic32_t refcnt;
};

```

## 路由类型

```c
/* dpvs defined. */
#define RTF_FORWARD     0x0400
#define RTF_LOCALIN     0x0800
#define RTF_DEFAULT     0x1000
#define RTF_KNI         0X2000
#define RTF_OUTWALL     0x4000
```

## 路由表类型

```c
#define this_route_lcore        (RTE_PER_LCORE(route_lcore))
#define this_local_route_table  (this_route_lcore.local_route_table)
#define this_net_route_table    (this_route_lcore.net_route_table)
#define this_gfw_route_table    (this_route_lcore.gfw_route_table)
#define this_num_routes         (RTE_PER_LCORE(num_routes))
#define this_num_out_routes      (RTE_PER_LCORE(num_out_routes))
```



- Local 类型路由

local 类型路由的作用和 Linux 下的 local 路由表的功能基本一样，主要是记录本地的 IP 地址。
我们知道进入的数据包，过了 prerouting 后是需要经过路由查找，如果确定是本地路由（本地 IP）就会进入 LocalIn 位置，否则丢弃或进入 Forward。

Local 类型路由就是用来判定接收数据包是否是本机 IP，在 DPVS 调用的就是 route4_input 函数

- Net 类型路由

数据包经过应用程序处理完毕后，要执行 output 时也需要根据目的 IP 来查找路由，确定从哪个网卡出去，下一跳等信息，然后执行后续操作。

在 DPVS 调用的就是 route4_output 函数。

使用dpip工具添加ip地址时，同时也会添加一条local路由和net路由

- gfw table

是新特性，可以理解成是另外一张路由表，用来做策略路由用的。如果conn的flag有outwall，则走这个表转发。一般不用这个表。

[What is ipset and outwall route in DPVS? · Issue #564 · iqiyi/dpvs (github.com)](https://github.com/iqiyi/dpvs/issues/564)


## 路由的添加

*dpip route add*
```c
static int route4_do_cmd(struct dpip_obj *obj, dpip_cmd_t cmd,
                        struct dpip_conf *conf)
```

命令行添加时，是根据scope来确定路由的类型的

ROUTE_CF_SCOPE_HOST  -- >RTF_LOCALIN

ROUTE_CF_SCOPE_KNI  --> RTF_KNI

Cf->utwalltb --> RTF_OUTWALL

其他  -- >  RTF_FORWARD

## 转发逻辑
**ipv4_rcv_fin** 是路由转发逻辑

如果找到的route 有forward的flag：
**rt->flag & RTF_FORWARD**
则走转发逻辑 *ipv4_forward  --> ipv4_output_fin2*
这个时候，路由表项rt已经带在mbuf的userdata里面了，后面的处理函数都能拿到这个包的路由

Rt->gw 表示网关地址，如果没有网关，这个值是0，说明是直连路由，直接往报文的目的ip （ip4_hdr(mbuf)->dst_addr）发送 ；

 否则，往这个网关发送