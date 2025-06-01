---
title: "dpvs session 同步"
date: 2025-02-18T11:11:00+08:00
draft: false
tags: ["dpdk", "dpvs", "高性能网络"]
tags_weight: 66
series: ["dpvs系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
---

LB session 同步思路

<!-- more -->

## 总体架构

#### lb session 同步采用分布式架构，session 创建流程触发 session 数据发送，依次向集群内所有其他节点发送

其他节点收到新的 session 数据修改本地 session 表。
session 接收和发送各占一个独立线程。

### step1: 向所有其他节点发送 session 数据

remote session：从别的节点同步来的 session

local session：本节点收到数据包自己生成的 session

### step2: session 同步至 worker

#### 方案一（有锁）：

![Alt text](/img/dpvs/session同步有锁.png)

• 独立进程和 core 处理 session 同步（per numa）
• 每个 lcore 分配 local session 和 remote session，正常情况下都能直接从 local session 走掉
• 同步过来的 session 写到 remote session 表
• session ip 根据 fdir 走到指定进程

#### 方案二（无锁）：

![Alt text](/img/dpvs/session同步无锁.png)
• 独立进程和 core 处理 session 同步消息
• 每个 lcore 有来 local session 和 remote session，通过 owner 属性区分。
• 同步过来的 session 由 session_sync core 发消息给对应的 slave，由对应的 slave 进行读写，因此可以做到无锁。
• session ip 根据 fdir 走到指定 core

#### session 同步具体实现

##### 亟待解决的问题：

1. 同步过来的 session 什么时候老化？

2. 别的节点上线，本节点要发送哪些 session？

3. 别的节点下线，本节点要删除哪些 session？

4. 是否要响应下线节点的删除/老化请求？

5. 下线节点怎么知道自己已经下线（数据面）？

##### 解决方案：

###### 方案一： session 增加 owner 属性 c

owner 属性：

```c
conn.owner // indicates who has this session
```

session 同步状态转移图

![Alt text](/img/dpvs/session同步状态转移.png)

一条 session 在一个集群中，应当只有一台机器在使用，所以有一个 owner 属性，代表这条 session 被谁拥有，其它所有机器只对这条 session 的 owner 发起的增删改查请求做响应。

**同步动作**
session 同步应当时实时的。在以下场景被触发：
**新建 session**
发送方：
session 新建完成之后：对于 tcp，是握手完毕的；对于 udp，是第一条连接。
接收方：
接收来自发送方的 session，在对应 core 上新建这条连接，开启老化，老化时间设定为默认时间（1 小时）。
fin/rst
发送方：
发送删除 session 消息
接收方：
接收方接收 session，做完校验后在对应 core 上删除 session
**老化**
发送方：
老化时间超时之后，本地 session 删除，同时发布老化信息，告知其它 lb，
接收方：
其它 lb 做完校验后，开始老化这条 session。
**设备下线**
下线后通过控制器更新其他 lb 的 session 同步地址信息，不再向该设备同步，同时开始老化全部属于该设备的 session。
**设备上线（包含设备扩容）**
新设备：
新上线设备引流前要接收其他设备的存量 session 信息，这个功能通过控制器触发完成，控制器感知到新 lb 上线后通知集群内其他 lb 向它同步存量 session，session 数量达到一致时（阿里 gw 用 70%阈值）允许新 lb 引流。
旧设备：
向目的方发送全部的属于自己的 session。

###### 方案二： 使用 session 的 lb 广播信息

**同步时机：**
**active：**
发送方：当新建了 session 或者有报文命中时，就发送 active 的 session，表明这条 session 还存在；
接收方：根据情况新建或者 refresh session 定时器；
**aged/deleted:**
不用发送任何信息，其它 lb 到期自然老化；
**节点上线：**
发送方：发送节点上所有的 session；
接收方：自行对重复的 session 进行判断；
**节点下线：**
其余节点无需做任何操作
session 同步目的地址选择

###### 方案三：控制器分配 session syn ip

session syn ip（sip）作为一种资源统一有控制器管理和分配，当集群有设备上线时，ip 分配过程如下：

1. lb 全量结束后向控制器请求 session syn ip；
2. 控制器下发 session syn ip 给新上线设备（通过管理口 ip 过滤），lb 设备宣告路由；
3. lb 向控制器请求 session 同步；
4. 控制器向集群存量设备通知新加入的 session syn ip，并触发存量 session 同步。
   当 lb 下线时，触发 SDN Controller 下发 lip 更新事件，通知集群内每个 lb 更新现存 session syn ip 数据。

![Alt text](/img/dpvs/session同步控制器.png)

## fdir 设计

session 同步报文采用 underlay 实现，因为不要求被同步者确认应答，所以用 UDP 协议实现较为合适。
![Alt text](/img/dpvs/session同步报文格式.png)
当 lb1 向 lb2 同步 session 时，src ip=sip1，dst ip=sip2，对应地，session 同步报文的 fdir 规则就用 sip 作为匹配项，即 lb2 网卡收到目的地址为 sip2 的报文后 fdir 会直接将这个报文送到 lb2 的 sessio 同步接收队列上，由对应的核处理，从而实现与转发业务隔离。

![Alt text](/img/dpvs/session同步fdir.png)

### 同步报文设计

#### 同步节奏&定时

需要批量发布 session 同步数据，报文最大长度设为网络 mtu，到最大长度后封下一个报文。
同步没有周期，只要有 session 变动就发送报文。

#### 批量& 报文设计

1. 探测报文： 可以复用其它探测类方法
2. session 传输报文格式
   总体格式： underlay 报文 eth + ip + udp + session_data
   eth 和 ip 是实际目的地址,在这个地方，是 sip。fdir 规则需要提前定义，将 sip 导入指定 worker 中
   session_data: 采用 tlv 格式，具体如下图。采用小端数据格式。

![Alt text](/img/dpvs/session同步报文协议.png) 3. session 同步 max 带宽估计 : 带宽使用最大的场景为设备上线，此时会有多台设备给同一台下，总共需要同步 20M 条 session
eth*hdr + ip_hdr + udp_hdr + FCS = 48 bytes 如果 session 同步时的 af 全都是 ipv6，那么 session_data 的 size 为 80 bytes
mtu = 1500： 一次最多同步 18 条 session，总数据量为 20M / 18 * 1.5k=1.7G
mtu = 8000: 一次最多同步 99 条 session， 总数据量为 20M / 99 \_ 8k = 1.1G
若在 1s 内完成全部同步，1.7GBps\*8 = 13.6Gbps 左右，未到网卡上限，总体风险可以控制。
