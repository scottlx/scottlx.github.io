---
title: "contiv memif"
date: 2023-04-07T15:10:00+08:00
draft: false
tags: ["vpp", "vpp-agent", "k8s", "contiv"]
tags_weight: 66
series: ["vpp系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
---

<!-- more -->

### contiv memif

contiv 的 cni 与 device plugin 相结合，实现了：

1. Pod 能同时接入不止一张网卡
2. Pod 接入的网卡可以是 tap，veth，memif

#### devicePlugin

Device Plugin 实际是一个运行在 Kubelet 所在的 Node 上的 gRPC server，通过 Unix Socket、基于以下（简化的）API 来和 Kubelet 的 gRPC server 通信，并维护对应设备资源在当前 Node 上的注册、发现、分配、卸载。
其中，`ListAndWatch()`负责对应设备资源的 discovery 和 watch；`Allocate()`负责设备资源的分配。

![6F9684EB-7E5E-4b77-9316-32D5C92FD07E](/img/vpp-agent/6F9684EB-7E5E-4b77-9316-32D5C92FD07E.png)

#### Insight

![contiveCNI.drawio](/img/vpp-agent/contiveCNI.drawio.png)

##### kubelet

kubelet 接收上图格式的 API。API 中的 annotations 定义了 pod 的网卡个数与类型，resources 中定义了所需要的 device plugin 的资源，也就是 memif。

kubelet 执行常规的 syncPod 流程，调用 contiv cni 创建网络。此时会在请求中将 annotation 传递给 cni。

同时，agent 的 DevicePluginServer 会向 kubelet 注册 rpc 服务，注册 contivpp.io/memif 的设备资源，从而 kubelet 的 device manager 会 grpc 请求 DevicePluginServer 获取 contivpp.io/memif 设备资源。

##### cni

cni 实现了 github.com/containernetworking/cni 标准的 add 和 del 接口。实际上做的事情只是将 cni 请求转换为了对 agent 的 grpc 请求：解析 args，并通过 grpc 调用 agent 的接口发送 cniRequest，再根据 grpc 的返回结果，将结果再次转换成标准 cni 接口的返回格式

##### Agent

###### podmanager

podmanager 实现了上述 cni 调用的 grpc server，主要任务是将 cni 的 request 转换为内部的 event 数据格式，供 event loop 处理。

request 是 cni 定义的请求数据类型，详见https://github.com/containernetworking/cni/blob/master/SPEC.md#parameters

event 则是 agent 内部的关于 pod 事务模型，类似原生 kvScheduler 的针对 vpp api 的 transaction。每一种 event 都会对应一个 plugin 去实现他的 handler，供 event loop 调用。

###### event loop

event loop 是整个 contiv agent 的核心处理逻辑，北向对接 event queue，南向调用各个 EventHandler，将 event 转换为 kvScheduler 的事务。

执行了以下步骤：

1. 对事件的预处理，包括校验，判断事件类型，加载必要的配置等
2. 判断是否是更新的事件
3. 对事件的 handler 进行排序，并生成正向或回退的 handler 顺序
4. 与本次事件无关的 handler 过滤掉
5. 创建对这次事件的记录 record
6. 打印上述步骤生成的所有事件相关信息
7. 执行事件更新或同步，生成 vpp-agent 里的事务
8. 将 contiv 生成的配置与外部配置进行 merge，得到最终配置
9. 将最终配置的 vpp-agent 事务 commit 到 agent 的 kvscheduler
10. 若事务失败，将已经完成的操作进行回退
11. 完成事件，输出记录 record 与计时
12. 打印回退失败等不可恢复的异常
13. 若开启一致性检查，则最好再执行一次同步校验

###### devicemanager

devicemanager 既实现了对接 kublet 的 DevicePluginServer，又实现了 AllocateDevice 类型的 event 的 handler。换句话说是自己产生并处理自己的 event。

主要业务逻辑：

1. 创建 memif socket 文件的目录并挂载至容器

2. 创建连接 socket 的 secret。

上述的创建并不是真实的创建，而是把需要的信息(event.Envs, event.Annotations, event.Mounts)通过 grpc 返回给 kublet，让 kubelet 去创建。

devicemanager 还会将上述 memif 的信息保存在缓存中，供其他插件来获取。若缓存中信息不存在，则会调用 kubelet 的 api 获取信息。

###### ipNet

ipNet 插件主要负责 node 和 pod 中各类网卡的创建销毁，vxlan 的分配，vrf 的分配等

更新网卡时，ipnet 会读取 annotation 中 kv，判断网卡类型。若类型为 memif，则会向 deviceManager 获取当前 pod 里各容器的 memifInfo，之后根据 memifInfo 里的 socket 地址和 secret，创建 memif 类型的网卡事务，并 push 至 kvscheduler
