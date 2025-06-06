---
title: "redis 多节点"
date: 2022-10-03T15:00:00+08:00
draft: false
tags: ["cache", "redis", "中间件"]
tags_weight: 66
series: ["redis系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
---

redis 多节点

<!-- more -->

# 主从（复制）

## 同步

slave 刚上线或断线重连时的第一次全量同步

slave 的客户端主动发送 sync 命令，触发 master 的 BGSAVE，BGSAVE 过程中将命令存入缓冲区，BGSAVE 完成后发送 RDB 文件，slave 完成 RDB 载入后再发送缓冲区的指令

## 命令传播

完成同步后的增量同步

master 主动发送命令

## psync

优化后的 sync，作为断线重连后的增量同步
slave 发送 psync 命令，master 返回+continue，之后发送断开期间执行的写命令

### 偏移量

主节点和从节点各自维护一个偏移量，表示当前已接收数据的字节数。当从节点发现自身偏移量与主节点不一致时，主动向主节点发送 psync 命令

### 复制缓冲区

主节点进行命令传播时(增量同步),会将写命令复制一份到缓冲区。且每个写命令都绑定一个对应的偏移量。从节点发送的 psync 中带有偏移量，
通过该偏移量在复制缓冲区中查找偏移量之后的写命令。如果查不到，则执行完整同步(sync)

### 服务器运行 ID

从节点向主节点注册自己的分布式 ID，新上线的从节点若不在注册表内，则进行完整同步(sync)，否则进行部分重同步。

## 同步过程

slaveof 命令设置 redisServer 中的 masterhost 和 masterport 字段，之后主从连接由 cron 定时器任务里触发

- anetTcpConnect 建立一个新的 tcp 连接
- ping-pong 命令测试连接
- auth 鉴权
- 发送端口号，主节点刷新 client 信息
- psyn/sync 同步
- 命令传播
- 心跳 发送 REPLCONF ACK 命令，其中带有从节点的偏移量，可以检测命令丢失（命令丢失后主节点和从节点的偏移量会不一样）；收到心跳的时间戳用来监测网络延迟状态，若一定数量的从服务器的 lag 超过一定值，表示该主从集合不健康，不允许 client 写入

### 一致性

不是强一致性（cap），是最终一致性。用最终一致性换取了高吞吐量

- master 与 slave 的同步存在数据不一致的时间窗口期
- 网络分区后哨兵模式或者集群模式的选主会产生脑裂

# 哨兵

多了一个哨兵节点进行主节点选举，触发从同步等工作，数据的同步还是主从模式
哨兵节点运行的是一个特殊模式的 redis 服务器，里面没有数据库。

### 连接类型

命令连接

订阅连接

```c
/*实例不是 Sentinel （主服务器或者从服务器）
并且以下条件的其中一个成立：
    1）SENTINEL 未收到过这个服务器的 INFO 命令回复
    2）距离上一次该实例回复 INFO 命令已经超过 info_period 间隔
   那么向实例发送 INFO 命令
*/
  if ((ri->flags & SRI_SENTINEL) == 0 &&
​    (ri->info_refresh == 0 ||
​    (now - ri->info_refresh) > info_period))
  {
​    /* Send INFO to masters and slaves, not sentinels. */
​    retval = redisAsyncCommand(ri->cc,
​      sentinelInfoReplyCallback, NULL, "INFO");
​    if (retval == REDIS_OK) ri->pending_commands++;
  } else if ((now - ri->last_pong_time) > ping_period) {

​    /* Send PING to all the three kinds of instances. */
​    sentinelSendPing(ri);
  } else if ((now - ri->last_pub_time) > SENTINEL_PUBLISH_PERIOD) {
​    /* PUBLISH hello messages to all the three kinds of instances. */
​    sentinelSendHello(ri);
  }
```

### 节点的连接

哨兵主定时任务开始时，以 1s 或 10s 的间隔发送 INFO 命令，得到主节点的回复，并处理 INFO 回复的信息。回复中包含该主节点的从节点 ip+port。处理函数中更新主节点的实例 sentinelRedisInstance，并创建从节点，进行从节点的连接，获取从节点的详细信息。

连接完成后，哨兵每隔 2s 向所有节点(主和从)的命令连接发送 hello 格式消息，消息中包含接收节点对应的主节点的信息（若接收节点是主节点，则是它自己的信息）。同时在订阅连接中接收 hello 的回复信息

往订阅连接发送的回复信息会备所有监视该节点的哨兵接收到，接收到后哨兵更新自己的 sentinel 字典和 masters 字典。因此监视相同的节点的 sentinel 之间可以互相发现，但互相发现后的 sentinel 之间只会创建命令连接不会创订阅连接

### 下线状态

主观下线

哨兵主定时任务会向所有节点（主，从，哨兵）发送 ping 命令，若在主观下线时常结束后没有收到有效回复（+PONG，-LOADING，-MASTERDOWN），则主观认为该节点下线（SDOWN）

客观下线

向同样监视该节点的哨兵询问（发送 SENTINEL is-master-down-by-addr 命令并接收回复），接收到一定数量的下线判断后(主观客观都算)，则将该节点置为客观下线状态(ODOWN)，并进行故障转移操作

不同 sentinel 对客观下线的判断标准会不同，由初始配置决定

### 领头选举

主节点客观下线后，所有监听该主节点的 sentinel 将进行领头选举。之后领头对该下线的 master 进行故障转移操作。成为领头的条件是自己被半数以上的 sentinel 设置成局部领头。选举开始时所有节点进行广播 SENTINEL is-master-down-by-addr 命令，接受到该广播命令的 sentinel 只对第一个命令进行回复，后续的命令直接 drop（类似发起 dhcp 请求后对 dhcp 回复报文的处理）。收到回复后 sentinel 在本次 epoch(一个选举次数计数器，记录了选举的轮次，类似 etcd 的 election term)将自己的局部领头数加一。其实就是 raft 算法里的 leader 选举机制，类似 etcd 的实现，因此哨兵的个数不能是偶数（1 个是不是也可以？）

### 故障转移

1. 在已下线的主服务器的从服务器中选出一台作为新的主服务器

   依据从服务器数据的新旧状态，优先级，复制偏移量以及运行 ID 综合选出新的主服务器，发送 SLAVEOF no one 命令切主，并用 INFO 命令检查是否已经切主

2. 让已下线的主服务器的从服务器改为复制这台新的主服务器

   依次对所有从服务器发送 SLAVEOF 命令

3. 将这台下线的主服务器作为新的主服务器的从服务器，重新上线后就成为了从服务器

   因为已经下线，所以发布了命令，改为在该 server 的实例结构里保存设置，重新上线后发送 SLAVEOF 命令

### 存在的问题

#### 主节点脑裂

若存在网络分区，会生成两个主节点。此时若 client 往其中小分区的主节点写数据，当网络恢复之后，该主节点降级回从节点，会丢失所有网络分区后写入的数据。可以采用 min-replica 选项限制 client 的写入（要求写入的主节点必须有一个从节点）
