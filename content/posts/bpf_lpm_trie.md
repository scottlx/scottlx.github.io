---
title: "bpf lpm trie"
date: 2025-03-06T15:49:00+08:00
draft: false
tags: ["ebpf","cilium","k8s"]
tags_weight: 66
series: ["ebpf系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
---



lpm有多种实现方式，最常用的是用trie。当然也会有更简单的实现方式，例如某些特定场景用多重哈希表就能解决（ipv4地址，32个掩码对应32个哈希表）



从4.11内核版本开始，bpf map引入了`BPF_MAP_TYPE_LPM_TRIE`

主要是用于匹配ip地址，内部是将数据存储在一个不平衡的trie中，key使用`prefixlen,data`

data是以大端网络序存储的，data[0]存的是msb。

prefixlen支持8的整数倍，最高可以是2048。因此除了ip匹配，还可以用来做端口，协议，vpcid等等的扩充匹配。在应用层面上除了做路由表，还可以作为acl，policy等匹配过滤的底层实现



#### 使用方式

[BPF_MAP_TYPE_LPM_TRIE — The Linux Kernel documentation](https://docs.kernel.org/next/bpf/map_lpm_trie.html)

除了上述基本的Ipv4的使用方式，扩展使用方式可以参考一下cillium中IPCACHE_MAP的使用



首先是map的定义

```c
struct ipcache_key {
	struct bpf_lpm_trie_key lpm_key;
	__u16 cluster_id;
	__u8 pad1;
	__u8 family;
	union {
		struct {
			__u32		ip4;
			__u32		pad4;
			__u32		pad5;
			__u32		pad6;
		};
		union v6addr	ip6;
	};
} __packed;

/* Global IP -> Identity map for applying egress label-based policy */
struct {
	__uint(type, BPF_MAP_TYPE_LPM_TRIE);
	__type(key, struct ipcache_key);
	__type(value, struct remote_endpoint_info);
	__uint(pinning, LIBBPF_PIN_BY_NAME);
	__uint(max_entries, IPCACHE_MAP_SIZE);
	__uint(map_flags, BPF_F_NO_PREALLOC);
} IPCACHE_MAP __section_maps_btf;
```

可以看到cillium将v4和v6合并成一个map查询，匹配条件并带上了`cluster_id`

因此查询map时候，prefix_len需要加上cluster_id，pad1，family这四个字节的长度。例如查询192.168.10.0/24的时候，prefix_len(bit)= 24bit + 4byte*8bit/byte = 56bit

```c
/* IPCACHE_STATIC_PREFIX gets sizeof non-IP, non-prefix part of ipcache_key */
#define IPCACHE_STATIC_PREFIX							\
	(8 * (sizeof(struct ipcache_key) - sizeof(struct bpf_lpm_trie_key)	\
	      - sizeof(union v6addr)))
#define IPCACHE_PREFIX_LEN(PREFIX) (IPCACHE_STATIC_PREFIX + (PREFIX))

static __always_inline __maybe_unused struct remote_endpoint_info *
ipcache_lookup4(const void *map, __be32 addr, __u32 prefix, __u32 cluster_id)
{
	struct ipcache_key key = {
		.lpm_key = { IPCACHE_PREFIX_LEN(prefix), {} },
		.family = ENDPOINT_KEY_IPV4,
		.ip4 = addr,
	};

	/* Check overflow */
	if (cluster_id > UINT16_MAX)
		return NULL;

	key.cluster_id = (__u16)cluster_id;

	key.ip4 &= GET_PREFIX(prefix);
	return map_lookup_elem(map, &key);
}
```

`cluster_id`的字节序是大端还是小端其实没有区别，只要插入和查询都用的相同的字节序就行





#### 基本原理

[lpm_trie.c - kernel/bpf/lpm_trie.c - Linux source code v5.13 - Bootlin Elixir Cross Referencer](https://elixir.bootlin.com/linux/v5.13/source/kernel/bpf/lpm_trie.c)



##### 数据结构

```c
struct lpm_trie_node {
    struct rcu_head rcu;
    struct lpm_trie_node __rcu *child[2];
    u32 prefixlen;  // node的前缀，例如192.168.0.0/24这个node是24
    u32 flags;
    u8 data[];   // data+value，例如192.168.0.0/24 --》123，那么data[5]={c0, a8, 00, 00, 7b}
};
struct lpm_trie {
	struct bpf_map			map;
	struct lpm_trie_node __rcu	*root;
	size_t				n_entries; // 节点数
	size_t				max_prefixlen; //最长前缀bit，例如ipv4是32，上述IPCACHE_MAP的例子是32+32 = 64
	size_t				data_size; // max_prefixlen的byte表示，max_prefixlen/8
	spinlock_t			lock;
};
```



- `data` 数组的前 `trie->data_size` 字节存储前缀数据。整个`trie`的所有节点，前缀数据的大小是固定的
- 如果节点有 `value`，则 `value` 紧跟在前缀数据之后存储。



##### 分配节点

```c
static struct lpm_trie_node *lpm_trie_node_alloc(const struct lpm_trie *trie, const void *value)
{
    struct lpm_trie_node *node;
    size_t size = sizeof(struct lpm_trie_node) + trie->data_size;

    if (value)
        size += trie->map.value_size;  // 如果有value，分配的空间加上value大小

    node = bpf_map_kmalloc_node(&trie->map, size, GFP_ATOMIC | __GFP_NOWARN, trie->map.numa_node);
    if (!node)
        return NULL;

    node->flags = 0;

    if (value)
        memcpy(node->data + trie->data_size, value, trie->map.value_size);  // 如果有value，将其复制到 data 数组中前缀数据之后的位置。

    return node;
}

```



##### 插入流程

```c
/* This trie implements a longest prefix match algorithm that can be used to
 * match IP addresses to a stored set of ranges.
 *
 * Data stored in @data of struct bpf_lpm_key and struct lpm_trie_node is
 * interpreted as big endian, so data[0] stores the most significant byte.
 *
 * Match ranges are internally stored in instances of struct lpm_trie_node
 * which each contain their prefix length as well as two pointers that may
 * lead to more nodes containing more specific matches. Each node also stores
 * a value that is defined by and returned to userspace via the update_elem
 * and lookup functions.
 *
 * For instance, let's start with a trie that was created with a prefix length
 * of 32, so it can be used for IPv4 addresses, and one single element that
 * matches 192.168.0.0/16. The data array would hence contain
 * [0xc0, 0xa8, 0x00, 0x00] in big-endian notation. This documentation will
 * stick to IP-address notation for readability though.
 *
 * As the trie is empty initially, the new node (1) will be places as root
 * node, denoted as (R) in the example below. As there are no other node, both
 * child pointers are %NULL.
 *
 *              +----------------+
 *              |       (1)  (R) |
 *              | 192.168.0.0/16 |
 *              |    value: 1    |
 *              |   [0]    [1]   |
 *              +----------------+
 *
 * Next, let's add a new node (2) matching 192.168.0.0/24. As there is already
 * a node with the same data and a smaller prefix (ie, a less specific one),
 * node (2) will become a child of (1). In child index depends on the next bit
 * that is outside of what (1) matches, and that bit is 0, so (2) will be
 * child[0] of (1):
 
 * 由于已经存在一个data相同但长度更短的前缀，新前缀将作为节点(1)的子节点存储。新前缀（192.168.0.0/24）在旧前缀（192.168.0.0/16）之后的下一位（第17位） 为0，因此将成为孩子0
 *
 *              +----------------+
 *              |       (1)  (R) |
 *              | 192.168.0.0/16 |
 *              |    value: 1    |
 *              |   [0]    [1]   |
 *              +----------------+
 *                   |
 *    +----------------+
 *    |       (2)      |
 *    | 192.168.0.0/24 |
 *    |    value: 2    |
 *    |   [0]    [1]   |
 *    +----------------+
 *
 * The child[1] slot of (1) could be filled with another node which has bit #17
 * (the next bit after the ones that (1) matches on) set to 1. For instance,
 * 192.168.128.0/24:
 * 第17位为1，所以插入在右边
 *
 *              +----------------+
 *              |       (1)  (R) |
 *              | 192.168.0.0/16 |
 *              |    value: 1    |
 *              |   [0]    [1]   |
 *              +----------------+
 *                   |      |
 *    +----------------+  +------------------+
 *    |       (2)      |  |        (3)       |
 *    | 192.168.0.0/24 |  | 192.168.128.0/24 |
 *    |    value: 2    |  |     value: 3     |
 *    |   [0]    [1]   |  |    [0]    [1]    |
 *    +----------------+  +------------------+
 *
 * Let's add another node (4) to the game for 192.168.1.0/24. In order to place
 * it, node (1) is looked at first, and because (4) of the semantics laid out
 * above (bit #17 is 0), it would normally be attached to (1) as child[0].
 * 本来要插入在（1）的左孩子，但是这个位置已经被（2）占掉了，所以要创造出这样的一个空间，也就是（2）的prefixlen 24往前移一位prefixlen 23，作为假节点
 * However, that slot is already allocated, so a new node is needed in between.
 * That node does not have a value attached to it and it will never be
 * returned to users as result of a lookup. It is only there to differentiate
 * the traversal further. It will get a prefix as wide as necessary to
 * distinguish its two children:
 *
 *                      +----------------+
 *                      |       (1)  (R) |
 *                      | 192.168.0.0/16 |
 *                      |    value: 1    |
 *                      |   [0]    [1]   |
 *                      +----------------+
 *                           |      |
 *            +----------------+  +------------------+
 *            |       (4)  (I) |  |        (3)       |
 *            | 192.168.0.0/23 |  | 192.168.128.0/24 |
 *            |    value: ---  |  |     value: 3     |
 *            |   [0]    [1]   |  |    [0]    [1]    |
 *            +----------------+  +------------------+
 *                 |      |
 *  +----------------+  +----------------+
 *  |       (2)      |  |       (5)      |
 *  | 192.168.0.0/24 |  | 192.168.1.0/24 |
 *  |    value: 2    |  |     value: 5   |
 *  |   [0]    [1]   |  |   [0]    [1]   |
 *  +----------------+  +----------------+
 *
 * 192.168.1.1/32 would be a child of (5) etc.
 *
 * An intermediate node will be turned into a 'real' node on demand. In the
 * example above, (4) would be re-used if 192.168.0.0/23 is added to the trie.
 *
 * A fully populated trie would have a height of 32 nodes, as the trie was
 * created with a prefix length of 32.
 *
 * The lookup starts at the root node. If the current node matches and if there
 * is a child that can be used to become more specific, the trie is traversed
 * downwards. The last node in the traversal that is a non-intermediate one is
 * returned.
 */


static size_t longest_prefix_match(const struct lpm_trie *trie,
				   const struct lpm_trie_node *node,
				   const struct bpf_lpm_trie_key *key)
{
	u32 limit = min(node->prefixlen, key->prefixlen);  // 匹配到两者的最小就可以停止了，例如/24和/16，只要匹配到/16就可以确定已经匹配上了
	u32 prefixlen = 0, i = 0;

	BUILD_BUG_ON(offsetof(struct lpm_trie_node, data) % sizeof(u32)); // 确保数据四字节对齐
	BUILD_BUG_ON(offsetof(struct bpf_lpm_trie_key, data) % sizeof(u32));

#if defined(CONFIG_HAVE_EFFICIENT_UNALIGNED_ACCESS) && defined(CONFIG_64BIT)  // 如果64位系统支持不对齐读取，则先处理开头的64bit

	/* data_size >= 16 has very small probability.
	 * We do not use a loop for optimal code generation.
	 */
	if (trie->data_size >= 8) {
		u64 diff = be64_to_cpu(*(__be64 *)node->data ^
				       *(__be64 *)key->data);

		prefixlen = 64 - fls64(diff);
		if (prefixlen >= limit)
			return limit;
		if (diff)
			return prefixlen;
		i = 8;
	}
#endif
	// 循环4个字节4个字节去匹配
	while (trie->data_size >= i + 4) {
		u32 diff = be32_to_cpu(*(__be32 *)&node->data[i] ^
				       *(__be32 *)&key->data[i]); // 异或，不匹配的bit为1

		prefixlen += 32 - fls(diff);   // Find Last Set，返回一个整数的最高有效位的位置（从1开始计数），取最高的不匹配的位置
		if (prefixlen >= limit)
			return limit; // 若超过limit，说明完全匹配上，直接返回limit
		if (diff)
			return prefixlen;  // 发现不匹配的位置，则当前的位置就是最长匹配前缀的位置
		i += 4; // 若没有diff，说明这四个byte都匹配上了，继续
	}

    // 如果trie->data_size不是8的倍数，需要再处理末尾的1，2，3个字节
	if (trie->data_size >= i + 2) {
		u16 diff = be16_to_cpu(*(__be16 *)&node->data[i] ^
				       *(__be16 *)&key->data[i]);

		prefixlen += 16 - fls(diff);
		if (prefixlen >= limit)
			return limit;
		if (diff)
			return prefixlen;
		i += 2;
	}

	if (trie->data_size >= i + 1) {
		prefixlen += 8 - fls(node->data[i] ^ key->data[i]);

		if (prefixlen >= limit)
			return limit;
	}

	return prefixlen;
}


/* Called from syscall or from eBPF program */
static int trie_update_elem(struct bpf_map *map,
			    void *_key, void *value, u64 flags)
{
	struct lpm_trie *trie = container_of(map, struct lpm_trie, map);
	struct lpm_trie_node *node, *im_node = NULL, *new_node = NULL;
	struct lpm_trie_node __rcu **slot;
	struct bpf_lpm_trie_key *key = _key;
	unsigned long irq_flags;
	unsigned int next_bit;
	size_t matchlen = 0;
	int ret = 0;

	if (unlikely(flags > BPF_EXIST))
		return -EINVAL;

	if (key->prefixlen > trie->max_prefixlen) //最大匹配长度校验
		return -EINVAL;

	spin_lock_irqsave(&trie->lock, irq_flags);

	/* Allocate and fill a new node */

	if (trie->n_entries == trie->map.max_entries) {  // 最大node容量校验
		ret = -ENOSPC;
		goto out;
	}

	new_node = lpm_trie_node_alloc(trie, value); // 分配新node
	if (!new_node) {
		ret = -ENOMEM;
		goto out;
	}

	trie->n_entries++;   // node计数增加

	new_node->prefixlen = key->prefixlen;
	RCU_INIT_POINTER(new_node->child[0], NULL);  // 初始化child指针
	RCU_INIT_POINTER(new_node->child[1], NULL);
	memcpy(new_node->data, key->data, trie->data_size);

	/* Now find a slot to attach the new node. To do that, walk the tree
	 * from the root and match as many bits as possible for each node until
	 * we either find an empty slot or a slot that needs to be replaced by
	 * an intermediate node.
	 */
	slot = &trie->root;

	while ((node = rcu_dereference_protected(*slot,
					lockdep_is_held(&trie->lock)))) {  //如果slot 非空，则将其值赋给 node，并进入循环体。
		matchlen = longest_prefix_match(trie, node, key);  // 匹配的长度

		if (node->prefixlen != matchlen || // 这边不相等的情况下matchlen一定比node->prefixlen小，也就是说key没有完全匹配上node，此时找到位置了，需要插在node上面
		    node->prefixlen == key->prefixlen ||  // 找到prefix完全匹配的节点，应该在这一层插入
		    node->prefixlen == trie->max_prefixlen) // node已经是trie最深的叶子节点了
			break;

		next_bit = extract_bit(key->data, node->prefixlen);  //从目标键 key 的数据中提取当前前缀长度位置的位，存储在 next_bit
		slot = &node->child[next_bit]; //slot是当前处理的指针，根据next_bit往下遍历
	}

	/* If the slot is empty (a free child pointer or an empty root),
	 * simply assign the @new_node to that slot and be done.
	 */
	if (!node) {
		rcu_assign_pointer(*slot, new_node); // 空位置则直接插入
		goto out;
	}

	/* If the slot we picked already exists, replace it with @new_node
	 * which already has the correct data array set.
	 
	 *   下面这种情况，matchlen == 24, key->prefixlen = 24，其实就是节点的替换，刷新value
     *   +----------------+           +----------------+   
     *   |     new_node   |           |       (1)      |
     *   | 192.168.0.0/24 |           | 192.168.0.0/16 |
     *   |    value: 3    |           |    value: 1    |
     *   |   [0]    [1]   |           |   [0]    [1]   |
     *   +----------------+           +----------------+  
     *            |                           |   
     			  +-------------------------->|
                     	             +----------------+ 
                     	             |       (2) node |  
                  	                 | 192.168.0.0/24 | 
                	                 |    value: 2    | 
                 	                 |   [0]    [1]   |  
                 	                 +----------------+ 
	 */
    
    
	if (node->prefixlen == matchlen) {
		new_node->child[0] = node->child[0];
		new_node->child[1] = node->child[1];

		if (!(node->flags & LPM_TREE_NODE_FLAG_IM))
			trie->n_entries--;

		rcu_assign_pointer(*slot, new_node);
		kfree_rcu(node, rcu);

		goto out;
	}

	/* If the new node matches the prefix completely, it must be inserted
	 * as an ancestor. Simply insert it between @node and *@slot.
	 
	 *   下面这种情况，matchlen == 17, key->prefixlen == 17，node->prefixlen > matchlen, 需要在node上面插入
     *   +----------------+           +----------------+   
     *   |       (3)  key |           |       (1)      |
     *   | 192.168.0.0/17 |           | 192.168.0.0/16 |
     *   |    value: 1    |           |    value: 1    |
     *   |   [0]    [1]   |           |   [0]    [1]   |
     *   +----------------+           +----------------+  
     *            |                           |   
     			  +-------------------------->|
                     	             +----------------+ 
                     	             |       (2) node |  
                  	                 | 192.168.0.0/24 | 
                	                 |    value: 2    | 
                 	                 |   [0]    [1]   |  
                 	                 +----------------+ 
 
	 */
	if (matchlen == key->prefixlen) {
		next_bit = extract_bit(node->data, matchlen);   // 上图例子：17bit是0， 所以是左孩子
		rcu_assign_pointer(new_node->child[next_bit], node); // node作为new_node的child
		rcu_assign_pointer(*slot, new_node);
		goto out;
	}

    /*
     *   下面这种情况，matchlen == 23, key->prefixlen == 24，node->prefixlen > matchlen, 需要插imnode，matchlen作为imnode的prefixlen
     *   +----------------+           +----------------+   
     *   |       (3)  new |           |       (1)      |
     *   | 192.168.1.0/24 |           | 192.168.0.0/16 |
     *   |    value: 1    |           |    value: 1    |
     *   |   [0]    [1]   |           |   [0]    [1]   |
     *   +----------------+           +----------------+  
     *            |                           |   
     			  +-------------------------->|
                     	             +----------------+ 
                     	             |       (2) node |  
                  	                 | 192.168.0.0/24 | 
                	                 |    value: 2    | 
                 	                 |   [0]    [1]   |  
                 	                 +----------------+ 
     
     						    ||
     							||
     							V
                 	                 
     *                      +----------------+
     *                      |       (1)  (R) |
     *                      | 192.168.0.0/16 |
     *                      |    value: 1    |
     *                      |   [0]    [1]   |
     *                      +----------------+
     *                           |      
     *            +----------------+ 
     *            |       (4)  (I) |
     *            | 192.168.0.0/23 | 
     *            |    value: ---  |  
     *            |   [0]    [1]   |  
     *            +----------------+ 
     *                 |      |
     *  +----------------+  +----------------+
     *  |       (2)      |  |  (5) new, key  |
     *  | 192.168.0.0/24 |  | 192.168.1.0/24 |
     *  |    value: 2    |  |     value: 5   |
     *  |   [0]    [1]   |  |   [0]    [1]   |
     *  +----------------+  +----------------+
 
	 */
    
	im_node = lpm_trie_node_alloc(trie, NULL); // 假node，value是空
	if (!im_node) {
		ret = -ENOMEM;
		goto out;
	}

	im_node->prefixlen = matchlen;  // matchlen作为imnode的prefixlen
	im_node->flags |= LPM_TREE_NODE_FLAG_IM; // node标记
	memcpy(im_node->data, node->data, trie->data_size);

	/* Now determine which child to install in which slot */
	if (extract_bit(key->data, matchlen)) {   // key是newnode，所以如果1，说明new_node在im_node右孩子
		rcu_assign_pointer(im_node->child[0], node);
		rcu_assign_pointer(im_node->child[1], new_node);
	} else {
		rcu_assign_pointer(im_node->child[0], new_node);
		rcu_assign_pointer(im_node->child[1], node);
	}

	/* Finally, assign the intermediate node to the determined spot */
	rcu_assign_pointer(*slot, im_node);

out:
	if (ret) {
		if (new_node)
			trie->n_entries--;

		kfree(new_node);
		kfree(im_node);
	}

	spin_unlock_irqrestore(&trie->lock, irq_flags);

	return ret;
}
```



##### 查询流程

```c
/* Called from syscall or from eBPF program */
static void *trie_lookup_elem(struct bpf_map *map, void *_key)
{
	struct lpm_trie *trie = container_of(map, struct lpm_trie, map);
	struct lpm_trie_node *node, *found = NULL;
	struct bpf_lpm_trie_key *key = _key;

	/* Start walking the trie from the root node ... */

	for (node = rcu_dereference(trie->root); node;) {
		unsigned int next_bit;
		size_t matchlen;

		/* Determine the longest prefix of @node that matches @key.
		 * If it's the maximum possible prefix for this trie, we have
		 * an exact match and can return it directly.
		 */
		matchlen = longest_prefix_match(trie, node, key);
		if (matchlen == trie->max_prefixlen) { // 到底了，直接返回
			found = node;
			break;
		}

		/* If the number of bits that match is smaller than the prefix
		 * length of @node, bail out and return the node we have seen
		 * last in the traversal (ie, the parent).
		 */
		if (matchlen < node->prefixlen) // 没有完全匹配，那就找到最长匹配了，但是是上一个
			break;

		/* Consider this node as return candidate unless it is an
		 * artificially added intermediate one.
		 */
		if (!(node->flags & LPM_TREE_NODE_FLAG_IM)) // 不是假node才算找到
			found = node;

		/* If the node match is fully satisfied, let's see if we can
		 * become more specific. Determine the next bit in the key and
		 * traverse down.
		 */
		next_bit = extract_bit(key->data, node->prefixlen); //完全匹配了，那就继续往下走继续匹配
		node = rcu_dereference(node->child[next_bit]);
	}

	if (!found)
		return NULL;

	return found->data + trie->data_size;  // value在data_size后面
}
```

