---
title: "dpvs 数据流分析"
date: 2025-02-18T11:11:00+08:00
draft: false
tags: ["dpdk","dpvs","高性能网络"]
tags_weight: 66
series: ["dpvs系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
---


## dpvs ingress流程分析

从 *lcore_job_recv_fwd* 开始，这个是dpvs收取报文的开始

### 设备层

dev->flag & NETIF_PORT_FLAG_FORWARD2KNI ---> 则拷贝一份mbuf到kni队列中，这个由命令行和配置文件决定（做流量镜像，用于抓包）

### eth层

*netif_rcv_mbuf* 这里面涉及到vlan的部分不做过多解析

-  不支持的协议

    目前dpvs支持的协议为ipv4, ipv6, arp。 其它报文类型直接丢给内核。其他类型可以看 [eth_types](https://en.wikipedia.org/wiki/EtherType#cite_note-ethtypes-3)。 [to_kni](#dpvs_kni_ingress)
- RTE_ARP_OP_REPLY

    复制 *nworks-1* 份mbuf，发送到其它worker的arp_ring上 ( ***to_other_worker*** ), 这份报文fwd到[arp协议](#arp协议).

- RTE_ARP_OP_REQUEST

    这份报文fwd到[arp协议](#arp协议).


### arp协议
    
- arp协议处理 *neigh_resolve_input*

    - RTE_ARP_OP_REPLY
        
        建立邻居表，记录信息，并且把这个报文送给内核。[to_kni](#dpvs_kni_ingress)

    - RTE_ARP_OP_REQUEST
        
        无条件返回网卡的ip以及mac地址 (*free arp*), *netif_xmit* 发送到 [core_tx_queue](#dpvs_egress报文分析)
    
    - 其它op_code
        
        [drop](#报文drop)

### ip层

- ipv4协议 (ipv6数据流程上一致)
    - *ipv4_rcv* 
        - ETH_PKT_OTHERHOST
            
            报文的dmac不是自己，[drop](#报文drop)

        - ipv4 协议校验
        
            不通过， [drop](#报文drop)
        
        - 下一层协议为 IPPROTO_OSPF

            [to_kni](#dpvs_kni_ingress)
        
        - *INET_HOOK_PRE_ROUTING hook*
         
            hook_list: *dp_vs_in* , *dp_vs_prerouting* 
            
            这两个都与synproxy有关系，但是我们不会启用这个代理，不过需要注意的是syncproxy不通过时会丢包 [drop](#报文drop)

            - dp_vs_in
            
                - 非 ETH_PKT_HOST(broadcast 或者 multicast报文)或ip报文交给 *ipv4_rcv_fin* 处理
                - 非 udp, tcp, icmp, icmp6报文交给 *ipv4_rcv_fin* 处理
                - 分片报文, 黑名单地址报文 [drop](#报文drop)
                - 非本core 报文， [redirect](#dpvs_redirect分析)到对应core上
                - conn超时，[drop](#报文drop)
                - xmit_inbound
                    
                    - 接收速率限速  [drop](#报文drop)
                    
                    - 这条连接的发送函数没有定义，走 *ipv4_rcv_fin*
                        
                        定义的发送函数： dp_vs_xmit_nat，dp_vs_xmit_tunnel， dp_vs_xmit_dr， dp_vs_xmit_fnat， dp_vs_xmit_snat

                    - 其它的包发送到 [core_tx_queue](#dpvs_egress报文分析)

                - xmit_outbound

                    - 发送速率限速  [drop](#报文drop)

                    - 这条连接的发送函数没有定义，走 *ipv4_rcv_fin*

                        定义的发送函数： dp_vs_out_xmit_nat， dp_vs_out_xmit_fnat， dp_vs_out_xmit_snat

                    - 其它的包发送到 [core_tx_queue](#dpvs_egress报文分析)
                
            
    - *ipv4_rcv_fin* 和内核处理流程基本一致
        - 找不到路由，[to_kni](#dpvs_kni_ingress)
        - localin， *ipv4_local_in*
        - RTF_KNI路由(下发时配置)，[to_kni](#dpvs_kni_ingress)
        - RTF_FORWARD
            - 非ETH_PKT_HOST, multicast or broadcast [to_kni](#dpvs_kni_ingress)
            - ETH_PKT_HOST，*ipv4_forward*
        - 其它类型的路由(RTF_OUTWALL)丢给内核，[to_kni](#dpvs_kni_ingress)

    - *ipv4_local_in* 是发给自己的报文
        - ip报文reassemble， 失败就 [drop](#报文drop)
        - INET_HOOK_LOCAL_IN hook_list: none
        - *ipv4_local_in_fin* 交给协议层处理
            - 当前支持的协议

                - [IPPROTO_GRE](https://blog.csdn.net/sinat_20184565/article/details/83280247) *gre_rcv*

                    gre协议的处理，处理通过之后，复用 [eth层](#eth层)处理
                - IPPROTO_IPIP *ipip_rcv*

                    ipip协议的处理，处理通过之后，复用 [eth层](#eth层)处理
                - IPPROTO_ICMP

                    icmp协议的处理，除了icmp_echo,别的都交给[to_kni](#dpvs_kni_ingress)。
                    
                    icmp最终 *ipv4_local_out*

            - 其它协议udp，tcp等 [to_kni](#dpvs_kni_ingress)

    - *ipv4_forward* 走路由
        - ttl check, fail, [drop](#报文drop)
        - mtu check, fail, [drop](#报文drop)
        - ipv4_forward_switch check, fail, [drop](#报文drop)
        - INET_HOOK_FORWARD hook_list: none
        - to *ipv4_output*

    - *ipv4_local_out* 
        - INET_HOOK_LOCAL_OUT hook_list: none
        - to *ipv4_output*

    - *ipv4_output*
        - INET_HOOK_POST_ROUTING hook_list: none
        - to *ipv4_output_fin*
    
    - *ipv4_output_fin*
        - ip 报文分片，失败会**drop***
        - to *ipv4_output_fin2*
    
    - *ipv4_output_fin2*

        查找路由，mbuf发送到 [core_tx_queue](#dpvs_egress报文分析)

## dpvs_egress报文分析

也就是 [core_tx_queue](#dpvs_kni_egress) 的数据流向，从*netif_tx_burst*开始分析。这里mbuf中的内容都已经填充完成，调用了 *rte_eth_tx_burst*，[发送至网卡](#用户态网卡驱动)。

## dpvs_redirect分析

*dp_vs_redirect_ring_proc*, 从 dp_vs_redirect_ring[cid][peer_id]中获取数据报文，导向 [dpvs_ingress](#dpvs-ingress流程分析)

## dpvs_kni
入口函数: *kni_lcore_loop*
- *kni_ingress_flow_process*: kni接管的队列的所有包都往[kernel_kni_in](#kernel_kni_ingress)上送 (非master core)
- *lcore_job_xmit*: 发送kni_core上的网络包(非master core),[core_tx_queue](#dpvs_egress报文分析)

dpvs 内部kni队列的处理如下两个流程。
### dpvs_kni_ingress
*kni_ingress_process*:
- *rte_ring_dequeue_burst(dev->kni.rx_ring)*: 收取所有dev上的kni报文接收队列的报文
- *rte_kni_tx_burst*: 发送到[kernel_kni_in](#kernel_kni_ingress)

### dpvs_kni_egress
*kni_egress_process*:
- rte_kni_handle_request: 分配mbuf给kni.resp_q
- rte_kni_rx_burst(dev->kni.kni): 接收kni设备中的报文,[kernel_kni_out](#kernel_kni_egress)
- netif_xmit: 发送到对应worker的发送队列,[core_tx_queue](#dpvs_egress报文分析)

# kni设备

![kni_module](https://pic3.zhimg.com/v2-e16e6eafac76d995f5b70b854c2d3f42_r.jpg)

## kni_init
- *rte_kni_alloc* 用户态kni设备申请

## kernel/linux/kni_misc.c
用于管理kni设备的。 简单的来说，这个文件创建了一个kni_misc设备，提供了ioctl方法创建相应的队列，可以简单的来看下ioctl的实现。

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
实际的kni_net设备,实现了报文的收发

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
内核发送给kni_thread， 对于内核来说最终调用kni_net_tx函数
```c
kni_net_tx
    kni_fifo_get(kni->alloc_q, &pkt_pa, 1); //从alloc_q 获取一个mbuf
	pkt_kva = get_kva(kni, pkt_pa); // mbuf转换为内核地址
	data_kva = get_data_kva(kni, pkt_kva); // mbuf的数据转换为内核地址
	pkt_va = pa2va(pkt_pa, pkt_kva); // 转换为虚拟地址？ 
    kni_fifo_put(kni->tx_q, &pkt_va, 1); // 放入tx_q
```


# 用户态网卡驱动
## igb_uio， mnlx cx5等物理网卡
- *rte_eth_tx_burst*
- *rte_eth_rx_burst*

## kni
- rte_kni_tx_burst(struct rte_kni *kni, struct rte_mbuf **mbufs, unsigned int num)
```c
num = RTE_MIN(kni_fifo_free_count(kni->rx_q), num);
phy_mbufs = va2pa_all(mbufs);
kni_fifo_put(kni->rx_q, phy_mbufs, num); // 发送到rx_q中
ret = kni_fifo_get(kni->free_q, (void **)pkts, MAX_MBUF_BURST_NUM); // 从free_q中取出mbuf后释放
rte_pktmbuf_free();

```

- rte_kni_rx_burst(struct rte_kni *kni, struct rte_mbuf **mbufs, unsigned int num)
```c
unsigned int ret = kni_fifo_get(kni->tx_q, (void **)mbufs, num);
kni_allocate_mbufs(); // 在alloc_q里面放入mbuf，防止kernel_kni_tx thread取不到mbuf
```
