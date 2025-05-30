---
title: "cilium endpoint 创建流程"
date: 2025-05-30T10:15:00+08:00
draft: false
tags: ["ebpf","cilium","k8s"]
tags_weight: 66
series: ["ebpf系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
---


pod创建后，cilium打通网络涉及以下内容：

1. lxc网卡的创建（cni插件）
2. bpf代码的加载（agent）
3. ipam 地址分配（agent）
4. endpoint CR的创建（agent）
5. endpoint生命周期管理（agent）

具体流程如下图

![cilium_cni流程](/img/blobs/cilium_cni流程.png)

> 流程说明

cni add流程主要分三步：

1. 调用ipam接口从agent获取ip信息
2. 创建lxc网卡（veth），根据ip信息配置网卡（mtu，gso，gro配置），容器命名空间内路由等
3. 调用endpointCreate接口通知agent开始接管lxc网卡（加载bpf代码等）

如果启用了cniChaning，还会去执行chaining的动作



ipam流程将在后期详细介绍，本篇主要分析endpointCreate之后的流程，也就是bpf代码是如何加载到lxc网卡上的。



### cilium controller

cilium agent代码内部，对于资源同步的场景，设计了一套controller框架。

controller可以理解为异步任务控制器，在后台尝试某一对象的同步任务直到成功，并记录成功失败次数，错误日志等监控数据。每个controller对应一个协程。

controller需要被manager绑定，而manager则绑定到某一特定资源，比如endpoint

由于资源的变配会需要多个异步任务的执行，因此一个manager可以关联多个controller，单个controller只负责某一特定的异步任务(只要是可能失败并需要重试的任务都适用，例如给k8s资源打annotation，同步对象到某个存储，kvstore，bpfmap等）

controller之间通信通过eventqueue进行异步解耦。每个evq对应一个协程



#### endpoint manager的架构图

![cilium_agent](/img/blobs/cilium_agent.png)

> 说明

上图中，endpoint manager维护了本节点endpoint列表，并实现了

- gc controller：endpoint定期清理，清理不健康的endpoint。
- regenerate controller：endpoint定期全量重建，重建ep对应的policy和configuration

CNI创建endpoint后，endpoint对象会被创建。每个endpoint初始化时会有一个eventQueue和处理该eventQueue的一个controller。endpoint manager会将regen事件入队到endpoint的eventQueue中，并启动endpoint的sync Controller。sync Controller会同步ep信息到k8s cep CR，这样用户就可以从apiserver获取endpoint状态了



### ebpf程序加载流程

eventQueue中的regeneration event会触发endpoint的重建，也就是相关ebpf程序的编译加载和ebpf map数据的插入。



bpf程序加载会发生在几种情况下：

1. 第一次创建时进行初始化
2. cilium重启时，会进行一次regenerate（按需初始化）
3. 用户执行cilium endpoint regenerate 时（按需）
4. 用户执行ciluim config时（按需）



程序是否加载由编译级别来控制

- 0 -> “invalid” (未设置)

- 1：RegenerateWithoutDatapath -> “no-rebuild” （更新policy，dns，只需更新map，不重新编译加载bpf）

- 2：RegenerateWithDatapath -> "rewrite+load" （新创建endpoint）

  

这边可能会有个疑问，为什么更新policy不是更新lxc代码。这是因为policy代码是在bpf_lxc的最后使用尾调用执行的。因此更新policy只要更新prog bpf，而不需要动已经加载在lxc上的代码

```c
static __always_inline int
l3_local_delivery(struct __ctx_buff *ctx, __u32 seclabel,
		  __u32 magic __maybe_unused,
		  const struct endpoint_info *ep __maybe_unused,
		  __u8 direction __maybe_unused,
		  bool from_host __maybe_unused,
		  bool from_tunnel __maybe_unused, __u32 cluster_id __maybe_unused)
{
    /*省略一些代码*/

	/* Jumps to destination pod's BPF program to enforce ingress policies. */
	ctx_store_meta(ctx, CB_SRC_LABEL, seclabel);
	ctx_store_meta(ctx, CB_DELIVERY_REDIRECT, 1);
	ctx_store_meta(ctx, CB_FROM_HOST, from_host ? 1 : 0);
	ctx_store_meta(ctx, CB_FROM_TUNNEL, from_tunnel ? 1 : 0);
	ctx_store_meta(ctx, CB_CLUSTER_ID_INGRESS, cluster_id);

	return tail_call_policy(ctx, ep->lxc_id);
#endif
}
```



EndpointRegenerationEvent handler流程

![emvs-regenerate](/img/blobs/emvs-regenerate.png)



endpoint的配置（例如开启debug，trace）是通过修改编译时的头文件来实现的

下面是state目录

![endpoint编译文件目录](/img/blobs/endpoint编译文件目录.PNG)

可以看到每个endpoint会有一个ep id对应的目录，目录里包含该ep的配置生成的头文件`ep_config.h`。

代码仓库里的`ep_config.h`只是一个样例，实际的是以state目录下生成的为准。

相应的，netdev也有配置文件`node-config.h`

编译过程中，会生成tmpDir，用来保存编译时的headers。只有整个generation完全成功后，tmpDir才会覆盖origDir。

可以在state目录下执行`inotifywait -r -m . -e open -e move -e create -e delete` 查看相关文件的生成情况
