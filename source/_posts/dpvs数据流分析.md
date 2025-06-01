---
title: "dpvs 数据流分析"
date: 2025-02-18T11:11:00+08:00
draft: false
tags: ["dpdk", "dpvs", "高性能网络"]
tags_weight: 66
series: ["dpvs系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
---

<!-- more -->

## dpvs ingress 流程分析

从 _lcore_job_recv_fwd_ 开始，这个是 dpvs 收取报文的开始

### 设备层

dev->flag & NETIF_PORT_FLAG_FORWARD2KNI ---> 则拷贝一份 mbuf 到 kni 队列中，这个由命令行和配置文件决定（做流量镜像，用于抓包）

### eth 层

_netif_rcv_mbuf_ 这里面涉及到 vlan 的部分不做过多解析

- 不支持的协议

  目前 dpvs 支持的协议为 ipv4, ipv6, arp。 其它报文类型直接丢给内核。其他类型可以看 [eth_types](https://en.wikipedia.org/wiki/EtherType#cite_note-ethtypes-3)。 [to_kni](#dpvs_kni_ingress)

- RTE_ARP_OP_REPLY

  复制 _nworks-1_ 份 mbuf，发送到其它 worker 的 arp_ring 上 ( **_to_other_worker_** ), 这份报文 fwd 到[arp 协议](#arp协议).

- RTE_ARP_OP_REQUEST

  这份报文 fwd 到[arp 协议](#arp协议).

### arp 协议

- arp 协议处理 _neigh_resolve_input_

  - RTE_ARP_OP_REPLY

    建立邻居表，记录信息，并且把这个报文送给内核。[to_kni](#dpvs_kni_ingress)

  - RTE_ARP_OP_REQUEST

    无条件返回网卡的 ip 以及 mac 地址 (_free arp_), _netif_xmit_ 发送到 [core_tx_queue](#dpvs_egress报文分析)

  - 其它 op_code
    [drop](#报文drop)

### ip 层

- ipv4 协议 (ipv6 数据流程上一致)

  - _ipv4_rcv_

    - ETH_PKT_OTHERHOST

      报文的 dmac 不是自己，[drop](#报文drop)

    - ipv4 协议校验

      不通过， [drop](#报文drop)

    - 下一层协议为 IPPROTO_OSPF

      [to_kni](#dpvs_kni_ingress)

    - _INET_HOOK_PRE_ROUTING hook_

      hook_list: _dp_vs_in_ , _dp_vs_prerouting_

      这两个都与 synproxy 有关系，但是我们不会启用这个代理，不过需要注意的是 syncproxy 不通过时会丢包 [drop](#报文drop)

      - dp_vs_in

        - 非 ETH_PKT_HOST(broadcast 或者 multicast 报文)或 ip 报文交给 _ipv4_rcv_fin_ 处理
        - 非 udp, tcp, icmp, icmp6 报文交给 _ipv4_rcv_fin_ 处理
        - 分片报文, 黑名单地址报文 [drop](#报文drop)
        - 非本 core 报文， [redirect](#dpvs_redirect分析)到对应 core 上
        - conn 超时，[drop](#报文drop)
        - xmit_inbound

          - 接收速率限速 [drop](#报文drop)

          - 这条连接的发送函数没有定义，走 _ipv4_rcv_fin_

            定义的发送函数： dp_vs_xmit_nat，dp_vs_xmit_tunnel， dp_vs_xmit_dr， dp_vs_xmit_fnat， dp_vs_xmit_snat

          - 其它的包发送到 [core_tx_queue](#dpvs_egress报文分析)

        - xmit_outbound

          - 发送速率限速 [drop](#报文drop)

          - 这条连接的发送函数没有定义，走 _ipv4_rcv_fin_

            定义的发送函数： dp_vs_out_xmit_nat， dp_vs_out_xmit_fnat， dp_vs_out_xmit_snat

          - 其它的包发送到 [core_tx_queue](#dpvs_egress报文分析)

  - _ipv4_rcv_fin_ 和内核处理流程基本一致

    - 找不到路由，[to_kni](#dpvs_kni_ingress)
    - localin， _ipv4_local_in_
    - RTF_KNI 路由(下发时配置)，[to_kni](#dpvs_kni_ingress)
    - RTF_FORWARD
      - 非 ETH_PKT_HOST, multicast or broadcast [to_kni](#dpvs_kni_ingress)
      - ETH_PKT_HOST，_ipv4_forward_
    - 其它类型的路由(RTF_OUTWALL)丢给内核，[to_kni](#dpvs_kni_ingress)

  - _ipv4_local_in_ 是发给自己的报文

    - ip 报文 reassemble， 失败就 [drop](#报文drop)
    - INET_HOOK_LOCAL_IN hook_list: none
    - _ipv4_local_in_fin_ 交给协议层处理

      - 当前支持的协议

        - [IPPROTO_GRE](https://blog.csdn.net/sinat_20184565/article/details/83280247) _gre_rcv_

          gre 协议的处理，处理通过之后，复用 [eth 层](#eth层)处理

        - IPPROTO_IPIP _ipip_rcv_

          ipip 协议的处理，处理通过之后，复用 [eth 层](#eth层)处理

        - IPPROTO_ICMP

          icmp 协议的处理，除了 icmp_echo,别的都交给[to_kni](#dpvs_kni_ingress)。

          icmp 最终 _ipv4_local_out_

      - 其它协议 udp，tcp 等 [to_kni](#dpvs_kni_ingress)

  - _ipv4_forward_ 走路由

    - ttl check, fail, [drop](#报文drop)
    - mtu check, fail, [drop](#报文drop)
    - ipv4_forward_switch check, fail, [drop](#报文drop)
    - INET_HOOK_FORWARD hook_list: none
    - to _ipv4_output_

  - _ipv4_local_out_

    - INET_HOOK_LOCAL_OUT hook_list: none
    - to _ipv4_output_

  - _ipv4_output_

    - INET_HOOK_POST_ROUTING hook_list: none
    - to _ipv4_output_fin_

  - _ipv4_output_fin_

    - ip 报文分片，失败会**drop\***
    - to _ipv4_output_fin2_

  - _ipv4_output_fin2_

    查找路由，mbuf 发送到 [core_tx_queue](#dpvs_egress报文分析)

## dpvs_egress 报文分析

也就是 [core_tx_queue](#dpvs_kni_egress) 的数据流向，从*netif_tx_burst*开始分析。这里 mbuf 中的内容都已经填充完成，调用了 _rte_eth_tx_burst_，[发送至网卡](#用户态网卡驱动)。

## dpvs_redirect 分析

_dp_vs_redirect_ring_proc_, 从 dp_vs_redirect_ring[cid][peer_id]中获取数据报文，导向 [dpvs_ingress](#dpvs-ingress流程分析)

## dpvs_kni

入口函数: _kni_lcore_loop_

- _kni_ingress_flow_process_: kni 接管的队列的所有包都往[kernel_kni_in](#kernel_kni_ingress)上送 (非 master core)
- _lcore_job_xmit_: 发送 kni_core 上的网络包(非 master core),[core_tx_queue](#dpvs_egress报文分析)

dpvs 内部 kni 队列的处理如下两个流程。

### dpvs_kni_ingress

_kni_ingress_process_:

- _rte_ring_dequeue_burst(dev->kni.rx_ring)_: 收取所有 dev 上的 kni 报文接收队列的报文
- _rte_kni_tx_burst_: 发送到[kernel_kni_in](#kernel_kni_ingress)

### dpvs_kni_egress

_kni_egress_process_:

- rte_kni_handle_request: 分配 mbuf 给 kni.resp_q
- rte_kni_rx_burst(dev->kni.kni): 接收 kni 设备中的报文,[kernel_kni_out](#kernel_kni_egress)
- netif_xmit: 发送到对应 worker 的发送队列,[core_tx_queue](#dpvs_egress报文分析)

# kni 设备

![kni_module](https://pic3.zhimg.com/v2-e16e6eafac76d995f5b70b854c2d3f42_r.jpg)

## kni_init

- _rte_kni_alloc_ 用户态 kni 设备申请

## kernel/linux/kni_misc.c

用于管理 kni 设备的。 简单的来说，这个文件创建了一个 kni_misc 设备，提供了 ioctl 方法创建相应的队列，可以简单的来看下 ioctl 的实现。

```c
kni_ioctl:
    case _IOC_NR(RTE_KNI_IOCTL_CREATE):
        kni_ioctl_create:
            param_check() // 参数检查
            alloc_netdev() // 申请一个net_dev
                params: priv=struct kni_dev, name=kni.name, init_func = kni_net_init // 由kni_net.c 提供
                addr_translation //地址转换，将物理地址转换为内核虚拟地址
                register_netdev
                kni_run_thread
                    //根据 multiple_kthread_on 创建相应的kernel线程，并绑核
                    // 对于multiple mode，每一个kni_net_dev都创建一个线程
                    // 对于single mode，只有第一次会创建线程，后面都只是把这个kni_net_dev加入队列中
                    kni_thread_multiple: // 就收发自己的kni_net_dev
                    kni_thread_single: // 收发这个kni所管理的全部net_dev
                        kni_net_rx(kni_dev) // kni_net.c 提供
                        kni_net_poll_resp(kni_dev)
```

## kernel/linux/kni_net.c

实际的 kni_net 设备,实现了报文的收发

![kni_packets](https://pic4.zhimg.com/v2-0b2066b331faa8907efad69e84e754b3_1440w.jpg)

### kni_net_init

```c
kni_net_init
    dev->netdev_ops      = &kni_net_netdev_ops; // 关键的open, tx，close 函数都在这儿
        kni_net_open
        kni_net_release
        kni_net_tx
	dev->header_ops      = &kni_net_header_ops; // 填充以太网头eth用的
	dev->ethtool_ops     = &kni_net_ethtool_ops; //获得驱动信息
	dev->watchdog_timeo = WD_TIMEOUT;
```

### kernel_kni_ingress

```c
kni_net_rx
    kni_net_rx_normal // normal mode, 在lo，lo_skb模式下会有变化 将dpdk发送来的数据发送到内核中
        kni_fifo_get(kni->rx_q, kni->pa, num_rx); // 从rx_q从接收数据
        get_kva(kni, kni->pa[i]); // 将mbuf的phy_addr转换为内核的virtaddr
        get_data_kva(kni, kva); //将mbuf data_addr的phy_addr转换为kva
        // mbuf->skb 处理
        netif_rx(skb) //将处理后的skb发送给内核
        kni_fifo_put(kni->free_q, kni->va, num_rx); // 把mbuf送到free_q，让dpdk释放它
```

### kernel_kni_egress

内核发送给 kni_thread， 对于内核来说最终调用 kni_net_tx 函数

```c
kni_net_tx
    kni_fifo_get(kni->alloc_q, &pkt_pa, 1); //从alloc_q 获取一个mbuf
	pkt_kva = get_kva(kni, pkt_pa); // mbuf转换为内核地址
	data_kva = get_data_kva(kni, pkt_kva); // mbuf的数据转换为内核地址
	pkt_va = pa2va(pkt_pa, pkt_kva); // 转换为虚拟地址？
    kni_fifo_put(kni->tx_q, &pkt_va, 1); // 放入tx_q
```

# 用户态网卡驱动

## igb_uio， mnlx cx5 等物理网卡

- _rte_eth_tx_burst_
- _rte_eth_rx_burst_

## kni

- rte_kni_tx_burst(struct rte_kni \*kni, struct rte_mbuf \*\*mbufs, unsigned int num)

```c
num = RTE_MIN(kni_fifo_free_count(kni->rx_q), num);
phy_mbufs = va2pa_all(mbufs);
kni_fifo_put(kni->rx_q, phy_mbufs, num); // 发送到rx_q中
ret = kni_fifo_get(kni->free_q, (void **)pkts, MAX_MBUF_BURST_NUM); // 从free_q中取出mbuf后释放
rte_pktmbuf_free();

```

- rte_kni_rx_burst(struct rte_kni \*kni, struct rte_mbuf \*\*mbufs, unsigned int num)

```c
unsigned int ret = kni_fifo_get(kni->tx_q, (void **)mbufs, num);
kni_allocate_mbufs(); // 在alloc_q里面放入mbuf，防止kernel_kni_tx thread取不到mbuf
```
