---
title: "nfv化dpvs的cpu fdir"
date: 2025-05-30T14:54:00+08:00
draft: false
tags: ["dpdk","dpvs","高性能网络"]
tags_weight: 66
series: ["dpvs系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
---

### 数据亲和性

如图所示，dpvs 机器有两块网卡，nic1 是 wan 外网网卡, nic0 是 lan 内网网卡。 当 client 发送 packet 时，网卡一般由 rss 来选择数据发送到哪个队列，一般这个队列都会绑定到某个核心 lcore.。rss 一般根据四元组<dport, dip, sport, sip>来分配网卡队列。但是，当 packet 由 rs 返回 dpvs 时，如果还是根据四元组来做 rss, 那么得到的队列必然无法对应到正确的 lcore. 这就会引起流表数据 miss, 如果再从其它 lcore 查表，必然会引起共享数据加锁，和 cpu cache 失效问题。

![dpvs亲和性](/img/dpvs/dpvs亲和性.webp)

dpvs提出的解决方案是flow director（fdir）。当server回包数据进入网卡后不再采用四元组+rss去计算队列，而是根据数据的dport将数据精确分配到预定队列。如下图所示，本地可用端口，根据 cpu 个数做掩码。将端口固定到某个 lcore。举个例子，如果网卡有 16 个队列，那么就配置 16 个 cpu, 掩码是 0x0F, 端口根据掩码取余，就会对应到指定的队列和cpu。

dpvs在新建一条conn时会从lcore所关联的port池里选择sport，从而实现了同一对数据包被同一个lcore处理。

![dpvs_fdir](/img/dpvs/dpvs_fdir.webp)

实际上，fdir是dpdk提供的api，支持多层协议堆叠的flow规则配置，用户可以基于proto、ip、port等信息随意定制自己的flow。根据硬件选型，lb物理机所采用的网卡是NVIDIA Mellanox ConnectX-6，支持L2-L4的fdir规则定制，因此lb的fdir规则考虑采用geneve封装的outer ip 作为flow的匹配条件，因为我们的lip是per core的，实现便捷，同时可以避免端口抢占。

检查网卡是否支持fdir：http://doc.dpdk.org/guides/nics/overview.html#id1



### nfv化问题

对于云上虚拟机 virtio 等没有硬件卸载能力，但有用户态网卡驱动的网卡，需要通过软件来实现上述fdir功能



#### 基本思路

在`netif_deliver_mbuf`处，也就是进入dpvs单核处理流程之前，进行一次cpu导流。

cpu两两之间预先分配好导流用的ring buffer，报文经过parser解析后，根据规则进行精确匹配。若匹配到fidr规则，则按照规则指定的cid转移报文到指定的cpu ring buffer

lcore job增加一个处理fdir ring的job ：lcore_process_fdir_ring`（类似process_arp_ring）
![cpu_fdir](/img/dpvs/cpu_fidr.png)


#### patch
基于dpvs commit 6ddc860a2f15c96b6141c2c2be1595ee72965d11

```c
diff --git a/config.mk b/config.mk
index 91673dc93aff8fc47bcde6f275c439b44c5b8ef8..ff98d5c997a9207feb4e73e3e9ad899e9f28f090 100644
--- a/config.mk
+++ b/config.mk
@@ -10,7 +10,7 @@ export CONFIG_PDUMP=y
 export CONFIG_ICMP_REDIRECT_CORE=n
 
 # debugging and logging
-export CONFIG_DEBUG=n
+export CONFIG_DEBUG=y
 export CONFIG_DPVS_NEIGH_DEBUG=n
 export CONFIG_RECORD_BIG_LOOP=n
 export CONFIG_DPVS_SAPOOL_DEBUG=n
diff --git a/include/conf/cpu_fdir.h b/include/conf/cpu_fdir.h
new file mode 100644
index 0000000000000000000000000000000000000000..2bd26dd5880aa740c1290b3f095b02bf6761f890
--- /dev/null
+++ b/include/conf/cpu_fdir.h
@@ -0,0 +1,22 @@
+#ifndef __CPU_FDIR_CONF_H__
+#define __CPU_FDIR_CONF_H__
+
+#include "conf/sockopts.h"
+
+struct cpu_fdir_conf {
+    uint8_t              proto;
+    uint8_t              cid;
+
+    uint32_t             daddr;
+    uint8_t              d_plen;
+    uint16_t             dport_from;
+    uint16_t             dport_to;
+}__attribute__((__packed__));
+
+
+struct cpu_fdir_conf_array {
+    int                    nrule;
+    struct cpu_fdir_conf   rules[0];
+} __attribute__((__packed__));
+
+#endif
\ No newline at end of file
diff --git a/include/conf/sockopts.h b/include/conf/sockopts.h
index 2e3cd75950c4939a3c1e3f143fcfd26306dcfcbb..9e0bf0d764c973341c24a04ac6f5858ce094efc8 100644
--- a/include/conf/sockopts.h
+++ b/include/conf/sockopts.h
@@ -149,6 +149,10 @@
     DPVSMSG(SOCKOPT_SET_IFTRAF_ADD) \
     DPVSMSG(SOCKOPT_SET_IFTRAF_DEL) \
     DPVSMSG(SOCKOPT_GET_IFTRAF_SHOW)\
+    \
+    DPVSMSG(SOCKOPT_SET_FDIR_ADD)  \
+    DPVSMSG(SOCKOPT_SET_FDIR_DEL)  \
+    DPVSMSG(SOCKOPT_GET_FDIR_SHOW) \
 
 typedef enum {
     DPVSMSG_SOCKOPT_ENUM(ENUM_ITEM)
diff --git a/include/fdir/cpu_fdir.h b/include/fdir/cpu_fdir.h
new file mode 100644
index 0000000000000000000000000000000000000000..71f780efe6910b5d91f43e5cb2fdb491dda4966d
--- /dev/null
+++ b/include/fdir/cpu_fdir.h
@@ -0,0 +1,29 @@
+#ifndef __CPU_FDIR_H__
+#define __CPU_FDIR_H__
+#include "conf/common.h"
+#include "netif.h"
+#include "list.h"
+#include "dpdk.h"
+
+struct cpu_fdir {
+    struct list_head     list;
+
+    uint8_t              proto;
+    lcoreid_t            cid;
+
+    // only support inner header for now
+    rte_be32_t           daddr;
+    rte_be32_t           daddr_mask;
+    uint16_t             dport_from;
+    uint16_t             dport_to;
+} __rte_cache_aligned;
+
+struct cpu_fdir *cpu_fdir_alloc(void);
+struct cpu_fdir *cpu_fdir_get(uint8_t proto, rte_be32_t saddr, rte_be32_t daddr,
+    uint16_t sport, uint16_t dport);
+int cpu_fdir_pkt(struct rte_mbuf *mbuf, lcoreid_t peer_cid);
+void cpu_fdir_ring_proc(lcoreid_t cid);
+int cpu_fdir_redirect(struct rte_mbuf *mbuf, int *redirected);
+int cpu_fdir_init(void);
+int cpu_fdir_term(void);
+#endif /* __CPU_FDIR_H__ */
\ No newline at end of file
diff --git a/include/fdir/parser.h b/include/fdir/parser.h
new file mode 100644
index 0000000000000000000000000000000000000000..f793d952293ee8ca5790c90c509d8464d8a8155a
--- /dev/null
+++ b/include/fdir/parser.h
@@ -0,0 +1,97 @@
+#ifndef __FDIR_PARSER_H__
+#define __FDIR_PARSER_H__
+#include "conf/common.h"
+#include <rte_mbuf.h>
+#include "netif.h"
+#include "dpdk.h"
+#include "rte_geneve.h"
+#include "rte_vxlan.h"
+
+enum {
+    TUNNEL_NONE,
+	TUNNEL_VXLAN,
+	TUNNEL_GENEVE,
+};
+
+#if RTE_BYTE_ORDER == RTE_LITTLE_ENDIAN
+#define _htons(x) ((uint16_t)((((x) & 0x00ffU) << 8) | (((x) & 0xff00U) >> 8)))
+#else
+#define _htons(x) (x)
+#endif
+
+// common parser and segment lib
+typedef struct sk_buff_info {
+	struct rte_mbuf*    mbuf;
+	/* Pointer to start of data.
+	 * Note this is different from mbuf.buf_addr.
+	 * head = (void *)skb + sizeof(*skb)
+	 * mbuf.buf_addr = (void *)skb + sizeof(mbuf)
+	 */
+	union {
+	    struct rte_ether_hdr    *outer_eth; 
+	    void    *outer_l2_hdr;
+	}; /* ether header, or NULL if not set */
+	
+	union {
+		struct rte_ipv4_hdr    *outer_iph;
+		struct rte_arp_hdr    *outer_arph;
+		void    *outer_l3_hdr;
+	}; /*inner ip/arp header, or NULL if not set */
+
+	union {
+		struct rte_tcp_hdr    *outer_tcph;
+		struct rte_udp_hdr    *outer_udph;
+		struct rte_icmp_hdr    *outer_icmph;
+		void    *outer_l4_hdr;
+	}; /* inner_tcp/udp/icmp header, or NULL if not set */
+	union {
+	    struct rte_ether_hdr    *eth; // vlan = eth + 1 if exist
+		void    *l2_hdr;
+	}; /* ether header, or NULL if not set */
+
+	union {
+		struct rte_ipv6_hdr    *ip6h;
+		struct rte_ipv4_hdr    *iph;
+		struct rte_arp_hdr    *arph;
+		void    *l3_hdr;
+	}; /* ip/arp header, or NULL if not set */
+
+	union {
+		struct rte_tcp_hdr    *tcph;
+		struct rte_udp_hdr    *udph;
+		struct rte_icmp_hdr    *icmph;
+		void    *l4_hdr;
+	}; /* tcp/udp/icmp header, or NULL if not set */
+
+	union {
+		struct rte_vxlan_hdr    *vxlanh;
+		struct rte_geneve_hdr    *genh;
+		void    *tunnel_hdr;
+	}; /* vxlan/geneve header, or NULL if not set */
+
+	uint32_t recv_vni;
+	uint8_t vni_flag;
+
+	uint16_t outer_l2_len;
+	uint16_t outer_l3_len;
+	uint16_t outer_l4_len;
+	
+    uint8_t  tunnel_type;
+	uint8_t  tunnel_flag;
+    uint16_t  tunnel_len;
+
+	uint16_t l2_len;
+	uint16_t l3_len;
+	uint16_t l4_len;
+
+	uint8_t outer_ethertype;
+	uint8_t ethertype;
+	uint8_t outer_l4_proto;
+	uint8_t l4_proto;
+	uint8_t is_tunnel;
+} pkt_info_t __rte_cache_aligned;
+
+
+void pkt_info_init(pkt_info_t* pkt_info, struct rte_mbuf* mbuf);
+
+#endif
\ No newline at end of file
diff --git a/src/fdir/cpu_fdir.c b/src/fdir/cpu_fdir.c
new file mode 100644
index 0000000000000000000000000000000000000000..3aef18a13dccb652210b8bad8de96b1c4e32c0c0
--- /dev/null
+++ b/src/fdir/cpu_fdir.c
@@ -0,0 +1,468 @@
+#include "fdir/cpu_fdir.h"
+#include "fdir/parser.h"
+#include "conf/cpu_fdir.h"
+#include "ctrl.h"
+
+
+#define CPU_FDIR_RING_SIZE  2048
+
+#define CPU_FDIR_TBL_BITS   10
+#define CPU_FDIR_TBL_SIZE   (1 << CPU_FDIR_TBL_BITS)
+#define CPU_FDIR_TBL_MASK   (CPU_FDIR_TBL_SIZE - 1)
+
+static struct list_head   *g_cpu_fdir_tbl;
+static rte_spinlock_t      cpu_fdir_tbl_lock[DPVS_MAX_SOCKET];
+static struct rte_mempool *g_cpu_fdir_cache[DPVS_MAX_SOCKET];
+static rte_atomic32_t num_fdir_rules[DPVS_MAX_SOCKET];
+#define this_cpu_fdir_cache      (g_cpu_fdir_cache[rte_socket_id()])
+#define this_num_fdir_rules      (num_fdir_rules[rte_socket_id()])
+
+
+static struct rte_ring    *cpu_fdir_ring[DPVS_MAX_LCORE][DPVS_MAX_LCORE];
+bool cpu_fdir_enable = true;
+
+#define CPU_FDIR
+#define RTE_LOGTYPE_CPU_FDIR    RTE_LOGTYPE_USER1
+
+
+#ifdef CONFIG_DPVS_IPVS_DEBUG
+static inline void
+cpu_fdir_show(struct cpu_fdir *r, const char *action)
+{
+    char sbuf[64], dbuf[64];
+    char sbufm[64], dbufm[64];
+
+    RTE_LOG(DEBUG, CPU_FDIR, "[%d] redirect %s: [%d] %s %s/%s:%d-%d -> %s/%s:%d-%d\n",
+            rte_lcore_id(), action, r->cid,
+            inet_proto_name(r->proto),
+            inet_ntop(r->af, &r->saddr, sbuf, sizeof(sbuf)) ? sbuf : "::",
+            inet_ntop(r->af, &r->saddr_mask, sbufm, sizeof(sbufm)) ? sbufm : "::",
+            ntohs(r->sport_from),
+            ntohs(r->sport_to),
+            inet_ntop(r->af, &r->daddr, dbuf, sizeof(dbuf)) ? dbuf : "::",
+            inet_ntop(r->af, &r->daddr_mask, dbufm, sizeof(dbufm)) ? dbufm : "::",
+            ntohs(r->dport_from),
+            ntohs(r->dport_to));
+}
+#endif
+
+/**
+ * try lookup this_cpu_fdir_tbl by packet inner header tuple
+ *
+ *  <proto, saddr, sport, daddr, dport>.
+ *
+ * return r if found or NULL if not exist.
+ */
+struct cpu_fdir *
+cpu_fdir_get(uint8_t proto, rte_be32_t saddr, rte_be32_t daddr,
+    uint16_t sport, uint16_t dport)
+{
+    struct cpu_fdir *r;
+
+    if (!cpu_fdir_enable) {
+        return EDPVS_OK;
+    }
+
+    // only match first rule in list
+    list_for_each_entry(r, &g_cpu_fdir_tbl[rte_socket_id()], list) {
+        if ( r->proto == proto
+            && (dport >= r->dport_from && dport <= r->dport_to)
+            && ((r->daddr_mask & daddr) == r->daddr)) {
+            goto found;
+        }
+    }
+
+    return NULL;
+
+found:
+#ifdef CONFIG_DPVS_IPVS_DEBUG
+    cpu_fdir_show(r, "get");
+#endif
+    return r;
+}
+
+/**
+ * Forward the packet to the found redirect owner core.
+ */
+static int cpu_fdir_redirect_pkt(struct rte_mbuf *mbuf, lcoreid_t peer_cid)
+{
+    lcoreid_t cid = rte_lcore_id();
+    int ret;
+
+    ret = rte_ring_enqueue(cpu_fdir_ring[peer_cid][cid], mbuf);
+    if (ret < 0) {
+        RTE_LOG(ERR, CPU_FDIR,
+                "%s: [%d] failed to enqueue mbuf to cpu_fdir_ring[%d][%d]\n",
+                __func__, cid, peer_cid, cid);
+        return INET_DROP;
+    }
+
+#ifdef CONFIG_DPVS_IPVS_DEBUG
+    RTE_LOG(DEBUG, CPU_FDIR,
+            "%s: [%d] enqueued mbuf to cpu_fdir_ring[%d][%d]\n",
+            __func__, cid, peer_cid, cid);
+#endif
+
+    return INET_STOLEN;
+}
+
+
+
+int cpu_fdir_redirect(struct rte_mbuf *mbuf, int *redirected) {
+
+    pkt_info_t pkt_info;
+    pkt_info_init(&pkt_info, mbuf);
+
+    // always use inner headers for fdir
+    struct rte_ipv4_hdr *iph = pkt_info.iph;
+    if (!iph) {
+        return EDPVS_INVPKT;
+    }
+
+    uint16_t sport, dport = 0;
+
+    switch (pkt_info.l4_proto)
+    {
+    case IPPROTO_TCP:
+        sport = pkt_info.tcph->src_port;
+        dport = pkt_info.tcph->dst_port;
+        break;
+    case IPPROTO_UDP:
+        sport = pkt_info.udph->src_port;
+        dport = pkt_info.udph->dst_port;
+        break;
+    case IPPROTO_ICMP:
+        sport = 0;
+        dport = 0;
+    default:
+        return EDPVS_INVPKT;
+    }
+
+    struct cpu_fdir *r = cpu_fdir_get(pkt_info.l4_proto, 
+        iph->src_addr, iph->dst_addr, sport, dport);
+    if (r) {
+        cpu_fdir_redirect_pkt(mbuf, r->cid);
+        *redirected = 1;
+    }
+    
+    return EDPVS_OK;
+}
+
+// share spin lock with dataplan, only add/del during inialization!!
+static int cpu_fdir_rule_add(rte_be32_t dst, rte_be32_t d_mask,
+                        rte_be16_t dport_from, rte_be16_t dport_to,
+                        uint8_t proto, uint8_t cid)
+{
+    int i = 0;
+    struct cpu_fdir *r;
+
+    for (i = 0; i < get_numa_nodes(); i++) {
+        if (unlikely(rte_mempool_get(g_cpu_fdir_cache[i], (void **)&r) != 0)) {
+            RTE_LOG(WARNING, CPU_FDIR,
+                    "%s: no memory for cpu fdir add\n", __func__);
+            return EDPVS_NOMEM;
+        }
+
+        memset(r, 0, sizeof(struct cpu_fdir));
+        r->daddr = dst;
+        r->daddr_mask = d_mask;
+        r->dport_from = dport_from;
+        r->dport_to   = dport_to;
+        r->proto      = proto;
+        r->cid        = cid;
+
+        rte_spinlock_lock(&cpu_fdir_tbl_lock[i]);
+        list_add(&r->list, &g_cpu_fdir_tbl[i]);
+        rte_atomic32_inc(&num_fdir_rules[i]);
+        rte_spinlock_unlock(&cpu_fdir_tbl_lock[i]);
+    }
+
+    return EDPVS_OK;
+}
+
+static int cpu_fdir_rule_del(rte_be32_t dst, rte_be32_t d_mask,
+                        rte_be16_t dport_from, rte_be16_t dport_to,
+                        uint8_t proto)
+{
+    int i = 0;
+    struct cpu_fdir *r, *r_next;
+
+    for (i = 0; i < get_numa_nodes(); i++) {
+        rte_spinlock_lock(&cpu_fdir_tbl_lock[i]);
+
+        // only delete first rule in list
+        list_for_each_entry_safe(r, r_next, &g_cpu_fdir_tbl[rte_socket_id()], list) {
+            if ( r->proto == proto
+                && (dport_from == r->dport_from)
+                && (dport_to == r->dport_to)
+                && (dst == r->daddr)
+                && (d_mask == r->daddr_mask)) {
+                list_del(&r->list);
+                rte_atomic32_dec(&num_fdir_rules[i]);
+            }
+        }
+
+        rte_spinlock_unlock(&cpu_fdir_tbl_lock[i]);
+    }
+    return EDPVS_OK;
+}
+
+void cpu_fdir_ring_proc(lcoreid_t cid)
+{
+    struct rte_mbuf *mbufs[NETIF_MAX_PKT_BURST];
+    uint16_t nb_rb;
+    lcoreid_t peer_cid;
+
+    if (!cpu_fdir_enable) {
+        return;
+    }
+
+    cid = rte_lcore_id();
+
+    for (peer_cid = 0; peer_cid < DPVS_MAX_LCORE; peer_cid++) {
+        if (cpu_fdir_ring[cid][peer_cid]) {
+            nb_rb = rte_ring_dequeue_burst(cpu_fdir_ring[cid][peer_cid],
+                                           (void**)mbufs,
+                                           NETIF_MAX_PKT_BURST, NULL);
+            if (nb_rb > 0) {
+                lcore_process_packets(mbufs, cid, nb_rb, 1);
+            }
+        }
+    }
+}
+
+
+static int cpu_fdir_ring_create(void)
+{
+    char name_buf[RTE_RING_NAMESIZE];
+    int socket_id;
+    lcoreid_t cid, peer_cid;
+
+    socket_id = rte_socket_id();
+
+    for (cid = 0; cid < DPVS_MAX_LCORE; cid++) {
+        if (!netif_lcore_is_fwd_worker(cid)) {
+            continue;
+        }
+
+        for (peer_cid = 0; peer_cid < DPVS_MAX_LCORE; peer_cid++) {
+            if (!netif_lcore_is_fwd_worker(peer_cid)
+                || cid == peer_cid) {
+                continue;
+            }
+
+            snprintf(name_buf, RTE_RING_NAMESIZE,
+                     "cpu_fdir_ring[%d[%d]", cid, peer_cid);
+
+            cpu_fdir_ring[cid][peer_cid] =
+                rte_ring_create(name_buf, CPU_FDIR_RING_SIZE, socket_id,
+                                RING_F_SP_ENQ | RING_F_SC_DEQ);
+
+            if (!cpu_fdir_ring[cid][peer_cid]) {
+                RTE_LOG(ERR, CPU_FDIR,
+                        "%s: failed to create cpu_fdir_ring[%d][%d]\n",
+                        __func__, cid, peer_cid);
+                return EDPVS_NOMEM;
+            }
+        }
+    }
+
+    return EDPVS_OK;
+}
+
+static void cpu_fdir_ring_free(void)
+{
+    lcoreid_t cid, peer_cid;
+
+    for (cid = 0; cid < DPVS_MAX_LCORE; cid++) {
+        for (peer_cid = 0; peer_cid < DPVS_MAX_LCORE; peer_cid++) {
+            rte_ring_free(cpu_fdir_ring[cid][peer_cid]);
+        }
+    }
+}
+
+
+static int cpu_fdir_table_cache_alloc(void)
+{
+    int i;
+    char pool_name[32];
+
+    for (i = 0; i < get_numa_nodes(); i++) {
+        snprintf(pool_name, sizeof(pool_name), "cpu_fdir_%d", i);
+        g_cpu_fdir_cache[i] = rte_mempool_create(pool_name,
+                        CPU_FDIR_TBL_SIZE,
+                        sizeof(struct cpu_fdir),
+                        256, 0, NULL, NULL, NULL, NULL,
+                        i, 0);
+
+        if (!g_cpu_fdir_cache[i]) {
+            return EDPVS_NOMEM;
+        }
+    }
+
+    return EDPVS_OK;
+}
+
+static void cpu_fdir_table_cache_free(void)
+{
+    int i;
+
+    for (i = 0; i < get_numa_nodes(); i++) {
+        rte_mempool_free(g_cpu_fdir_cache[i]);
+    }
+}
+
+static int cpu_fdir_table_create(void)
+{
+    int i;
+
+    if (cpu_fdir_table_cache_alloc() != EDPVS_OK) {
+        goto cache_free;
+    }
+
+
+    g_cpu_fdir_tbl =
+        rte_malloc(NULL, sizeof(struct list_head ),
+                          RTE_CACHE_LINE_SIZE);
+    if (!g_cpu_fdir_tbl) {
+        goto cache_free;
+    }
+
+    /* per socket */
+    for (i = 0; i < get_numa_nodes(); i++) {
+        INIT_LIST_HEAD(&g_cpu_fdir_tbl[i]);
+        rte_spinlock_init(&cpu_fdir_tbl_lock[i]);
+    }
+
+    return EDPVS_OK;
+
+cache_free:
+    cpu_fdir_table_cache_free();
+    return EDPVS_NOMEM;
+}
+
+static void cpu_fdir_table_free(void)
+{
+    cpu_fdir_table_cache_free();
+
+    if (g_cpu_fdir_tbl) {
+        rte_free(g_cpu_fdir_tbl);
+    }
+}
+
+
+static int fdir_sockopt_set(sockoptid_t opt, const void *cf, size_t size)
+{
+    if (!cpu_fdir_enable) {
+        return EDPVS_NOTSUPP;
+    }
+
+    struct cpu_fdir_conf *conf = (struct cpu_fdir_conf*)cf;
+
+    if (!cf || size != sizeof(*conf))
+        return EDPVS_INVAL;
+    
+
+    uint32_t daddr_mask = htonl(0xFFFFFFFF << (32 - conf->d_plen));
+
+    switch (opt) {
+        case SOCKOPT_SET_FDIR_ADD:
+            return cpu_fdir_rule_add(conf->daddr, daddr_mask,
+                                htons(conf->dport_from), htons(conf->dport_to),
+                                conf->proto, conf->cid);
+        case SOCKOPT_SET_FDIR_DEL:
+            return cpu_fdir_rule_del(conf->daddr, daddr_mask,
+                                htons(conf->dport_from), htons(conf->dport_to),
+                                conf->proto);
+        default:
+            return EDPVS_NOTSUPP;
+    }
+    
+    return EDPVS_NOTSUPP;
+}
+
+static int fdir_sockopt_get(sockoptid_t opt, const void *conf, size_t size,
+                             void **out, size_t *outsize)
+{
+    if (!cpu_fdir_enable) {
+        return EDPVS_NOTSUPP;
+    }
+
+    struct cpu_fdir_conf_array *array;
+    size_t nrule;
+    struct cpu_fdir *r;
+    int off = 0;
+
+    nrule = rte_atomic32_read(&this_num_fdir_rules);
+    *outsize = sizeof(struct cpu_fdir_conf_array) + nrule * sizeof(struct cpu_fdir_conf);
+    *out = rte_calloc(NULL, 1, *outsize, 0);
+    if (!(*out))
+        return EDPVS_NOMEM;
+    array = (struct cpu_fdir_conf_array*)*out;
+
+    list_for_each_entry(r, &g_cpu_fdir_tbl[rte_socket_id()], list) {
+        if (off >= nrule) {
+            break;
+        }
+        memset(&array->rules[off], 0, sizeof(struct cpu_fdir_conf));
+        array->rules[off].proto = r->proto;
+        array->rules[off].cid = r->cid;
+        array->rules[off].daddr = r->daddr;
+        array->rules[off].d_plen = 32 - __builtin_clz(r->daddr_mask);
+        array->rules[off].dport_from = r->dport_from;
+        array->rules[off].dport_to = r->dport_to;
+        off++;
+    }
+    array->nrule = off;
+    return EDPVS_OK;
+}
+
+
+static struct dpvs_sockopts cpu_fdir_sockopts = {
+    .version        = SOCKOPT_VERSION,
+    .set_opt_min    = SOCKOPT_SET_FDIR_ADD,
+    .set_opt_max    = SOCKOPT_SET_FDIR_DEL,
+    .set            = fdir_sockopt_set,
+    .get_opt_min    = SOCKOPT_GET_FDIR_SHOW,
+    .get_opt_max    = SOCKOPT_GET_FDIR_SHOW,
+    .get            = fdir_sockopt_get,
+};
+
+int cpu_fdir_init(void)
+{
+    int err;
+
+    if (!cpu_fdir_enable) {
+        return EDPVS_OK;
+    }
+
+    err = cpu_fdir_ring_create();
+    if (err != EDPVS_OK) {
+        return err;
+    }
+
+    if ((err = sockopt_register(&cpu_fdir_sockopts)) != EDPVS_OK) {
+         return err;
+    }
+       
+
+    return cpu_fdir_table_create();
+}
+
+int cpu_fdir_term(void)
+{
+    int err;
+    if (!cpu_fdir_enable) {
+        return EDPVS_OK;
+    }
+
+    if ((err = sockopt_unregister(&cpu_fdir_sockopts)) != EDPVS_OK) {
+        return err;
+    }
+
+    cpu_fdir_ring_free();
+    cpu_fdir_table_free();
+
+    return EDPVS_OK;
+}
+
diff --git a/src/fdir/parser.c b/src/fdir/parser.c
new file mode 100644
index 0000000000000000000000000000000000000000..8dc136ed2c5fed27d4aba1010acc0f79703ad92a
--- /dev/null
+++ b/src/fdir/parser.c
@@ -0,0 +1,156 @@
+#include "fdir/parser.h"
+
+
+static void
+parse_ipv4(struct sk_buff_info* this_sk_info)
+{
+    struct rte_ipv4_hdr *ipv4_hdr = this_sk_info->iph;
+	this_sk_info->l3_len = rte_ipv4_hdr_len(ipv4_hdr);
+	this_sk_info->l4_proto = ipv4_hdr->next_proto_id;	/* only fill l4_len for TCP, it's useful for TSO */
+	if (this_sk_info->l4_proto == IPPROTO_TCP) {
+        this_sk_info->tcph = (struct rte_tcp_hdr *)
+            ((char *)ipv4_hdr + this_sk_info->l3_len);
+        this_sk_info->l4_len = (this_sk_info->tcph->data_off & 0xf0) >> 2;
+	} else if (this_sk_info->l4_proto == IPPROTO_UDP) {
+        this_sk_info->udph = (struct rte_udp_hdr *)
+            ((char *)ipv4_hdr + this_sk_info->l3_len);
+		this_sk_info->l4_len = sizeof(struct rte_udp_hdr);
+    } else if (this_sk_info->l4_proto == IPPROTO_ICMP) {
+		this_sk_info->icmph = (struct rte_icmp_hdr*)
+			((char *)ipv4_hdr + this_sk_info->l3_len);
+	} else 
+		this_sk_info->l4_len = 0;
+}
+
+static void
+update_tunnel_outer(struct sk_buff_info* this_sk_info)
+{
+	this_sk_info->is_tunnel = 1;
+	this_sk_info->outer_ethertype = this_sk_info->ethertype;
+	this_sk_info->outer_l2_len = this_sk_info->l2_len;
+	this_sk_info->outer_l3_len = this_sk_info->l3_len;
+	this_sk_info->outer_l4_proto = this_sk_info->l4_proto;
+	this_sk_info->outer_l2_hdr = this_sk_info->l2_hdr;
+    this_sk_info->outer_l3_hdr = this_sk_info->l3_hdr;
+    this_sk_info->outer_l4_hdr = this_sk_info->l4_hdr;
+}
+
+/*
+ * Parse an ethernet header to fill the ethertype, l2_len, l3_len and
+ * ipproto. This function is able to recognize IPv4 with optional VLAN
+ * headers.
+ */
+static void
+parse_ethernet(struct sk_buff_info* this_sk_info)
+{
+    struct rte_ether_hdr *eth_hdr = this_sk_info->eth;
+	struct rte_vlan_hdr *vlan_hdr;
+	this_sk_info->l2_len = sizeof(struct rte_ether_hdr);
+	this_sk_info->ethertype = this_sk_info->eth->ether_type;
+	while (this_sk_info->ethertype == _htons(RTE_ETHER_TYPE_VLAN) ||
+	       this_sk_info->ethertype == _htons(RTE_ETHER_TYPE_QINQ)) {
+		vlan_hdr = (struct rte_vlan_hdr *)
+			((char *)eth_hdr + this_sk_info->l2_len);
+		this_sk_info->l2_len  += sizeof(struct rte_vlan_hdr);
+		this_sk_info->ethertype = vlan_hdr->eth_proto;
+	}
+
+	switch (this_sk_info->ethertype) {
+		case _htons(RTE_ETHER_TYPE_IPV4):
+			this_sk_info->iph = (struct rte_ipv4_hdr *)
+					((char *)this_sk_info->eth + this_sk_info->l2_len);
+			parse_ipv4(this_sk_info);
+			break;
+		default:
+			this_sk_info->l4_len = 0;
+			this_sk_info->l3_len = 0;
+			this_sk_info->l4_proto = 0;
+			break;
+	}
+}
+
+/* Parse a geneve header */
+static void
+parse_geneve(struct sk_buff_info* this_sk_info)
+{
+    struct rte_geneve_hdr *genh;	/* Check udp destination port. */
+	if (this_sk_info->udph->dst_port != _htons(RTE_GENEVE_DEFAULT_PORT)) {
+		return;
+	}
+		
+	genh = (struct rte_geneve_hdr *)((char *)this_sk_info->udph +
+				sizeof(struct rte_udp_hdr));
+    this_sk_info->genh = genh;
+    this_sk_info->tunnel_type = TUNNEL_GENEVE;
+	this_sk_info->tunnel_len = sizeof(struct rte_geneve_hdr) + this_sk_info->genh->opt_len * 4;
+	if (!this_sk_info->genh->proto || this_sk_info->genh->proto ==
+	    _htons(RTE_ETHER_TYPE_IPV4)) {
+		update_tunnel_outer(this_sk_info);
+		this_sk_info->iph = (struct rte_ipv4_hdr *)((char *)this_sk_info->genh +
+			   this_sk_info->tunnel_len);
+		parse_ipv4(this_sk_info);
+		this_sk_info->ethertype = _htons(RTE_ETHER_TYPE_IPV4);
+		this_sk_info->l2_len = 0;
+	} else if (this_sk_info->genh->proto == _htons(RTE_GENEVE_TYPE_ETH)) {
+		update_tunnel_outer(this_sk_info);
+		this_sk_info->eth = (struct rte_ether_hdr *)((char *)this_sk_info->genh +
+			  this_sk_info->tunnel_len);
+		parse_ethernet(this_sk_info);
+	} else {
+		return;
+	}
+		
+	this_sk_info->recv_vni = genh->vni[0]<<16 | genh->vni[1]<<8 | genh->vni[2];
+    this_sk_info->vni_flag = genh->reserved1 & 0x3f; // lower 6 bit
+    /* l2_len must be set correctly for nic offload */
+	this_sk_info->l2_len += this_sk_info->tunnel_len + sizeof(struct rte_udp_hdr);
+}
+
+/* Parse a vxlan header */
+static void
+parse_vxlan(struct sk_buff_info* this_sk_info)
+{
+	/* check udp destination port, RTE_VXLAN_DEFAULT_PORT (4789) is the
+	 * default vxlan port (rfc7348) or that the rx offload flag is set
+	 * (i40e only currently)
+	 */
+	if (this_sk_info->udph->dst_port != _htons(RTE_VXLAN_DEFAULT_PORT) &&
+		RTE_ETH_IS_TUNNEL_PKT(this_sk_info->mbuf->packet_type) == 0) {
+			return;
+		}
+
+	update_tunnel_outer(this_sk_info);
+    this_sk_info->tunnel_type = TUNNEL_VXLAN;
+    this_sk_info->tunnel_len = RTE_ETHER_VXLAN_HLEN;
+    this_sk_info->vxlanh = (struct rte_vxlan_hdr *)((char *)this_sk_info->udph +
+		sizeof(struct rte_udp_hdr));
+	this_sk_info->eth = (struct rte_ether_hdr *)((char *)this_sk_info->vxlanh +
+		sizeof(struct rte_vxlan_hdr));
+	parse_ethernet(this_sk_info);
+    this_sk_info->recv_vni = rte_be_to_cpu_32(this_sk_info->vxlanh->vx_vni);
+    this_sk_info->vni_flag = this_sk_info->vxlanh->vx_flags;
+	this_sk_info->l2_len += this_sk_info->tunnel_len; /* add udp + vxlan */
+}
+
+void pkt_info_init(pkt_info_t* pkt_info, struct rte_mbuf* mbuf)
+{
+ 	memset(pkt_info, 0, sizeof(struct sk_buff_info));
+ 	pkt_info->mbuf   = mbuf;
+    pkt_info->l2_hdr = rte_pktmbuf_mtod(pkt_info->mbuf, struct rte_ether_hdr*);
+	parse_ethernet(pkt_info);
+
+     /* check if it's a supported tunnel */
+    if (pkt_info->l4_proto == IPPROTO_UDP) {
+         parse_vxlan(pkt_info);
+         if (pkt_info->is_tunnel) {
+             goto tunnel_ok;
+         }
+         parse_geneve(pkt_info);
+         if (pkt_info->is_tunnel) {
+             goto tunnel_ok;
+         }
+     }
+
+ tunnel_ok:
+    return;
+}
diff --git a/src/main.c b/src/main.c
index 00ca8965593eaa41818d3158b3c670412505ff4e..93f49638213b7eb5341092c522d8a5cf2dc88daa 100644
--- a/src/main.c
+++ b/src/main.c
@@ -49,6 +49,7 @@
 #include "eal_mem.h"
 #include "scheduler.h"
 #include "pdump.h"
+#include "fdir/cpu_fdir.h"
 
 #define DPVS    "dpvs"
 #define RTE_LOGTYPE_DPVS RTE_LOGTYPE_USER1
@@ -107,6 +108,8 @@ static void inline dpdk_version_check(void)
                     iftraf_init,         iftraf_term),          \
         DPVS_MODULE(MODULE_EAL_MEM,     "eal_mem",              \
                     eal_mem_init,        eal_mem_term),         \
+        DPVS_MODULE(MODULE_CPU_FDIR,     "cpu_fdir",            \
+                    cpu_fdir_init,     cpu_fdir_term),          \
         DPVS_MODULE(MODULE_LAST,        "last",                 \
                     NULL,                NULL)                  \
     }
