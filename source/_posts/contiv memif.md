---
title: "contiv memif"
date: 2023-04-07T15:10:00+08:00
draft: false
tags: ["vpp","vpp-agent","k8s","contiv"]
tags_weight: 66
series: ["vpp系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
---

### contiv memif 



contiv的cni与device plugin相结合，实现了：

1. Pod能同时接入不止一张网卡
2. Pod接入的网卡可以是tap，veth，memif



#### devicePlugin

Device Plugin实际是一个运行在Kubelet所在的Node上的gRPC server，通过Unix Socket、基于以下（简化的）API来和Kubelet的gRPC server通信，并维护对应设备资源在当前Node上的注册、发现、分配、卸载。
其中，`ListAndWatch()`负责对应设备资源的discovery和watch；`Allocate()`负责设备资源的分配。

![6F9684EB-7E5E-4b77-9316-32D5C92FD07E](/img/vpp-agent/6F9684EB-7E5E-4b77-9316-32D5C92FD07E.png)



#### Insight

![contiveCNI.drawio](/img/vpp-agent/contiveCNI.drawio.png)

##### kubelet

kubelet接收上图格式的API。API中的annotations定义了pod的网卡个数与类型，resources中定义了所需要的device plugin的资源，也就是memif。

kubelet执行常规的syncPod流程，调用contiv cni创建网络。此时会在请求中将annotation传递给cni。

同时，agent的DevicePluginServer会向kubelet注册rpc服务，注册contivpp.io/memif的设备资源，从而kubelet的device manager会grpc请求DevicePluginServer获取contivpp.io/memif设备资源。



##### cni

cni实现了github.com/containernetworking/cni标准的add和del接口。实际上做的事情只是将cni请求转换为了对agent的grpc请求：解析args，并通过grpc调用agent的接口发送cniRequest，再根据grpc的返回结果，将结果再次转换成标准cni接口的返回格式



##### Agent

###### podmanager

podmanager实现了上述cni调用的grpc server，主要任务是将cni的request转换为内部的event数据格式，供event loop处理。

request是cni定义的请求数据类型，详见https://github.com/containernetworking/cni/blob/master/SPEC.md#parameters

event则是agent内部的关于pod事务模型，类似原生kvScheduler的针对vpp api的transaction。每一种event都会对应一个plugin去实现他的handler，供event loop调用。

###### event loop

event loop是整个contiv agent的核心处理逻辑，北向对接event queue，南向调用各个EventHandler，将event转换为kvScheduler的事务。

执行了以下步骤：

1. 对事件的预处理，包括校验，判断事件类型，加载必要的配置等
2. 判断是否是更新的事件
3. 对事件的handler进行排序，并生成正向或回退的handler顺序
4. 与本次事件无关的handler过滤掉
5. 创建对这次事件的记录record
6. 打印上述步骤生成的所有事件相关信息
7. 执行事件更新或同步，生成vpp-agent里的事务
8. 将contiv生成的配置与外部配置进行merge，得到最终配置
9. 将最终配置的vpp-agent事务commit到agent的kvscheduler
10. 若事务失败，将已经完成的操作进行回退
11. 完成事件，输出记录record与计时
12. 打印回退失败等不可恢复的异常
13. 若开启一致性检查，则最好再执行一次同步校验

###### devicemanager

devicemanager既实现了对接kublet的DevicePluginServer，又实现了AllocateDevice类型的event的handler。换句话说是自己产生并处理自己的event。

主要业务逻辑：

1. 创建memif socket文件的目录并挂载至容器

2. 创建连接socket的secret。

上述的创建并不是真实的创建，而是把需要的信息(event.Envs, event.Annotations, event.Mounts)通过grpc返回给kublet，让kubelet去创建。

devicemanager还会将上述memif的信息保存在缓存中，供其他插件来获取。若缓存中信息不存在，则会调用kubelet的api获取信息。



###### ipNet

ipNet插件主要负责node和pod中各类网卡的创建销毁，vxlan的分配，vrf的分配等

更新网卡时，ipnet会读取annotation中kv，判断网卡类型。若类型为memif，则会向deviceManager获取当前pod里各容器的memifInfo，之后根据memifInfo里的socket地址和secret，创建memif类型的网卡事务，并 push 至kvscheduler