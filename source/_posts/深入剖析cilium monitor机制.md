---
title: "深入剖析cilium monitor机制"
date: 2025-05-26T17:53:00+08:00
draft: false
tags: ["ebpf", "cilium", "k8s"]
tags_weight: 66
series: ["ebpf系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
---

<!-- more -->

### 可调试性

报文转发面组件中，可调试性十分关键。开发阶段可能可以使用 gdb（ebpf 甚至不能用 gdb，只能用 trace_printk），log 等方式进行调试，但到了生产环境，以下几个功能是必须要完备的：

- 抓包手段

  - 按照网卡抓包
  - 按照流进行抓包
  - 按照特定过滤条件抓包，例如源目的地址，端口，协议号等

- 报文计数
  - 收发包计数：rx，tx 阶段计数
  - 丢包计数：按照错误码进行区分
  - 特定观测点计数：一些重要转发函数，例如 l3_fwd, arp_response 等
- 流日志
  - 流量方向：egress/ingress
  - session 信息：五元组，nat 信息，tcp 状态等
  - 其他必要的上下文：例如转发表项查找的结果，构造的 action，硬件卸载标记等

### linux perf_events

![img](/img/blobs/linuxperfevent.png)

ebpf perf 基于 linux perf_event 子系统。epbf 通知用户态拷贝数据时基于 perf_events 的

### perf buffer

ebpf 中提供了内核和用户空间之间高效地交换数据的机制：perf buffer。它是一种 per-cpu 的环形缓冲区，当我们需要将 ebpf 收集到的数据发送到用户空间记录或者处理时，就可以用 perf buffer 来完成。它还有如下特点：

1. 能够记录可变长度数据记；
2. 能够通过内存映射的方式在用户态读取读取数据，而无需通过系统调用陷入到内核去拷贝数据；
3. 实现 epoll 通知机制

因此在 cilium 中，实现上述调试手段的思路，就是在转发面代码中构造相应的 event 到`EVENTS_MAP`，之后通过别的工具去读取并解析`EVENTS_MAP`中的数据

EVENTS_MAP 定义如下: bpf/lib/events.h

```c
struct {
	__uint(type, BPF_MAP_TYPE_PERF_EVENT_ARRAY);
	__uint(key_size, sizeof(__u32));
	__uint(value_size, sizeof(__u32));
	__uint(pinning, LIBBPF_PIN_BY_NAME);
	__uint(max_entries, __NR_CPUS__);
} EVENTS_MAP __section_maps_btf;

```

key 是 cpu 的编号，因此大小是 u32；value 一般是文件描述符 fd，关联一个 perf event，因此也是 u32

数据面代码构造好 data 之后，使用 helper function: `bpf_perf_event_output`通知用户态代码拷贝数据

下面是 cilium 代码中封装好的 event 输出函数，最终就是调用的 bpf_perf_event_output

```c
// bpf/include/bpf/ctx/skb.h
#define ctx_event_output	skb_event_output

// bpf/include/bpf/helpers_skb.h
/* Events for user space */
static int BPF_FUNC_REMAP(skb_event_output, struct __sk_buff *skb, void *map,
			  __u64 index, const void *data, __u32 size) =
			 (void *)BPF_FUNC_perf_event_output; //对应的func id 是 25

// /usr/include/linux/bpf.h
/* integer value in 'imm' field of BPF_CALL instruction selects which helper
 * function eBPF program intends to call
 */
#define __BPF_ENUM_FN(x) BPF_FUNC_ ## x
enum bpf_func_id {
	__BPF_FUNC_MAPPER(__BPF_ENUM_FN)
	__BPF_FUNC_MAX_ID,
};
#undef __BPF_ENUM_FN
```

### 转发面生成 perf

debug，drop notify，trace 都只是不同的数据格式，最终都是调用`ctx_event_output`生成 event

数据格式依靠 common header 的 type 进行区分

```c
// bpf/lib/common.h
#define NOTIFY_COMMON_HDR \
	__u8		type;		\
	__u8		subtype;	\
	__u16		source;		\
	__u32		hash;

//type 定义
enum {
	CILIUM_NOTIFY_UNSPEC,
	CILIUM_NOTIFY_DROP,
	CILIUM_NOTIFY_DBG_MSG,
	CILIUM_NOTIFY_DBG_CAPTURE,
	CILIUM_NOTIFY_TRACE,
	CILIUM_NOTIFY_POLICY_VERDICT,
	CILIUM_NOTIFY_CAPTURE,
	CILIUM_NOTIFY_TRACE_SOCK,
};
```

subtype，source，hash 这三个字段，不同的 type 有各自不同的用法，后面会提到

#### Debug 日志

debug 分两种，

- 简单的传参，只传递 2 个或 3 个 u32 到用户态
- 带 capture 的，将整个\_\_ctx_buff 报文带到用户态空间

```c
// bpf/lib/dbg.h
// 只带arg的，common header后直接加data
static __always_inline void cilium_dbg(struct __ctx_buff *ctx, __u8 type,
				       __u32 arg1, __u32 arg2)
{
	struct debug_msg msg = {
		__notify_common_hdr(CILIUM_NOTIFY_DBG_MSG, type),
		.arg1	= arg1,
		.arg2	= arg2,
	};

	ctx_event_output(ctx, &EVENTS_MAP, BPF_F_CURRENT_CPU,
			 &msg, sizeof(msg));
}

// 带capture的，common header后带了pktcap_hdr，指定原包长和抓包的包长
static __always_inline void cilium_dbg_capture2(struct __ctx_buff *ctx, __u8 type,
						__u32 arg1, __u32 arg2)
{
	__u64 ctx_len = ctx_full_len(ctx);
	__u64 cap_len = min_t(__u64, TRACE_PAYLOAD_LEN, ctx_len);
	struct debug_capture_msg msg = {
		__notify_common_hdr(CILIUM_NOTIFY_DBG_CAPTURE, type),
		__notify_pktcap_hdr((__u32)ctx_len, (__u16)cap_len, NOTIFY_CAPTURE_VER),
		.arg1	= arg1,
		.arg2	= arg2,
	};

	ctx_event_output(ctx, &EVENTS_MAP,
			 (cap_len << 32) | BPF_F_CURRENT_CPU,
			 &msg, sizeof(msg));
}

```

其中 type（common_header 中的 subtype）定义了用户态代码在解析时的输出格式，由 monitor 进行格式化输出

```go
// pkg/monitor/datapath_debug.go
// Message returns the debug message in a human-readable format
func (n *DebugMsg) Message(linkMonitor getters.LinkGetter) string {
	switch n.SubType {
	case DbgGeneric:
		return fmt.Sprintf("No message, arg1=%d (%#x) arg2=%d (%#x)", n.Arg1, n.Arg1, n.Arg2, n.Arg2)
	case DbgLocalDelivery:
		return fmt.Sprintf("Attempting local delivery for container id %d from seclabel %d", n.Arg1, n.Arg2)
	case DbgEncap:
		return fmt.Sprintf("Encapsulating to node %d (%#x) from seclabel %d", n.Arg1, n.Arg1, n.Arg2)
	case DbgLxcFound:
		var ifname string
		if linkMonitor != nil {
			ifname = linkMonitor.Name(n.Arg1)
		}
		return fmt.Sprintf("Local container found ifindex %s seclabel %d", ifname, byteorder.NetworkToHost16(uint16(n.Arg2)))
	case DbgPolicyDenied:
		return fmt.Sprintf("Policy evaluation would deny packet from %d to %d", n.Arg1, n.Arg2)
	case DbgCtLookup:
		return fmt.Sprintf("CT lookup: %s", ctInfo(n.Arg1, n.Arg2))
	case DbgCtLookupRev:
		return fmt.Sprintf("CT reverse lookup: %s", ctInfo(n.Arg1, n.Arg2))
	case DbgCtLookup4:
        // ...
```

#### Drop notification

drop notification 是一种带了更多信息的 debug capture，数据格式如下

```c
// bpf/lib/drop.h
struct drop_notify {
	NOTIFY_CAPTURE_HDR
	__u32		src_label; /* identifaction labels */
	__u32		dst_label;
	__u32		dst_id; /* 0 for egress */
	__u16		line;  /* 发生丢包的代码行 */
	__u8		file;  /* 发生丢包的文件名 */
	__s8		ext_error; /* 扩展错误码 */
	__u32		ifindex;  /* 丢包网卡 */
};
```

文件名和代码行是编译器内置宏输出的

```c
// bpf/lib/source_info.h

#define __MAGIC_FILE__ (__u8)__id_for_file(__FILE_NAME__)
#define __MAGIC_LINE__ __LINE__

#define _strcase_(id, known_name) do {			\
	if (!__builtin_strcmp(header_name, known_name))	\
		return id;				\
	} while (0)

/*
 * __id_for_file is used by __MAGIC_FILE__ to encode source file information in
 * drop notifications and forward/drop metrics. It must be inlined, otherwise
 * clang won't translate this to a constexpr.
 *
 * The following list of files is static, but it is validated during build with
 * the pkg/datapath/loader/check-sources.sh tool.
 */
static __always_inline int
__id_for_file(const char *const header_name)
{
	/* @@ source files list begin */

	/* source files from bpf/ */
	_strcase_(1, "bpf_host.c");
	_strcase_(2, "bpf_lxc.c");
	_strcase_(3, "bpf_overlay.c");
	_strcase_(4, "bpf_xdp.c");
	_strcase_(5, "bpf_sock.c");
	_strcase_(6, "bpf_network.c");
	_strcase_(7, "bpf_wireguard.c");

	/* header files from bpf/lib/ */
	_strcase_(101, "arp.h");
	_strcase_(102, "drop.h");
	_strcase_(103, "srv6.h");
	_strcase_(104, "icmp6.h");
	_strcase_(105, "nodeport.h");
	_strcase_(106, "lb.h");
	_strcase_(107, "mcast.h");
	_strcase_(108, "ipv4.h");
	_strcase_(109, "conntrack.h");
	_strcase_(110, "l3.h");
	_strcase_(111, "trace.h");
	_strcase_(112, "encap.h");
	_strcase_(113, "encrypt.h");
	_strcase_(114, "host_firewall.h");
	_strcase_(115, "nodeport_egress.h");

	/* @@ source files list end */

	return 0;
}
```

用户态解析时，文件编号需要对应上，可以通过`contrib/scripts/check-source-info.sh`这个脚本来确保两个文件是对应上的

```go
// pkg/monitor/api/files.go

// Keep in sync with __id_for_file in bpf/lib/source_info.h.
var files = map[uint8]string{
	// @@ source files list begin

	// source files from bpf/
	1: "bpf_host.c",
	2: "bpf_lxc.c",
	3: "bpf_overlay.c",
	4: "bpf_xdp.c",
	5: "bpf_sock.c",
	6: "bpf_network.c",
	7: "bpf_wireguard.c",

	// header files from bpf/lib/
	101: "arp.h",
	102: "drop.h",
	103: "srv6.h",
	104: "icmp6.h",
	105: "nodeport.h",
	106: "lb.h",
	107: "mcast.h",
	108: "ipv4.h",
	109: "conntrack.h",
	110: "l3.h",
	111: "trace.h",
	112: "encap.h",
	113: "encrypt.h",
	114: "host_firewall.h",
	115: "nodeport_egress.h",

	// @@ source files list end
}

// BPFFileName returns the file name for the given BPF file id.
func BPFFileName(id uint8) string {
	if name, ok := files[id]; ok {
		return name
	}
	return fmt.Sprintf("unknown(%d)", id)
}

```

相同的，`bpf/lib/common.h`和`pkg/monitor/api/drop.go`的错误码也要对应上

#### trace

数据格式如下

```c
struct trace_notify {
	NOTIFY_CAPTURE_HDR
	__u32		src_label;
	__u32		dst_label;
	__u16		dst_id;
	__u8		reason;
	__u8		ipv6:1;
	__u8		pad:7;
	__u32		ifindex;
	union {
		struct {
			__be32		orig_ip4;
			__u32		orig_pad1;
			__u32		orig_pad2;
			__u32		orig_pad3;
		};
		union v6addr	orig_ip6;
	};
};
```

转发 reason 有以下几种，与 conntrack 状态强相关

```c
// bpf/lib/trace.h
/* Reasons for forwarding a packet, keep in sync with pkg/monitor/datapath_trace.go */
enum trace_reason {
	TRACE_REASON_POLICY = CT_NEW,
	TRACE_REASON_CT_ESTABLISHED = CT_ESTABLISHED,
	TRACE_REASON_CT_REPLY = CT_REPLY,
	TRACE_REASON_CT_RELATED = CT_RELATED,
	TRACE_REASON_RESERVED,
	TRACE_REASON_UNKNOWN,
	TRACE_REASON_SRV6_ENCAP,
	TRACE_REASON_SRV6_DECAP,
	TRACE_REASON_ENCRYPT_OVERLAY,
	/* Note: TRACE_REASON_ENCRYPTED is used as a mask. Beware if you add
	 * new values below it, they would match with that mask.
	 */
	TRACE_REASON_ENCRYPTED = 0x80,
} __packed;
```

观测点

```c
// bpf/lib/trace.h
enum trace_point {
	TRACE_TO_LXC,
	TRACE_TO_PROXY,
	TRACE_TO_HOST,
	TRACE_TO_STACK,
	TRACE_TO_OVERLAY,
	TRACE_FROM_LXC,
	TRACE_FROM_PROXY,
	TRACE_FROM_HOST,
	TRACE_FROM_STACK,
	TRACE_FROM_OVERLAY,
	TRACE_FROM_NETWORK,
	TRACE_TO_NETWORK,
} __packed;
```

trace 的观测点是保存在 common header 的 subtype 字段，代码如下

```c
#define send_trace_notify(ctx, obs_point, src, dst, dst_id, ifindex, reason, monitor) \
		_send_trace_notify(ctx, obs_point, src, dst, dst_id, ifindex, reason, monitor, \
		__MAGIC_LINE__, __MAGIC_FILE__)
static __always_inline void
_send_trace_notify(struct __ctx_buff *ctx, enum trace_point obs_point,
		   __u32 src, __u32 dst, __u16 dst_id, __u32 ifindex,
		   enum trace_reason reason, __u32 monitor, __u16 line, __u8 file)
{
	__u64 ctx_len = ctx_full_len(ctx);
	__u64 cap_len = min_t(__u64, monitor ? : TRACE_PAYLOAD_LEN,
			      ctx_len);
	struct ratelimit_key rkey = {
		.usage = RATELIMIT_USAGE_EVENTS_MAP,
	};
	struct ratelimit_settings settings = {
		.topup_interval_ns = NSEC_PER_SEC,
	};
	struct trace_notify msg __align_stack_8;

	_update_trace_metrics(ctx, obs_point, reason, line, file); // 更新metrics计数

	if (!emit_trace_notify(obs_point, monitor))
		return;

	if (EVENTS_MAP_RATE_LIMIT > 0) {  // 防止报文过多打爆ring环
		settings.bucket_size = EVENTS_MAP_BURST_LIMIT;
		settings.tokens_per_topup = EVENTS_MAP_RATE_LIMIT;
		if (!ratelimit_check_and_take(&rkey, &settings))
			return;
	}

	msg = (typeof(msg)) {
		__notify_common_hdr(CILIUM_NOTIFY_TRACE, obs_point), // subtype是obs_point
		__notify_pktcap_hdr((__u32)ctx_len, (__u16)cap_len, NOTIFY_CAPTURE_VER),
		.src_label	= src,
		.dst_label	= dst,
		.dst_id		= dst_id,
		.reason		= reason,
		.ifindex	= ifindex,
	};
	memset(&msg.orig_ip6, 0, sizeof(union v6addr));

	ctx_event_output(ctx, &EVENTS_MAP,
			 (cap_len << 32) | BPF_F_CURRENT_CPU,
			 &msg, sizeof(msg));
}
```

### monitor socket

cilium daemon 启动时，会启动 monitor-agent，读取 perf event ring 并提供 api 给 cilium-dbg 工具或 envoy 进行连接

![cilium event](/img/blobs/ciliumevent.png)

#### agent 组件

```go
// pkg/monitor/agent/agent.go
type agent struct {
	lock.Mutex
	models.MonitorStatus

	ctx              context.Context
	perfReaderCancel context.CancelFunc

	// listeners are external cilium monitor clients which receive raw
	// gob-encoded payloads
	listeners map[listener.MonitorListener]struct{}
	// consumers are internal clients which receive decoded messages
	consumers map[consumer.MonitorConsumer]struct{}

	events        *ebpf.Map
	monitorEvents *perf.Reader
}
```

#### 读取 perf ring 流程

```go
func (a *agent) handleEvents(stopCtx context.Context) {
	scopedLog := log.WithField(logfields.StartTime, time.Now())
	scopedLog.Info("Beginning to read perf buffer")
	defer scopedLog.Info("Stopped reading perf buffer")

	bufferSize := int(a.Pagesize * a.Npages)
	monitorEvents, err := perf.NewReader(a.events, bufferSize) //初始化reader
	if err != nil {
		scopedLog.WithError(err).Fatal("Cannot initialise BPF perf ring buffer sockets")
	}
	defer func() {
		monitorEvents.Close()
		a.Lock()
		a.monitorEvents = nil
		a.Unlock()
	}()

	a.Lock()
	a.monitorEvents = monitorEvents
	a.Unlock()

	for !isCtxDone(stopCtx) {
		record, err := monitorEvents.Read()
		switch {
		case isCtxDone(stopCtx):
			return
		case err != nil:
			if perf.IsUnknownEvent(err) {
				a.Lock()
				a.MonitorStatus.Unknown++
				a.Unlock()
			} else {
				scopedLog.WithError(err).Warn("Error received while reading from perf buffer")
				if errors.Is(err, unix.EBADFD) {
					return
				}
			}
			continue
		}

		a.processPerfRecord(scopedLog, record) // 解析每个event
	}
}
// processPerfRecord processes a record from the datapath and sends it to any
// registered subscribers
func (a *agent) processPerfRecord(scopedLog *logrus.Entry, record perf.Record) {
	a.Lock()
	defer a.Unlock()

	if record.LostSamples > 0 {
		a.MonitorStatus.Lost += int64(record.LostSamples)
		a.notifyPerfEventLostLocked(record.LostSamples, record.CPU)
		a.sendToListenersLocked(&payload.Payload{
			CPU:  record.CPU,
			Lost: record.LostSamples,
			Type: payload.RecordLost,
		})

	} else {
		a.notifyPerfEventLocked(record.RawSample, record.CPU)
		a.sendToListenersLocked(&payload.Payload{ // 广播到所有listener，也就是客户端
			Data: record.RawSample,
			CPU:  record.CPU, // 每个cpu都有一个record，是独立的
			Type: payload.EventSample,
		})
	}
}

```

每个连接的 client 都会创建一个 listener。listener 会分配一个队列。当一个 event 生成后，event 会被广播到所有 listener 的队列中，队列中的 event 会被 listener 消费并发送给 client。发送给 client 的数据都是 raw data，需要 client 自行解析

```go
// listenerv1_2 implements the cilium-node-monitor API protocol compatible with
// cilium 1.2
// cleanupFn is called on exit
type listenerv1_2 struct {
	conn      net.Conn
	queue     chan *payload.Payload
	cleanupFn func(listener.MonitorListener)
	// Used to prevent queue from getting closed multiple times.
	once sync.Once
}

func newListenerv1_2(c net.Conn, queueSize int, cleanupFn func(listener.MonitorListener)) *listenerv1_2 {
	ml := &listenerv1_2{
		conn:      c,
		queue:     make(chan *payload.Payload, queueSize),
		cleanupFn: cleanupFn,
	}

	go ml.drainQueue()

	return ml
}

func (ml *listenerv1_2) drainQueue() {
	defer func() {
		ml.cleanupFn(ml)
	}()

	enc := gob.NewEncoder(ml.conn)
	for pl := range ml.queue {
		if err := pl.EncodeBinary(enc); err != nil { //写到socket
			switch {
			case listener.IsDisconnected(err):
				log.Debug("Listener disconnected")
				return

			default:
				log.WithError(err).Warn("Removing listener due to write failure")
				return
			}
		}
	}
}
```

具体报文解析代码位于`pkg/monitor/format/format.go`

#### perf.Reader 的实现

下面详细展开一下 cilium 对于 perf.Reader 的实现

主要流程：

1. 为每个 cpu 创建 perf event
2. perf event 的 fd 做 mmap 映射，拿到内存地址
3. fd 加到 epoll
4. 启动 readInto，大部分时候 epoll wait 等待，直到有 epoll 事件时从 ring 中读取 event

```go
func NewReaderWithOptions(array *ebpf.Map, perCPUBuffer int, opts ReaderOptions) (pr *Reader, err error) {
	closeOnError := func(c io.Closer) {
		if err != nil {
			c.Close()
		}
	}

	if perCPUBuffer < 1 {
		return nil, errors.New("perCPUBuffer must be larger than 0")
	}
	if opts.WakeupEvents > 0 && opts.Watermark > 0 {
		return nil, errors.New("WakeupEvents and Watermark cannot both be non-zero")
	}

	var (
		nCPU     = int(array.MaxEntries())
		rings    = make([]*perfEventRing, 0, nCPU)
		eventFds = make([]*sys.FD, 0, nCPU)
	)

	poller, err := epoll.New() // 使用epoll读取fd
	if err != nil {
		return nil, err
	}
	defer closeOnError(poller)

	// bpf_perf_event_output checks which CPU an event is enabled on,
	// but doesn't allow using a wildcard like -1 to specify "all CPUs".
	// Hence we have to create a ring for each CPU.
	bufferSize := 0
	for i := 0; i < nCPU; i++ { //编译所有可能的cpu
		event, ring, err := newPerfEventRing(i, perCPUBuffer, opts)
		if errors.Is(err, unix.ENODEV) {
			// The requested CPU is currently offline, skip it.
			continue
		}

		if err != nil {
			return nil, fmt.Errorf("failed to create perf ring for CPU %d: %v", i, err)
		}
		defer closeOnError(event)
		defer closeOnError(ring)

		bufferSize = ring.size()
		rings = append(rings, ring)
		eventFds = append(eventFds, event)
		/* 将fd加入到epoll */
		if err := poller.Add(event.Int(), 0); err != nil {
			return nil, err
		}
	}

	// Closing a PERF_EVENT_ARRAY removes all event fds
	// stored in it, so we keep a reference alive.
	array, err = array.Clone()
	if err != nil {
		return nil, err
	}

	pr = &Reader{
		array:        array,
		rings:        rings,
		poller:       poller,
		deadline:     time.Time{},
		epollEvents:  make([]unix.EpollEvent, len(rings)),
		epollRings:   make([]*perfEventRing, 0, len(rings)),
		eventHeader:  make([]byte, perfEventHeaderSize),
		eventFds:     eventFds,
		overwritable: opts.Overwritable,
		bufferSize:   bufferSize,
	}
	if err = pr.Resume(); err != nil {
		return nil, err
	}
	runtime.SetFinalizer(pr, (*Reader).Close)
	return pr, nil
}

func newPerfEventRing(cpu, perCPUBuffer int, opts ReaderOptions) (_ *sys.FD, _ *perfEventRing, err error) {
	closeOnError := func(c io.Closer) {
		if err != nil {
			c.Close()
		}
	}

	if opts.Watermark >= perCPUBuffer {
		return nil, nil, errors.New("watermark must be smaller than perCPUBuffer")
	}

	fd, err := createPerfEvent(cpu, opts) //创建perfEvent，得到对应的fd
	if err != nil {
		return nil, nil, err
	}
	defer closeOnError(fd)

	if err := unix.SetNonblock(fd.Int(), true); err != nil {
		return nil, nil, err
	}

	protections := unix.PROT_READ
	if !opts.Overwritable {
		protections |= unix.PROT_WRITE
	}

	mmap, err := unix.Mmap(fd.Int(), 0, perfBufferSize(perCPUBuffer), protections, unix.MAP_SHARED) // mmap到该ring的地址空间
	if err != nil {
		return nil, nil, fmt.Errorf("can't mmap: %v", err)
	}

	// This relies on the fact that we allocate an extra metadata page,
	// and that the struct is smaller than an OS page.
	// This use of unsafe.Pointer isn't explicitly sanctioned by the
	// documentation, since a byte is smaller than sampledPerfEvent.
	meta := (*unix.PerfEventMmapPage)(unsafe.Pointer(&mmap[0]))

	var reader ringReader
	if opts.Overwritable {
		reader = newReverseReader(meta, mmap[meta.Data_offset:meta.Data_offset+meta.Data_size])
	} else {
		reader = newForwardReader(meta, mmap[meta.Data_offset:meta.Data_offset+meta.Data_size])
	}

	ring := &perfEventRing{
		cpu:        cpu,
		mmap:       mmap,
		ringReader: reader,
	}
	runtime.SetFinalizer(ring, (*perfEventRing).Close)

	return fd, ring, nil
}



func (pr *Reader) ReadInto(rec *Record) error {
	pr.mu.Lock()
	defer pr.mu.Unlock()

	pr.pauseMu.Lock()
	defer pr.pauseMu.Unlock()

	if pr.overwritable && !pr.paused {
		return errMustBePaused
	}

	if pr.rings == nil {
		return fmt.Errorf("perf ringbuffer: %w", ErrClosed)
	}

	for {
		if len(pr.epollRings) == 0 {
			if pe := pr.pendingErr; pe != nil {
				// All rings have been emptied since the error occurred, return
				// appropriate error.
				pr.pendingErr = nil
				return pe
			}

			// NB: The deferred pauseMu.Unlock will panic if Wait panics, which
			// might obscure the original panic.
			pr.pauseMu.Unlock()
			_, err := pr.poller.Wait(pr.epollEvents, pr.deadline)
			pr.pauseMu.Lock()

			if errors.Is(err, os.ErrDeadlineExceeded) || errors.Is(err, ErrFlushed) {
				// We've hit the deadline, check whether there is any data in
				// the rings that we've not been woken up for.
				pr.pendingErr = err
			} else if err != nil {
				return err
			}

			// Re-validate pr.paused since we dropped pauseMu.
			if pr.overwritable && !pr.paused {
				return errMustBePaused
			}

			// Waking up userspace is expensive, make the most of it by checking
			// all rings.
			for _, ring := range pr.rings {
				ring.loadHead()
				pr.epollRings = append(pr.epollRings, ring)
			}
		}

		// Start at the last available event. The order in which we
		// process them doesn't matter, and starting at the back allows
		// resizing epollRings to keep track of processed rings.
		err := pr.readRecordFromRing(rec, pr.epollRings[len(pr.epollRings)-1])
		if err == errEOR {
			// We've emptied the current ring buffer, process
			// the next one.
			pr.epollRings = pr.epollRings[:len(pr.epollRings)-1]
			continue
		}

		return err
	}
}
```

### monitor 样例输出

本人开发的基于 cilium 的魔改版本 😉（实现基本的 vpc 功能）

![trace](/img/blobs/trace.PNG)
