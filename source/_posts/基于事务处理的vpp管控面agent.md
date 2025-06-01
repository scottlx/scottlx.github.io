---
title: "基于事务处理的vpp管控面agent"
date: 2022-10-03T13:00:00+08:00
draft: false
tags: ["vpp", "vpp-agent", "sdn", "nfv"]
tags_weight: 66
series: ["vpp系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
---

<!-- more -->

### 问题背景

vpp 作为 vrouter，类似物理交换机，各配置项依赖关系复杂。以下为 vpp 配置 abf 策略路由的例子：

```c
typedef abf_policy
{
  u32 policy_id;
  u32 acl_index;  //依赖acl
  u8 n_paths;
  vl_api_fib_path_t paths[n_paths];
};

autoreply define abf_policy_add_del
{
  option status="in_progress";
  u32 client_index;
  u32 context;
  bool is_add;
  vl_api_abf_policy_t policy;
};

typedef abf_itf_attach
{
  u32 policy_id;
  vl_api_interface_index_t sw_if_index; //依赖interface，interface又会依赖其他资源
  u32 priority;
  bool is_ipv6;
};

```

可以看到，策略路由首先依赖 acl 规则，之后将 abf 绑定至接口时需要依赖对应 interface 的 index，且创建 interface 又需要依赖其他资源（绑定 vrf 等）。

除此之外，vpp 配置写入存在中间状态与崩溃的问题，且无法避免。“崩溃”类似数据库写入的概念。数据必须要成功写入磁盘、磁带等持久化存储器后才能拥有持久性，只存储在，内存中的数据，一旦遇到应用程序忽然崩溃，或者数据库、操作系统一侧的崩溃，甚至是机器突然断电宕机等情况就会丢失，这些意外情况都统称为“崩溃”。

因此，为了解决 vpp（物理交换机也适用）各配置项的依赖关系，以及保证原子性和持久性，实现崩溃恢复，需要在管控面 agent 侧处理好上述问题。

### 事务处理

本人对分布式事务领域涉及不深，以下摘自于

[凤凰架构]: https://jingyecn.top:18080/

本地事务（也可称为局部事务），是单个服务使用单个数据源场景，也就是最基本的本地数据落盘的事务。本地事务要求底层数据源需要支持事务的开启、终止、提交、回滚、嵌套等。在数据库领域（ARIES 理论，基于语义的恢复与隔离），感兴趣的可以研究下 commiting logging 机制（OceanBase）和 shadow paging

全局事务，是单个服务多个数据源场景。主要目的是为了解决事务一致性问题，并做到统一提交，统一回滚的功能。例如我有一个全局事务需要在 A 表中写入记录 a（本地事务 A），再在 B 表中写入记录 b（本地事务 B），A 表和 B 表分别在两台物理机的磁盘上。在数据存储领域由 X/Open XA 对此发布了一个事务处理架构，且当前很多分布式事务处理框架都是基于此来设计的。主要核心如下：

- 全局事务管理器（Transaction Manager，TM）：协调全局事务
- 局部资源管理器（Resource Manaeger，RM）：驱动本地事务
- 模型：XA，TCC，SAGA，AT 。。。

感兴趣的可以研究下阿里的 seata。

### 事务处理视角看待 vpp 管控面

#### 本地事务

- vpp 的配置是内存上的配置，不需要落盘。
- vpp 的每个资源的 api 可视为一个数据源
- 数据源没有实现事务的开启、终止、提交、回滚、嵌套、设置隔离级别等能力，只提供了下发，删除，读取接口
- 上述数据源未提供的能力需要 agent 来补齐

#### 全局事务

- agent 暴露给上层的接口可视为全局事务
- 有些全局事务只涉及单个数据源，有些全局事务涉及多个数据源
- agent 内部需要实现 TM，将全局事务转为有序的本地事务列表
- agent 内部需要实现 RM，调用 vpp api，驱动本地事务的执行

举例：

全局事务：创建策略路由

本地事务：创建 acl -->创建 abf --> 创建接口 --> 绑定接口 IP --> 绑定 abf 至接口

那么问题来了：如何处理单个数据源（本地事务）的回滚？如何实现 TM 的功能（全局事务到本地事务的转换）？

### agent 设计思想

![image-kvscheduler](/img/vpp-agent/kvscheduler.svg)

#### RM 的实现

**配置描述符（descriptors）**

- 单条 vpp 配置（数据源）定义配置描述符
- 配置描述符中定义了数据源的基本操作 CRUD，以及与其他配置描述符的关系（依赖 depend，派生 derive）
- 派生机制：将一个本地事务分割成多个本地事务，分割后的本地事务有自己的 CRUD，依赖，派生
- 全局事务：”根节点”
- 本地事务：”其他节点”

#### TM 的实现

**调度器 （KVscheduler）**

- 充当 TM 的角色
- 根据 descriptors 定义的关系（依赖，派生）创建关系图，编排配置的下发顺序
- 按照 saga 事务模型处理事务一致性问题（单条本地事务异常，涉及到的数据源全部回退）

事实上 agent 内的本地事务与全局事务并没有像数据库领域那样明显的区分。以上均为个人便于理解加以区分，实际代码内部并没有上述的区分。一个事务可以同时有全局事务和本地事务的属性，取决于是否存在衍生。全局事务内部也有与 vpp 交互的本地事务

![image-20221003140631624](/img/vpp-agent/image-20221003140631624.png)

### 事务流程（创建 bridge domain 为例）

![image-txn](/img/vpp-agent/add_bd_before_interface.svg?sanitize=true)

#### 初始化阶段

1. 配置描述符（descriptor, RM）向调度器（Schedluer, TM）注册本地事务逻辑
2. Descriptors 在定义了对应资源 CRUD 的回调函数，
3. 回调函数内部使用 unix socket 与 vpp 通信

#### 全量同步（TX0，南向同步阶段）

1. 编排器（Orchestrator）作为全局事务来源，北向收集 etcd，http 请求，grpc 请求等接口中的全局事务，下发至调度器
2. 启动阶段编排器默认下发全量同步事务 0（假设此时 ETCD 中只有一个 key）
3. retrieve 调用 vpp 提供各数据源的 dump 接口，同步上一次本地事务的执行状态至内存

全局事务类型

- **北向同步**：只同步北向所有全局事务至缓存
- **南向同步**：只同步南向所有数据源至缓存
- **全量同步**：北向同步 + 南向同步
- **增量同步**：同步北向单个全局事务至缓存 + 同步缓存中单个全局事务涉及到的数据源到 vpp

#### 全量同步（ TX0，北向同步阶段）

1. 扫描所有已注册的描述符，找到全局事务涉及的本地事务的描述符
2. 依次调用配置描述符的依赖，派生，校验，创建等回调函数
3. 全局事务涉及的数据源，根据描述符的派生机制，按照 DAG 的形式，**动态生成** （首先只有 BD Descriptor 本地事务，之后派生出 BD-Interface 本地事务）
4. 再次扫描所有已注册的描述符，找到上一步衍生出的本地事务对应的描述符
5. 依次调用配置描述符的依赖，派生，校验，创建等回调函数
6. 由于该描述符存在依赖，且内存中不存在此依赖，本次全局事务结束 (依赖不满足不代表全局事务执行失败，本地事务进入挂起状态（在后续全局事务中完成），处理下一个全局事务)

#### 增量同步（ TX1）

1. 北向数据源更新数据（修改 ETCD，调用 HTTP，RPC 接口），下发增量同步全局事务（TX1）
2. 找到指定配置描述符，依次调用配置描述符的依赖，派生，校验，创建等回调函数
3. 创建成功后，修改描述符标志位，使得满足上一次 pending 本地事务的依赖，触发 pending 本地事务的运行

#### 异常情况（ TX1）

- 当本地事务的描述符 CRUD 操作发生异常（VPP 报错），本次全局事务视为失败

- besteffort 策略（默认策略）

  - 调度器复制本次失败的本地事务，新建 retry 全局事务，进行重新尝试

    - 由于上一次 vpp api 调用失败，无法预测 vpp 当前的配置，首先 retreive 失败的本地事务（vpp dump api）

    - 使用 retreive 得到的配置刷新内存，再次执行失败的本地事务

      - retreive 的数据与事务数据一致：事务无需执行直接成功（上次失败原因：已经下发成功了但没收到回复）

      - retreive 的数据与事务数据不一致：执行 CRUD 覆盖 vpp 的脏数据（上次失败原因：vpp 内部异常）

      - retreive 不到数据：执行 CRUD 下发配置（失败原因：IO 或网络原因请求未到达 vpp）

- revert 策略
  - 调度器创建回退全局事务，进行回退
    - 由于上一次 vpp api 调用失败，无法预测 vpp 当前的配置，首先 retreive 失败的本地事务
    - 使用 retreive 得到的配置刷新内存，按照 derive 的顺序自底向上进行回退

### 总结

- vpp agent 事务处理方式符合 X/Open XA 的分布式事务处理规范
- 管控面采用 vpp agent 能极大降低开发者的心智负担
- 其中事务处理的的思想也适用于其他路由器的 sdn 代码开发

### 参考文档

[ligato](https://github.com/ligato/vpp-agent)[/](https://github.com/ligato/vpp-agent)[vpp](https://github.com/ligato/vpp-agent)[-agent: ⚡️ Control plane management agent for ](https://github.com/ligato/vpp-agent)[FD.io's](https://github.com/ligato/vpp-agent)[ VPP (github.com)](https://github.com/ligato/vpp-agent)

[Control Flows - ](https://docs.ligato.io/en/latest/developer-guide/control-flows/)[Ligato](https://docs.ligato.io/en/latest/developer-guide/control-flows/)[ Docs](https://docs.ligato.io/en/latest/developer-guide/control-flows/)

[Seata](https://seata.io/zh-cn/docs/overview/what-is-seata.html)

[凤凰架构：构筑可靠的大型分布式系统](http://icyfenix.cn/)