diff --git a/src/netif.c b/src/netif.c
index d2ae6cf91da116ee7ccb3981eb1d40122da33d6b..85b0a0f44e6f636dd4bc3444ecc5aa42e5fd3078 100644
--- a/src/netif.c
+++ b/src/netif.c
@@ -44,6 +44,7 @@
 #include <netinet/in.h>
 #include <arpa/inet.h>
 #include <ipvs/redirect.h>
+#include <fdir/cpu_fdir.h>
 #ifdef CONFIG_ICMP_REDIRECT_CORE
 #include "icmp.h"
 #endif
@@ -2373,6 +2374,12 @@ static int netif_deliver_mbuf(struct netif_port *dev, lcoreid_t cid,
     /* reuse mbuf.packet_type, it was RTE_PTYPE_XXX */
     mbuf->packet_type = eth_type_parse(eth_hdr, dev);
 
+    int redirected = 0;
+    ret = cpu_fdir_redirect(mbuf, &redirected);
+    if (redirected) {
+        return ret;
+    }
+
     /*
      * In NETIF_PORT_FLAG_FORWARD2KNI mode.
      * All packets received are deep copied and sent to KNI
@@ -2569,6 +2576,11 @@ static void lcore_process_redirect_ring(lcoreid_t cid)
     dp_vs_redirect_ring_proc(cid);
 }
 
+static void lcore_process_fdir_ring(lcoreid_t cid)
+{
+    cpu_fdir_ring_proc(cid);
+}
+
 static void lcore_job_recv_fwd(void *arg)
 {
     int i, j;
@@ -2588,6 +2600,7 @@ static void lcore_job_recv_fwd(void *arg)
 
             lcore_process_arp_ring(cid);
             lcore_process_redirect_ring(cid);
+            lcore_process_fdir_ring(cid);
             qconf->len = netif_rx_burst(pid, qconf);
 
             lcore_stats_burst(&lcore_stats[cid], qconf->len);
diff --git a/tools/dpip/Makefile b/tools/dpip/Makefile
index e1bbe21e4390cde15d2e27a9d904756ade42afb0..b8771fb2123e358f872bca1bf61a7835fd10cb5c 100644
--- a/tools/dpip/Makefile
+++ b/tools/dpip/Makefile
@@ -40,7 +40,7 @@ DEFS = -D DPVS_MAX_LCORE=64 -D DPIP_VERSION=\"$(VERSION_STRING)\"
 CFLAGS += $(DEFS)
 
 OBJS = ipset.o dpip.o utils.o route.o addr.o neigh.o link.o vlan.o \
-	   qsch.o cls.o tunnel.o ipset.o ipv6.o iftraf.o eal_mem.o flow.o \
+	   qsch.o cls.o tunnel.o ipset.o ipv6.o iftraf.o eal_mem.o flow.o fdir.o\
 	   ../../src/common.o ../keepalived/keepalived/check/sockopt.o
 
 all: $(TARGET)
diff --git a/tools/dpip/dpip.c b/tools/dpip/dpip.c
index 596a534f54c9b185d69f5f72c80271ae9ec3f587..069215e4a203d5bb69ded33d47b33a2ef4157646 100644
--- a/tools/dpip/dpip.c
+++ b/tools/dpip/dpip.c
@@ -35,7 +35,7 @@ static void usage(void)
         "    "DPIP_NAME" [OPTIONS] OBJECT { COMMAND | help }\n"
         "Parameters:\n"
         "    OBJECT  := { link | addr | route | neigh | vlan | tunnel | qsch | cls |\n"
-        "                 ipv6 | iftraf | eal-mem | ipset | flow }\n"
+        "                 ipv6 | iftraf | eal-mem | ipset | flow | fdir }\n"
         "    COMMAND := { create | destroy | add | del | show (list) | set (change) |\n"
         "                 replace | flush | test | enable | disable }\n"
         "Options:\n"
diff --git a/tools/dpip/fdir.c b/tools/dpip/fdir.c
new file mode 100644
index 0000000000000000000000000000000000000000..be10b921f5f4b36462ea9ba19879507e00262f23
--- /dev/null
+++ b/tools/dpip/fdir.c
@@ -0,0 +1,144 @@
+#include <stdlib.h>
+#include "sockopt.h"
+#include "dpip.h"
+#include "conf/common.h"
+#include "conf/cpu_fdir.h"
+
+
+static void fdir_help(void)
+{
+    fprintf(stderr,
+        "Usage:\n"
+        "    dpip fdir { show | help }\n"
+        "    dpip fdir { add | del } RULE\n"
+        "Parameters:\n"
+        "    RULE       := cid CID daddr DADDR dp_from DPORT_FROM dp_to DPORT_TO proto PROTO\n"
+        "    CID        := { NUM }\n"
+        "    SADDR      := { ip/plen }\n"
+        "    DADDR      := { ip/plen }\n"
+        "    SPORT_FROM := { NUM }\n"
+        "    SPORT_TO   := { NUM }\n"
+        "    DPORT_FROM := { NUM }\n"
+        "    DPORT_TO   := { NUM }\n"
+        "    PROTO      := { 6 | 17 | 1 }\n"
+        "Examples:\n"
+        "    dpip fdir show\n"
+        "    dpip fdir add cid 5 daddr 172.0.0.0/16 dp_from 1000 dp_to 2000 proto 6\n"
+        "    dpip fdir del daddr 172.0.0.0/16 dp_from 1000 dp_to 2000 proto 6\n"
+        );
+}
+
+
+static void fdir_dump(struct cpu_fdir_conf *rule)
+{
+    char dst[64], src[64];
+
+    printf("cid %u dst %s/%d dport %u-%u proto %d\n",
+            rule->cid,
+            inet_ntop(AF_INET, &rule->daddr, dst, sizeof(dst)) ? dst : "::",
+            rule->d_plen,
+            ntohs(rule->dport_from),
+            ntohs(rule->dport_to),
+            rule->proto);
+    return;
+}
+
+static int fdir_parse_args(struct dpip_conf *conf,
+                            struct cpu_fdir_conf *rule)
+{
+    char *addr, *plen;
+    union inet_addr tmp;
+    memset(rule, 0, sizeof(*rule));
+    while (conf->argc > 0) {
+        if (strcmp(conf->argv[0], "cid") == 0) {
+            NEXTARG_CHECK(conf, "cid");
+            rule->cid = atoi(conf->argv[0]);
+        } else if (strcmp(conf->argv[0], "daddr") == 0) {
+            NEXTARG_CHECK(conf, "daddr");
+            addr = conf->argv[0];
+            if ((plen = strchr(addr, '/')) != NULL)
+                *plen++ = '\0';
+
+            int af = AF_INET;
+            if (inet_pton_try(&af, addr, &tmp) <= 0)
+                return -1;
+
+            rule->daddr = tmp.in.s_addr;
+            rule->d_plen = plen ? atoi(plen) : 0;
+        } else if (strcmp(conf->argv[0], "dp_from") == 0) {
+            NEXTARG_CHECK(conf, "dp_from");
+            rule->dport_from = atoi(conf->argv[0]);
+        } else if (strcmp(conf->argv[0], "dp_to") == 0) {
+            NEXTARG_CHECK(conf, "dp_to");
+            rule->dport_to = atoi(conf->argv[0]);
+        } else if (strcmp(conf->argv[0], "proto") == 0) {
+            NEXTARG_CHECK(conf, "proto");
+            rule->proto = atoi(conf->argv[0]);
+        }
+        NEXTARG(conf);
+    }
+
+    if (conf->argc > 0) {
+        fprintf(stderr, "too many arguments\n");
+        return -1;
+    }
+
+
+    return 0;
+}
+
+static int fdir_do_cmd(struct dpip_obj *obj, dpip_cmd_t cmd,
+                        struct dpip_conf *conf)
+{
+    struct cpu_fdir_conf rule;
+    struct cpu_fdir_conf_array *array;
+    size_t size, i;
+    int err;
+
+    if (fdir_parse_args(conf, &rule) != 0)
+        return EDPVS_INVAL;
+
+    switch (conf->cmd) {
+        case DPIP_CMD_ADD:
+            return dpvs_setsockopt(SOCKOPT_SET_FDIR_ADD, &rule, sizeof(rule));
+        case DPIP_CMD_DEL:
+            return dpvs_setsockopt(SOCKOPT_SET_FDIR_DEL, &rule, sizeof(rule));
+        case DPIP_CMD_SHOW:
+            err = dpvs_getsockopt(SOCKOPT_GET_FDIR_SHOW, &rule, sizeof(rule),
+                        (void **)&array, &size);
+            if (err != 0)
+                return err;
+            if (size < sizeof(*array)
+                || size != sizeof(*array) + \
+                           array->nrule * sizeof(struct cpu_fdir_conf)) {
+                fprintf(stderr, "corrupted response.\n");
+                dpvs_sockopt_msg_free(array);
+                return EDPVS_INVAL;
+            }
+            for (i = 0; i < array->nrule; i++) {
+                fdir_dump(&array->rules[i]);
+            }
+                
+            dpvs_sockopt_msg_free(array);
+            return EDPVS_OK;
+        default:
+            return EDPVS_NOTSUPP;  
+    }
+}
+
+struct dpip_obj dpip_fdir = {
+    .name   = "fdir",
+    .help   = fdir_help,
+    .do_cmd = fdir_do_cmd,
+};
+
+static void __init fdir_init(void)
+{
+    dpip_register_obj(&dpip_fdir);
+}
+
+static void __exit fdir_exit(void)
+{
+    dpip_unregister_obj(&dpip_fdir);
+}
+

```

