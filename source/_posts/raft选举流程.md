---
title: "raft选举流程"
date: 2022-10-28T09:30:00+08:00
draft: false
tags: ["go", "etcd", "raft"]
tags_weight: 66
series: ["etcd系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
---

<!-- more -->

### 图解

[Raft (thesecretlivesofdata.com)](http://thesecretlivesofdata.com/raft/)

算法目的：实现了分布式节点的数据一致性

节点有三个状态：follower，candidate，leader

### leader election

初始阶段所有节点处于 follower 状态

follower 状态下节点存在一个 election timeout（150ms—300ms 之间的随机数，随机降低了多个节点同时升级为 candidate 的可能性），election timeout 内没有收到 leader 的 heartbeat 后，会自动升级为 candidate 状态，并开始一个新的 election term。term 是全局的，表示整个集群发生过选举的轮次(任期)。

candidate 状态下，节点会向集群内所有节点发送 requests votes 请求。其他节点收到 requests votes 请求后，如果在本次 term 内还没有投过票，则会返回选票，如果 candidate 收到的选票占集群节点的大多数，则升级为本次 term 的 leader 节点。升级为 leader 之后向他的 follower 发送 append entries 消息（也就是包含 entry 消息的心跳），follower 也会返回消息的 response，系统正常情况下维持在该状态

如果选举时，在一个 term 内发生了两个节点有同样的选票，会在超时过后进入下一轮进行重新选举

### log replication

client 的请求只会发往 leader。leader 收到改动后，将改动写入日志（还未持久化 commit），并将改动通过 heartbeat 广播至 follower 节点。follower 节点写了 entry 之后（此时还未 commit），返回 ack。leader 收到大于集群节点一半的 ack 之后，认为已经可以 commit 了，广播 commit 的通知。最终集群内所有 follower 触发 commit，向 leader 返回 ack。最后 leader 认为集群已经达成一致性了，向 client 返回 ack

如果集群中产生网络隔离，每个隔离域中会产生一个新的 leader，整个集群会存在多个 leader。follower 少的 leader 由于获取不到 majority ack，他的 entry 不会被 commit。此时 client 往另一个 follower 多的 leader 发送数据改变请求，该隔离域的节点会被 commit

此时去掉网络隔离后，之前 follower 少的隔离域内未 commit 的 entry 会被刷成之前 follower 多的隔离域的 entry,随后 commit，此时集群再次达成一致性
