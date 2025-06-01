---
title: "dpdk转发面trace"
date: 2025-05-27T15:54:00+08:00
draft: false
tags: ["dpdk", "高性能网络"]
tags_weight: 66
series: ["dpdk系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
---

<!-- more -->

上一期分析了 ebpf 转发面通过 linux perf event 的思路进行 trace，这一期介绍一种 dpdk 程序的 trace 方法。基本原理大致相通，也是通过共享内存的方式进行数据传输。转发面代码和工具代码通过 mmap 共享一段内存，转发面产生数据，工具代码消费数据。

![ptrace原理](/img/blobs/ptrace原理.png)

### 共享内存 buffer

由于大部分 dpdk 程序是 run to completion 模型，一个 lcore 对应一个网卡队列，报文尽可能不要在 cpu 之间来回切换，而是由单个 cpu 处理完所有逻辑之后发送。因此，相对应的，我们的 buffer 要为每一个 cpu 都分配一个 ring

```c
extern struct trace_buffer *g_trace;

typedef OBJ_RING(struct trace_record, 1 << 11) __rte_cache_aligned trace_ring_t;

struct trace_buffer {
    trace_ring_t percpu[16];
    int enabled;
    uint8_t ports[MAX_PORT][NR_TRACE_ON];
};
```

### ring buffer

常规做法是使用 dpdk lib 的 rte_ring，但是该 ring 的实现比较复杂，且很多功能我们不会用到。因此下面介绍一个简易的 ring 实现，相比 dpdk rte_ring，内存占用较少，逻辑简单。

核心思路

- 使用内存屏障`rte_smp_wmb()/rte_smp_rmb()`替代锁，保证线程安全
- 使用 prepare，commit 两段式提交，确保数据一致性
- 使用 mask 位运算代替取模获取索引，提升查找效率

具体实现代码如下

```c
#define OBJ_RING(obj_type, ring_size) \
	struct { \
		uint64_t next_seq; \
		obj_type buffer[IS_POWER_OF_2(ring_size) ? \
		    (int)((ring_size) + MEMBER_TYPE_ASSERT(obj_type, seq, uint64_t)) : -1]; \
	}

#define obj_ring_type(ring)             typeof((ring)->buffer[0])
#define obj_ring_mask(ring)             (ARRAY_SIZE((ring)->buffer) - 1)
#define obj_ring_at(ring, seq)  (&(ring)->buffer[(seq) & obj_ring_mask(ring)])

#define obj_ring_init(ring) do { \
		obj_ring_type(ring) *_obj; \
		(ring)->next_seq = 0; \
		array_for_each(_obj, (ring)->buffer) \
		_obj->seq = 0; \
} while (0)

#define obj_ring_write_prepare(ring) ({ \
		obj_ring_type(ring) *_obj = obj_ring_at((ring), (ring)->next_seq); \
		_obj->seq = (ring)->next_seq; \
		rte_smp_wmb(); \
		_obj; \
	})

#define obj_ring_write_commit(ring) do { \
		rte_smp_wmb(); \
		(ring)->next_seq++; \
} while (0)

#define obj_ring_next_seq(ring) ACCESS_ONCE((ring)->next_seq)

#define obj_ring_read(ring, nseq, res) ({ \
		uint64_t _seq = (nseq); \
		obj_ring_type(ring) *_res = NULL; \
		if (_seq < obj_ring_next_seq(ring)) { \
			obj_ring_type(ring) *_obj = obj_ring_at((ring), _seq); \
			_res = (res); \
			do { \
				_seq = ACCESS_ONCE(_obj->seq); \
				while (_seq >= obj_ring_next_seq(ring)) {} \
				rte_smp_rmb(); \
				*_res = ACCESS_ONCE(*_obj); \
				rte_smp_rmb(); \
				_res->seq = ACCESS_ONCE(_obj->seq); \
			} while (_seq != _res->seq); \
		} \
		_res; \
	})

```

使用方式

```c
//prepare
obj_ring_write_prepare(ring);
    _obj = obj_ring_at(ring, next_seq); // 获取写入位置
    _obj->seq = next_seq;              // 设置有效标记
    rte_smp_wmb();                     // 写内存屏障保证可见性

// 构造record

//commit
obj_ring_write_commit(ring);
    rte_smp_wmb();      // 确保数据完全写入后执行
    next_seq++;         // 递增序列号提交写入
```

### data in memory file

转发面的表项 mmap 到文件，之后用工具读取文件并解析，是 dpdk 转发面开发中很常见的场景，比如路由表，邻居表的 dump。

相较于用 unix socket 传输，直接读取内存文件不会有速率的限制，且实现起来比较简单。

另一个优点是表项文件可以保留在主机上，当进程发生异常重启时，初始化阶段可以通过内存文件 restore 所有表项。

但是 trade off 是要处理好并发读写的问题。

我们可以定义一种通用的 header 带在数据前面方便我们解析

- magic：魔术字，进行解析之前校验该特征
- version: 版本，预留
- data_size: data[]部分的长度
- file_size：包含 header 的整个内存文件的大小，按 page size 取整
- type: 定义文件时只读还是可写

```c
struct dmf_header {
	uint32_t magic;
	uint32_t version;
	uint64_t data_size;
	uint64_t file_size;     /* PAGE_SIZE aligned */
	u8       initialized;   /* flag to indicate whether the file is initialized */
	char name[DMF_SPC_HDR_NAME_LEN];
	u8       type;
	char reserved[18];

	char data[];
};


```

创建 dmf 映射时，我们可以定义一种通用的 spec

```c
struct dmf_spec {
	struct pal_hlist_node   hnode;
	obj_id_t                   id;

	char name[DMF_SPC_HDR_NAME_LEN];
	uint32_t version;
	uint32_t page_shift;  /* 内存页大小 */
	int reset_level;

	uint64_t addr;    /* 用户态代码中的虚拟内存地址 */
	size_t   head_padding;
	size_t   data_size;
};
```

使用时指定好 spec 就可以进行内存文件映射

```c
static struct dmf_spec dmf_specs[] = {
	{{NULL, NULL}, OBJ_ID_NULL, "fp/trace",         0x20200320, DMF_PAGE_SHIFT, DMF_RL_DEL, 0x6670ull << 32, 0,   sizeof(*g_trace)},
};

void mvs_dp_dmf_map_all(int readonly)
{
	g_trace      = dmf_map("fp/trace", 0);
}
```

dmf_map 流程

1. 寻找文件路径，若文件存在，mmap 整个文件到 spec->addr
2. 若文件不存在，创建文件再 mmap
3. 检查 header 的格式，若不合法，reset data

```c
void *dmf_map_by_spec(struct dmf_spec *spec, const char *name, int readonly)
{
	char path[PATH_MAX];
	void *data = NULL;

	dmf_file_path(spec, path, sizeof(path));
	if (access(path, W_OK | R_OK) < 0) {
		if (readonly) {
			PAL_ERROR("dmf: file not exist: %s\n", name);
			goto out;
		}

		if (dmf_create(name) < 0) {
			goto out;
		}
	}

	data = dmf_map_file(path, spec, readonly);

out:
	return data;
}

int dmf_create(const char *name)
{
	struct dmf_spec *spec;

	spec = dmf_spec_find(name);
	if (!spec) {
		PAL_ERROR("dmf: no spec found for %s\n", name);
		return -1;
	}

	return dmf_do_create(spec);
}

int dmf_do_create(struct dmf_spec *spec)
{
	struct dmf_header hdr;
	size_t data_size;
	size_t file_size;
	size_t page_size;
	char path[PATH_MAX];
	int ret = 0;

	data_size = spec->data_size;
	page_size = 1ull << spec->page_shift;
	file_size = ROUND_UP_TO_PAGE_SIZE(sizeof(struct dmf_header) + data_size +
					  spec->head_padding, spec->page_shift);

	PAL_DEBUG("spec data_size 0x%x, file_size = 0x%x", data_size, file_size);

	dmf_file_path(spec, path, sizeof(path));
	if (dmf_mkdir_p(path) < 0) {
        return -1;
	}

	dmf_hdr_reset_hard(&hdr, spec, file_size, DMF_FILE_TYPE_FIXD_STATIC);
	if (DMF_IS_1G_HUGEPAGE(spec) || DMF_IS_2M_HUGEPAGE(spec)) {
		ret = dmf_create_hugetlbfs(&hdr, path, file_size, spec->head_padding);
	} else {
		ret = dmf_create_tmpfs(&hdr, path, file_size, page_size, spec->head_padding);
	}
	if (ret < 0) {
		return ret;
	}

	PAL_LOG("dmf: created file %s, file_size=%zd, page_size=%zd\n",
		path, file_size, page_size);
	return 0;
}

static int dmf_create_tmpfs(struct dmf_header *hdr, const char *path,
		 size_t file_size, size_t page_size,
		 size_t head_padding)
{
	char *data = NULL;
	int fd = -1;

	ASSERT(head_padding <= (page_size - sizeof(*hdr)));
	data = malloc(page_size);
	if (!data) {
		goto err;
	}
	memset(data, 0, page_size);

	fd = open(path, O_WRONLY | O_TRUNC | O_CREAT, 0644);
	if (fd < 0) {
		goto err;
	}

	while (file_size) {
		size_t bytes = dmf_min(page_size, file_size);
		if ((size_t)write(fd, data, bytes) != bytes) {
			goto err;
		}

		file_size -= bytes;
	}

	if (lseek(fd, head_padding, SEEK_SET) == -1) {
		goto err;
	}

	if ((size_t)write(fd, hdr, sizeof(*hdr)) != sizeof(*hdr)) {
		goto err;
	}

	close(fd);
	free(data);

	return 0;

err:
	PAL_ERROR("dmf: failed to create file: %s: %s\n",
		  path, strerror(errno));

	if (fd >= 0) {
		close(fd);
	}
	if (data) {
		free(data);
	}

	return -1;
}

static int dmf_create_hugetlbfs(struct dmf_header *hdr, const char *path,
		     size_t file_size, size_t head_padding)
{
	void *addr;
	int fd;

	ASSERT(head_padding < (file_size - sizeof(*hdr)));

	fd = open(path, O_CREAT | O_RDWR, 0600);
	if (fd < 0) {
		return -1;
	}

	/* do ftruncate first to avoid mmap failed */
	ftruncate(fd, file_size);

	addr = mmap(NULL, file_size, PROT_READ | PROT_WRITE,
		    MAP_SHARED | MAP_POPULATE, fd, 0);
	close(fd);

	if (addr == MAP_FAILED) {
		PAL_ERROR("dmf: failed to create file: %s: %s\n",
			  path, strerror(errno));
		return -1;
	}

	memcpy((char *)addr + head_padding, hdr, sizeof(*hdr));
	munmap(addr, file_size);
	return 0;
}

void * dmf_map_file(char *path, struct dmf_spec *spec, int readonly)
{
	size_t file_size;
	struct dmf_header *hdr;

	BUILD_BUG_ON(sizeof(*hdr) != 128);
	hdr = dmf_do_map(path, spec, &file_size, readonly);
	if (!hdr) {
		PAL_ERROR("dmf: failed to map %s: %s\n", spec->name, strerror(errno));
		goto err;
	}

	if (file_size < spec->data_size) {
		PAL_ERROR("dmf: file_size %zd < data_size %zd\n",
			  file_size, spec->data_size);
		goto err;
	}

	if (dmf_need_reset(hdr, spec)) {
		if (readonly) {
			goto err;
		}

		hdr->data_size = spec->data_size;
		hdr->file_size = file_size;
		hdr->version   = spec->version;
	}

	return hdr->data;
err:
	return NULL;
}

static inline void * dmf_do_map(const char *path, struct dmf_spec *spec,
	   size_t *file_size, int readonly)
{
	uint8_t *ptr;
	struct stat st;
	int fd = -1;

	fd = open(path, O_RDWR, 0644);
	if (fd < 0) {
		PAL_ERROR("dmf: failed to open file %s: %s\n", path, strerror(errno));
		goto err;
	}

	if (fstat(fd, &st) != 0) {
		PAL_ERROR("dmf: fstat failed: %s: %s\n", path, strerror(errno));
		goto err;
	}

	/* map the whole file, even we may use part of them; ct is one example. */
	ptr = mmap((void *)(uintptr_t)spec->addr, st.st_size,
		   readonly ? PROT_READ : PROT_READ | PROT_WRITE,
		   MAP_SHARED | MAP_POPULATE, fd, 0);
	if (ptr == MAP_FAILED) {
		PAL_ERROR("dmf: failed to map file %s: %s\n", path, strerror(errno));
		goto err;
	}

	if (spec->addr && (uintptr_t)ptr != spec->addr) {
		PAL_ERROR("dmf: requested map addr 0x%lx, but mapped at 0x%lx\n",
			  spec->addr, (intptr_t)ptr);
		goto err;
	} else {
		spec->addr = (uintptr_t)ptr;
	}

	*file_size = st.st_size;

	close(fd);

	return ptr + spec->head_padding;

err:
	if (fd >= 0) {
		close(fd);
	}
	return NULL;
}

static inline int dmf_need_reset(struct dmf_header *hdr, struct dmf_spec *spec)
{
	const char *name = spec->name;

	if (hdr->magic != DMF_MAGIC) {
		PAL_WARNING("dmf: %s: magic mismatch: hdr magic 0x%08x != 0x%08x\n",
			    name, hdr->magic, DMF_MAGIC);
		return 1;
	}

	if (hdr->data_size != spec->data_size) {
		PAL_WARNING("dmf: %s: data_size mismatch: hdr data_size %zd != %zd\n",
			    name, hdr->data_size, spec->data_size);
		return 1;
	}

	/* '\0' is included */
	if (memcmp(hdr->name, name, dmf_min(strlen(name) + 1, sizeof(hdr->name))) != 0) {
		PAL_WARNING("dmf: %s: name mismatch: hdr name %s\n",
			    name, hdr->name);
		return 1;
	}

	if (hdr->version != spec->version) {
		PAL_WARNING("dmf: %s version mismatch: hdr version 0x%08x != 0x%08x\n",
			    name, hdr->version, spec->version);
		return 1;
	}

	return 0;
}
```

### trace record

一条 trace_record 需要记录了一条流的完整信， 包括 rx 和 tx 时的 skb 信息，cpu id，经过的所有 trace points 以及必要的 info

```c
/* trace的skb只记录必要的header信息*/
struct trace_skb {
    char data[192];
    uint32_t pkt_len: 31;
    uint32_t has_eth_hdr: 1;
    uint16_t recv_if;
    uint16_t send_if;
    uint32_t vpcid;
    uint16_t segsz;
    uint16_t l2_len;
    uint16_t l3_len;
    uint16_t l4_len;
    uint64_t mbuf_oflag;
};

struct trace_skbinfo {
    uint16_t  init: 1,
         tcp_state: 4,
         reserved: 11;
    uint16_t action_cnt[DIR_COUNT]; // ct最终执行的action个数
    ct_tuple_t tuple[DIR_COUNT];  // ct五元组信息
    uint32_t vmip;
    obj_id_t ct_id; // conntrack id
};

struct trace_point {
    short ctx;  // 观测点
    short err;  // 错误码
    uint32_t tsc; // 时间戳
};

struct trace_record {
    uint64_t seq;
    uint64_t rx_tsc;
    uint32_t flags;
    uint16_t tid;
    uint16_t nr_point;
    struct trace_point points[32];
    struct trace_skb skbs[2];  // rx和tx时的skb
    struct trace_skbinfo skbinfo; // 扩展信息
};

```

### trace 语法

下面是一个典型使用样例

```c
packet rx
TRACE_BEGIN()
    do logic
        ...
TRACE_POINT()
   do logic
        ...
TRACE_ERROR()
   do logic
        ...
TRACE_SEND()
TRACE_END()
packet tx
```

- trace_begin

  - 初始化 trace_record
  - 拷贝 ingress 时的 skb

  ```c
  static inline struct trace_record *
  trace_begin(struct sk_buff *skb, int ctx,
          int has_eth_hdr)
  {
      struct trace_record *record;
      trace_ring_t *ring;
      trace_filter_t trace_filter = g_trace_filter;
      int tid;

      if (unlikely(trace_filter && trace_filter(skb, ctx, has_eth_hdr) != 0)) {
          return NULL;
      }

      tid = pal_thread_id();

      ring = get_trace_ring(tid);
      if (unlikely(ring == NULL)) {
          return NULL;
      }

      record = obj_ring_write_prepare(ring);
      record->rx_tsc = pal_thread_conf(tid)->start_cycle;
      record->flags = 0;
      record->tid = tid;
      record->nr_point = 0;
      record->skbinfo.tcp_state = 0;
      record->skbinfo.ct_id = OBJ_ID_NULL;

      record->skbs[0].pkt_len = 0;
      record->skbs[1].pkt_len = 0;

      if (trace_enabled(skb->recv_if, TRACE_ON_POINTS)) {
          trace_add_point(record, ctx, 0);
      }

      if (trace_enabled(skb->recv_if, TRACE_ON_RX)) {
          trace_set_skb(record, skb, 0, has_eth_hdr);
          trace_set_skbinfo(&record->skbinfo, NULL);
      }

      skb->trace = record;

      return record;
  }
  ```

- trace_point

  - 为 trace_record 的 points 增加一个观测点

    ```c
    static inline void
    trace_point(struct trace_record *record, int ctx, int err)
    {
        if (record->nr_point && record->nr_point < ARRAY_SIZE(record->points)) {
            trace_add_point(record, ctx, err);
        }
    }
    ```

- trace_error

  - 为 trace_record 的 points 增加一个带错误码的观测点

- trace_end

  - 增加 end 观测点

  - 将 record 写到 cpu 对应的 ring

    ```c
    static inline void
    trace_end(struct trace_record *record, int err)
    {
        trace_ring_t *ring;

        if (!record) {
            return;
        }

        if (record->skbs[0].pkt_len == 0 && record->skbs[1].pkt_len == 0) {
            return;
        }

        trace_point(record, TRACE_POINT_end, err);

        ring = get_trace_ring(record->tid);
        if (ring) {
            obj_ring_write_commit(ring);
        }
    }
    ```

- trace_send

  - 增加一个观测点
  - 拷贝 egress 时的 skb

  ```c
  static inline void
  trace_send(struct sk_buff *skb, int ctx)
  {
      struct trace_record *record = skb->trace;

      trace_point(record, ctx, 0);

      if (trace_enabled(skb->send_if, TRACE_ON_TX)) {
          trace_set_skb(record, skb, 1, 1);  // trace tx
      }
  }
  ```

### trace_reader

reader 打开内存文件读取 record，每一个 ring 相应的需要创建对应的 cache。

由于 dump 打印到标准输出的速度较慢，大流量场景下，cache 可能会被打满，因此需要 read 和 lost 统计

按照 trace_option 对 trace_record 进行过滤

```c
struct trace_reader {
	struct trace_cache percpu[ARRAY_SIZE(g_trace->percpu)];
	uint64_t start_time_usec;
	uint64_t start_time_tsc;
	size_t nr_read;
	size_t nr_lost;
	size_t nr_filtered;
	struct trace_option option;
};
```

record 解析流程

```c

static void *trace_skb_header(struct trace_skb *skb, uint16_t offset, uint16_t size)
{
	uint16_t data_len = RTE_MIN((size_t)skb->pkt_len, sizeof(skb->data));

	if (offset >= data_len || offset + size > data_len)
		return NULL;

	return skb->data + offset;
}

#define TRACE_SKB_HEADER(skb, offset, type) \
	((type *)trace_skb_header(skb, offset, sizeof(type)))


#define VALUE_IN_RANGE(v, min, max)	((v) >= (min) && (v) <= (max))

static int match_addr(struct addr_filter *af, u32 ip, u16 port)
{
	if (!VALUE_IN_RANGE(ip, af->ip_min, af->ip_max))
		return 0;
	if (!VALUE_IN_RANGE(port, af->port_min, af->port_max))
		return 0;
	return 1;
}

static int match_tuple(struct tuple_filter *tf, u32 sip, u32 dip, u16 sport, u16 dport)
{
	if (match_addr(&tf->src, sip, sport) && match_addr(&tf->dst, dip, dport))
		return 1;
	if (tf->nr_dir == 1)
		return 0;
	if (match_addr(&tf->dst, sip, sport) && match_addr(&tf->src, dip, dport))
		return 1;
	return 0;
}

static struct pal_ip_hdr *parse_trace_skb(struct trace_skb *skb, uint32_t *vpcid,
				struct pal_l4port_hdr **l4h, uint16_t *l3_proto)
{
	struct pal_ip_hdr *iph;
	struct pal_geneve_hdr *genh;
	uint16_t off = 0;

	*vpcid = skb->vpcid;

	if (skb->has_eth_hdr) {
		struct pal_eth_hdr *eth = TRACE_SKB_HEADER(skb, off, struct pal_eth_hdr);
		if (!eth)
			return NULL;
		*l3_proto = pal_ntohs(eth->type);
		off += sizeof(struct pal_eth_hdr);
	}

	iph = TRACE_SKB_HEADER(skb, off, struct pal_ip_hdr);
	if (!iph)
		return NULL;

	off += iph->ihl << 2;
	*l4h = TRACE_SKB_HEADER(skb, off, struct pal_l4port_hdr);
	if (iph->protocol == PAL_IPPROTO_UDP && !ip_is_fragment(iph) && (*l4h)->dest == pal_ntohs(GENEVE_PORT)) {
		off += 8;
		genh = TRACE_SKB_HEADER(skb, off, struct pal_geneve_hdr);
		*vpcid = ((uint32_t)genh->vni[0] << 16) | ((uint32_t)genh->vni[1] << 8) | (uint32_t)genh->vni[2];
		off += sizeof(*genh) + (genh->opt_len << 2);
		if (genh->proto == pal_ntohs(GENEVE_TYPE_ETH)) {
			struct pal_eth_hdr *eth = TRACE_SKB_HEADER(skb, off, struct pal_eth_hdr);
			if (!eth)
				return NULL;
			*l3_proto = pal_ntohs(eth->type);
			off += sizeof(struct pal_eth_hdr);
		} else if (genh->proto == pal_ntohs(GENEVE_TYPE_IPV4)){
			*l3_proto = pal_ntohs(genh->proto);
		}
		iph = TRACE_SKB_HEADER(skb, off, struct pal_ip_hdr);
		if (!iph)
			return NULL;
		off += iph->ihl << 2;
		*l4h = TRACE_SKB_HEADER(skb, off, struct pal_l4port_hdr);
	}

	return iph;
}

static void pop_outer_header(struct trace_skb *skb)
{
	struct pal_ip_hdr *outer_iph, *inner_iph;
	struct pal_geneve_hdr *genh;
	uint16_t off = 0;
	uint16_t move_len = 0;

	if (skb->has_eth_hdr) {
		struct pal_eth_hdr *eth = TRACE_SKB_HEADER(skb, off, struct pal_eth_hdr);
		if (!eth)
			return;
		off += sizeof(struct pal_eth_hdr);
	}

	outer_iph = TRACE_SKB_HEADER(skb, off, struct pal_ip_hdr);
	if (!outer_iph)
		return;

	off += outer_iph->ihl << 2;
	if (outer_iph->protocol != PAL_IPPROTO_UDP || ip_is_fragment(outer_iph))
		return;
	struct pal_udp_hdr *udph = TRACE_SKB_HEADER(skb, off, struct pal_udp_hdr);
	if (udph->dest != pal_ntohs(GENEVE_PORT))
		return;
	off += sizeof(*udph);
	genh = TRACE_SKB_HEADER(skb, off, struct pal_geneve_hdr);
	if (!genh)
		return;
	off += sizeof(*genh) + (genh->opt_len << 2);

	if (genh->proto == pal_ntohs(GENEVE_TYPE_ETH))
		off += sizeof(struct pal_eth_hdr);

	inner_iph = TRACE_SKB_HEADER(skb, off, struct pal_ip_hdr);
	if (!inner_iph)
		return;

	move_len = skb->data + skb->pkt_len - (char *)inner_iph;
	skb->pkt_len -= (char *)inner_iph - (char *)outer_iph;
	memmove(outer_iph, inner_iph, move_len);
}

static void mangle_skb(struct trace_option *opt, struct trace_record *r)
{
	struct trace_skb *rx_skb = &r->skbs[0];
	struct trace_skb *tx_skb = &r->skbs[1];

	if (opt->inner) {
		pop_outer_header(rx_skb);
		pop_outer_header(tx_skb);
	}
}

static int match_skb(struct trace_option *opt, struct trace_skb *skb)
{
	uint16_t l3_proto = 0;
	struct pal_ip_hdr *iph;
	struct pal_l4port_hdr *l4h = NULL;
	uint32_t vpcid = -1;

	if (!skb->pkt_len)
		return 0;

	iph = parse_trace_skb(skb, &vpcid, &l4h, &l3_proto);
	if (opt->arp) {
		return l3_proto == PAL_ETH_ARP;
	}

	if (opt->vpcid != -1u && opt->vpcid != vpcid)
		return 0;

	if (iph && ip_is_fragment(iph) && opt->ip_frag == 0)
		return 0;

	if (!opt->tuple.proto && !opt->tuple.nr_dir)
		return 1;

	if (iph) {
		if (opt->tuple.proto && opt->tuple.proto != iph->protocol)
			return 0;
		if (!opt->tuple.nr_dir)
			return 1;
		if (l4h && !ip_is_fragment(iph)) {
			return match_tuple(&opt->tuple,
							   ntohl(iph->saddr), ntohl(iph->daddr),
							   ntohs(l4h->source), ntohs(l4h->dest));
		} else {
			/* match l3 only */
			return match_tuple(&opt->tuple,
							ntohl(iph->saddr), ntohl(iph->daddr),
							opt->tuple.src.port_min, opt->tuple.dst.port_min);
		}
	}

	return 0;
}

static int match_record(struct trace_option *opt, struct trace_record *r)
{
	struct trace_skb *rx_skb = &r->skbs[0];
	struct trace_skb *tx_skb = &r->skbs[1];

	if (opt->pkt_err && r->nr_point &&
		r->points[r->nr_point-1].err != opt->pkt_err)
		return 0;

	if (!trace_option_has(opt, rx_skb->recv_if, TRACE_ON_RX) &&
		!trace_option_has(opt, tx_skb->send_if, TRACE_ON_TX))
		return 0;

	if (!match_skb(opt, rx_skb) &&
		!match_skb(opt, tx_skb))
		return 0;

	return 1;
}

static void trace_cache_init(struct trace_cache *c, trace_ring_t *ring)
{
	c->ring = ring;
	c->next_seq = obj_ring_next_seq(ring);
	c->nr_lost = 0;
	c->r = 0;
	c->w = 0;
}

static struct trace_record *trace_cache_at(struct trace_cache *c, size_t index)
{
	return &c->records[index & (ARRAY_SIZE(c->records) - 1)];
}

static int trace_cache_full(struct trace_cache *c)
{
	return c->w - c->r >= ARRAY_SIZE(c->records);
}

static int trace_cache_empty(struct trace_cache *c)
{
	return c->w == c->r;
}

static void  trace_cache_fill(struct trace_cache *c, struct trace_option *opt)
{
	struct trace_record *r;

	while (!trace_cache_full(c)) {
		r = trace_cache_at(c, c->w);
		if (!obj_ring_read(c->ring, c->next_seq, r))
			break;
		assert(r->seq >= c->next_seq);
		c->nr_lost += r->seq - c->next_seq;
		c->next_seq = r->seq + 1;
		if (match_record(opt, r)) {
			c->w++;
			mangle_skb(opt, r);
		} else
			c->nr_filtered++;
	}
}

void trace_option_sync(struct trace_option *opt)
{
	int port_id;
	int i;

	fp_memory_set_writable(g_trace, 1);

	for (port_id = 0; port_id < PAL_MAX_PORT; port_id++) {
		for (i = 0; i < NR_TRACE_ON; i++) {
			if (trace_option_has(opt, port_id, i))
				g_trace->ports[port_id][i] = 30;
		}
	}

	fp_memory_set_writable(g_trace, 0);
}

static void *trace_option_thread(void *arg)
{
	struct trace_option *opt = arg;

	while (1) {
		trace_option_sync(opt);
		sleep(20);
	}

	return NULL;
}

static void trace_option_init(struct trace_option *opt)
{
	const char *pkt_err;
	const char *vpcid;
	const char *proto;
	const char *addr;
	const char *inner;
	const char *ip_frag;
	const char *count;
	const char *file_r;
	const char *file_w;
	const char *arp;
	pthread_t pid;

	fp_memset(opt->ports_flags, 0);
	opt->pkt_err = 0;
	opt->vpcid = -1;
	opt->inner = 0;
	opt->ip_frag = 0;
	tuple_filter_init(&opt->tuple);
	opt->nr_read_max = -1u;
	opt->fp_r = NULL;
	opt->fp_w = NULL;

	pkt_err = getenv("dpdk_trace_error");
	if (pkt_err) {
		opt->pkt_err = error_code(pkt_err);
		if (!opt->pkt_err) {
			fprintf(stderr, "Invalid trace error: %s\n", pkt_err);
			exit(1);
		}
	}

	vpcid = getenv("dpdk_trace_vpcid");
	if (vpcid)
		opt->vpcid = strtoul(vpcid, NULL, 10);

	proto = getenv("dpdk_trace_proto");
	if (proto)
		opt->tuple.proto = strtoul(proto, NULL, 10);

	addr = getenv("dpdk_trace_addr");
	if (addr) {
		int err = tuple_filter_parse(&opt->tuple, &addr);
		if (err) {
			fprintf(stderr, "Invalid trace addr: %s, at '%s'\n",
				tuple_filter_error(err), addr);
			exit(1);
		}
		if (*addr) {
			fprintf(stderr, "Unexpected input for trace addr: '%s'\n", addr);
			exit(1);
		}
	}

	inner = getenv("dpdk_trace_inner");
	if (inner)
		opt->inner = !!strtoul(inner, NULL, 10);

	ip_frag = getenv("dpdk_trace_ip_frag");
	if (ip_frag)
		opt->ip_frag = !!strtoul(ip_frag, NULL, 10);

	count = getenv("dpdk_trace_count");
	if (count)
		opt->nr_read_max = strtoul(count, NULL, 10);

	file_r = getenv("dpdk_trace_read");
	if (file_r)
		opt->fp_r = fopen(file_r, "rb");

	file_w = getenv("dpdk_trace_write");
	if (file_w)
		opt->fp_w = fopen(file_w, "wb");

	//trace arp only
	arp = getenv("dpdk_trace_arp");
	if (arp)
		opt->arp = strtoul(arp, NULL, 10);

	if (pthread_create(&pid, NULL, trace_option_thread, opt) != 0) {
		fprintf(stderr, "<%s> pthread_create error: %s\n", __func__, strerror(errno));
		exit(1);
	}
}

static uint64_t current_time_usec(void)
{
	struct timeval tv;
	gettimeofday(&tv, NULL);
	return tv.tv_sec * 1000000 + tv.tv_usec;
}

void trace_reader_strftime(struct trace_reader *reader, uint64_t tsc, struct time_buf *buf)
{
	uint64_t usec = trace_reader_get_usec(reader, tsc);
	time_t sec = usec / 1000000;
	struct tm tm;

	localtime_r(&sec, &tm);

	buf->len = strftime(buf->data, sizeof(buf->data), "%Y-%m-%d %H:%M:%S", &tm);
	SPRINTF(*buf, ".%06d", (int)(usec % 1000000));
}

void trace_reader_init(struct trace_reader *reader)
{
	struct trace_cache *c;

	array_for_each(c, reader->percpu) {
		trace_cache_init(c, &g_trace->percpu[c - reader->percpu]);
	}

	reader->start_time_usec = current_time_usec();
	reader->start_time_tsc = rte_rdtsc();
	reader->nr_read = 0;
	reader->nr_lost = 0;
	reader->nr_filtered = 0;

	trace_option_init(&reader->option);

	if (reader->option.fp_r) {
		if (fread(&reader->start_time_usec, sizeof(uint64_t), 2, reader->option.fp_r) != 2) {
			fprintf(stderr, "failed to read binary records: %s\n", strerror(errno));
			fclose(reader->option.fp_r);
			reader->option.fp_r = NULL;
		}
	}

	if (reader->option.fp_w) {
		if (fwrite(&reader->start_time_usec, sizeof(uint64_t), 2, reader->option.fp_w) != 2) {
			fprintf(stderr, "failed to write binary records: %s\n", strerror(errno));
			fclose(reader->option.fp_w);
			reader->option.fp_w = NULL;
		}
	}

	{
		struct time_buf time_buf;
		trace_reader_strftime(reader, reader->start_time_tsc, &time_buf);
		fprintf(stderr, "trace_start_time: %.*s\n", time_buf.len, time_buf.data);
		fprintf(stderr, "filter: vpcid %d ", reader->option.vpcid);
		tuple_filter_dump(&reader->option.tuple, stderr);
	}
}

static void fill_trace_cache(struct trace_reader *reader)
{
	int i;

	for (i = 0; i < 1; i++) {
		struct trace_cache *c;
		size_t nr_lost = 0;
		size_t nr_filtered = 0;
		array_for_each(c, reader->percpu) {
			trace_cache_fill(c, &reader->option);
			nr_lost += c->nr_lost;
			nr_filtered += c->nr_filtered;
		}
		reader->nr_lost = nr_lost;
		reader->nr_filtered = nr_filtered;
	}
}

static struct trace_cache *choose_trace_cache(struct trace_reader *reader)
{
	uint64_t tsc_min = 0;
	struct trace_cache *tsc_min_c = NULL;
	struct trace_cache *c;
	struct trace_record *r;

	array_for_each(c, reader->percpu) {
		if (trace_cache_empty(c))
			continue;
		r = trace_cache_at(c, c->r);
		if (!tsc_min_c || tsc_min > r->rx_tsc) {
			tsc_min_c = c;
			tsc_min = r->rx_tsc;
		}
	}

	return tsc_min_c;
}

struct trace_record *trace_read(struct trace_reader *reader)
{
	struct trace_cache *c;
	struct trace_record *r;
	static struct trace_record rbuf;

	if (reader->nr_read >= reader->option.nr_read_max)
		exit(1); // SIGINT was ignored somewhere unexpectly.

	if (reader->option.fp_r) {
		r = &rbuf;
		for (;;) {
			if (fread(r, sizeof(*r), 1, reader->option.fp_r) != 1) {
				reader->option.nr_read_max = reader->nr_read; // to avoid next read.
				(void)raise(SIGINT);
				return NULL;
			}
			if (match_record(&reader->option, r))
				break;
			reader->nr_filtered++;
		}
	} else {
		fill_trace_cache(reader);
		c = choose_trace_cache(reader);
		if (!c)
			return NULL;
		r = trace_cache_at(c, c->r++);
	}

	if (reader->option.fp_w) {
		if (fwrite(r, sizeof(*r), 1, reader->option.fp_w) != 1) {
			fprintf(stderr, "failed to write binary records: %s\n", strerror(errno));
			fclose(reader->option.fp_w);
			reader->option.fp_w = NULL;
			(void)raise(SIGINT);
		}
	}

	reader->nr_read++;

	if (reader->nr_read == reader->option.nr_read_max) {
		if (reader->option.fp_w)
			fclose(reader->option.fp_w);
		(void)raise(SIGINT);
	}

	return r;
}


```
