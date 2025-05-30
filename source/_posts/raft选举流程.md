---
title: "raft选举流程"
date: 2022-10-28T09:30:00+08:00
draft: false
tags: ["go","etcd","raft"]
tags_weight: 66
series: ["etcd系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
---



### 图解

[Raft (thesecretlivesofdata.com)](http://thesecretlivesofdata.com/raft/)

算法目的：实现了分布式节点的数据一致性

 节点有三个状态：follower，candidate，leader

 

### leader election

初始阶段所有节点处于follower状态

follower状态下节点存在一个election timeout（150ms—300ms之间的随机数，随机降低了多个节点同时升级为candidate的可能性），election timeout内没有收到leader的heartbeat后，会自动升级为candidate状态，并开始一个新的election term。term是全局的，表示整个集群发生过选举的轮次(任期)。

candidate状态下，节点会向集群内所有节点发送requests votes请求。其他节点收到requests votes请求后，如果在本次term内还没有投过票，则会返回选票，如果candidate收到的选票占集群节点的大多数，则升级为本次term的leader节点。升级为leader之后向他的follower 发送append entries消息（也就是包含entry消息的心跳），follower也会返回消息的response，系统正常情况下维持在该状态

 如果选举时，在一个term内发生了两个节点有同样的选票，会在超时过后进入下一轮进行重新选举

### log replication

client的请求只会发往leader。leader收到改动后，将改动写入日志（还未持久化commit），并将改动通过heartbeat广播至follower节点。follower节点写了entry之后（此时还未commit），返回ack。leader收到大于集群节点一半的ack之后，认为已经可以commit了，广播commit的通知。最终集群内所有follower触发commit，向leader返回ack。最后leader认为集群已经达成一致性了，向client返回ack

 如果集群中产生网络隔离，每个隔离域中会产生一个新的leader，整个集群会存在多个leader。follower少的leader由于获取不到majority ack，他的entry不会被commit。此时client往另一个follower多的leader发送数据改变请求，该隔离域的节点会被commit

 此时去掉网络隔离后，之前follower少的隔离域内未commit的entry会被刷成之前follower多的隔离域的entry,随后commit，此时集群再次达成一致性
