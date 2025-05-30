---
title: "dpvs session 同步"
date: 2025-02-18T11:11:00+08:00
draft: false
tags: ["dpdk","dpvs","高性能网络"]
tags_weight: 66
series: ["dpvs系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
---

## 总体架构

#### lb session同步采用分布式架构，session创建流程触发session数据发送，依次向集群内所有其他节点发送

其他节点收到新的session数据修改本地session表。
session接收和发送各占一个独立线程。

### step1: 向所有其他节点发送session数据

remote session：从别的节点同步来的session

local session：本节点收到数据包自己生成的session

### step2: session同步至worker

#### 方案一（有锁）： 
![Alt text](/img/dpvs/session同步有锁.png)

•	独立进程和core处理session同步（per numa）
•	每个lcore分配local session和remote session，正常情况下都能直接从local session走掉
•	同步过来的session写到remote session表
•	session ip根据fdir走到指定进程

#### 方案二（无锁）： 
![Alt text](/img/dpvs/session同步无锁.png)
•	独立进程和core处理session同步消息
•	每个lcore 有来local session和remote session，通过owner属性区分。
•	同步过来的session由session_sync core发消息给对应的slave，由对应的slave进行读写，因此可以做到无锁。
•	session ip根据fdir走到指定core

#### session同步具体实现 

##### 亟待解决的问题：

1. 同步过来的session什么时候老化？

2. 别的节点上线，本节点要发送哪些session？

3. 别的节点下线，本节点要删除哪些session？

4. 是否要响应下线节点的删除/老化请求？

5. 下线节点怎么知道自己已经下线（数据面）？

  

##### 解决方案：

###### 方案一： session增加owner属性c

owner属性：

```c
conn.owner // indicates who has this session
```

session 同步状态转移图

![Alt text](/img/dpvs/session同步状态转移.png)

一条session在一个集群中，应当只有一台机器在使用，所以有一个owner属性，代表这条session被谁拥有，其它所有机器只对这条session的owner发起的增删改查请求做响应。

**同步动作**
session同步应当时实时的。在以下场景被触发：
**新建session**
发送方：
    session新建完成之后：对于tcp，是握手完毕的；对于udp，是第一条连接。
接收方：
   接收来自发送方的session，在对应core上新建这条连接，开启老化，老化时间设定为默认时间（1小时）。
fin/rst
发送方：
    发送删除session消息
接收方：
   接收方接收session，做完校验后在对应core上删除session
**老化**
发送方：
    老化时间超时之后，本地session删除，同时发布老化信息，告知其它lb，
接收方：
    其它lb 做完校验后，开始老化这条session。
**设备下线**
下线后通过控制器更新其他lb的session同步地址信息，不再向该设备同步，同时开始老化全部属于该设备的session。
**设备上线（包含设备扩容）**
新设备：
    新上线设备引流前要接收其他设备的存量session信息，这个功能通过控制器触发完成，控制器感知到新lb上线后通知集群内其他lb向它同步存量session，session数量达到一致时（阿里gw用70%阈值）允许新lb引流。
旧设备：
   向目的方发送全部的属于自己的session。

###### 方案二： 使用session的lb广播信息

**同步时机：**
**active：**
   发送方：当新建了session或者有报文命中时，就发送active的session，表明这条session还存在；
   接收方：根据情况新建或者refresh session 定时器；
**aged/deleted:**
   不用发送任何信息，其它lb到期自然老化；
**节点上线：**
  发送方：发送节点上所有的session；
  接收方：自行对重复的session进行判断；
**节点下线：**
  其余节点无需做任何操作
session同步目的地址选择

###### 方案三：控制器分配session syn ip

session syn ip（sip）作为一种资源统一有控制器管理和分配，当集群有设备上线时，ip分配过程如下：

1. lb全量结束后向控制器请求session syn ip；
2. 控制器下发session syn ip给新上线设备（通过管理口ip过滤），lb设备宣告路由；
3. lb向控制器请求session同步；
4. 控制器向集群存量设备通知新加入的session syn ip，并触发存量session同步。
   当lb下线时，触发SDN Controller下发lip更新事件，通知集群内每个lb更新现存session syn ip数据。

![Alt text](/img/dpvs/session同步控制器.png)

## fdir设计

session同步报文采用underlay实现，因为不要求被同步者确认应答，所以用UDP协议实现较为合适。
![Alt text](/img/dpvs/session同步报文格式.png)
当lb1向lb2同步session时，src ip=sip1，dst ip=sip2，对应地，session同步报文的fdir规则就用sip作为匹配项，即lb2网卡收到目的地址为sip2的报文后fdir会直接将这个报文送到lb2的sessio同步接收队列上，由对应的核处理，从而实现与转发业务隔离。

![Alt text](/img/dpvs/session同步fdir.png)
### 同步报文设计

#### 同步节奏&定时

需要批量发布session同步数据，报文最大长度设为网络mtu，到最大长度后封下一个报文。
同步没有周期，只要有session变动就发送报文。

#### 批量& 报文设计

1.	探测报文： 可以复用其它探测类方法
2.	session传输报文格式
总体格式： underlay 报文 eth + ip + udp + session_data
eth和ip是实际目的地址,在这个地方，是sip。fdir规则需要提前定义，将sip导入指定worker中
session_data: 采用tlv格式，具体如下图。采用小端数据格式。 

![Alt text](/img/dpvs/session同步报文协议.png)
3.	session同步max带宽估计 : 带宽使用最大的场景为设备上线，此时会有多台设备给同一台下，总共需要同步20M条session
eth_hdr + ip_hdr + udp_hdr + FCS = 48 bytes 如果session同步时的af全都是ipv6，那么session_data的size为 80 bytes
mtu = 1500： 一次最多同步18条session，总数据量为 20M / 18 * 1.5k=1.7G
mtu = 8000: 一次最多同步99条session， 总数据量为 20M / 99 * 8k = 1.1G
若在1s内完成全部同步，1.7GBps*8 = 13.6Gbps 左右，未到网卡上限，总体风险可以控制。