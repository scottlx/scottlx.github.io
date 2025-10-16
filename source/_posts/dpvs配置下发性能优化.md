---
title: "dpvs配置下发性能优化"
date: 2025-10-16T10:29:00+08:00
draft: false
tags: ["dpdk", "dpvs", "高性能网络"]
tags_weight: 66
series: ["dpvs系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
---

记录一次dpvs配置下发性能优化

<!-- more -->

### 问题描述

控制面批量创建或删除监听器时，控制器消费队列积攒且消费速度很慢，影响业务下发

### 架构

![frame](/img/dpvs/lb整体框架.png)

### 问题总结

整体打点，pprof，perf测试下来，发现各个组件都存在或多或少的性能问题，列举如下

**dpvs**

1. dpvs的监听器接口比较臃肿，是包含监控数据的，agent dump dpvs的数据做差分时，会get到大量无用的监控数据，占用了大量的I/O

**agent**

1. agent缓存的差分代码中，存在encode/decode，涉及一些反射，影响性能
2. agent下发增量业务配置时，为了保证强一致性，会dump所有南向dpvs的配置数据。当业务配置越来越多时，每次dump的速度会越来越慢。
3. monitor每隔30s会通过socket全量dump dpvs的监控数据，会占用大量的I/O，影响业务表项的下发

**controller**

1. tcc事务需要同步记录到etcd，而etcd的事务，要等raft至少2个节点提交才能结束。
2. controller没有开启并发
3. controller开启并发后，client-go请求api-server存在限流，需要调高qps和burst
4. 一些业务逻辑涉及client-go List，而List会将informer cache里的对象都进行一次deepcopy到栈上的内存。这就导致业务增长后会进行大量的deepcopy而影响性能



### 解决方案

**dpvs**

1. 监控业务分离：新增svc，dest dump接口，数据通过内存文件上送monitor

![monitor](/img/dpvs/监控业务分离.png)

2. 调高socket缓冲区大小

**agent**

1. 优化差分逻辑：只有初始化时全量同步南向dpvs的数据。增量下发时，只针对agent缓存里的数据做差分

**controller**

1. tcc事务由etcd存储改为redis存储
2. client-go List对于一些只读操作，添加UnsafeDisableDeepCopy选项，避免无意义的deepcopy
3. 开启并发，适当调高api-server限流的qps和burst

