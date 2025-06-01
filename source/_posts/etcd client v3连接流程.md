---
title: "etcd client v3 连接流程"
date: 2022-10-28T09:15:00+08:00
draft: false
tags: ["go", "etcd", "grpc"]
tags_weight: 66
series: ["etcd系列"]
series_weight: 96
categories: ["问题分析"]
categoryes_weight: 96
---

<!-- more -->

首先需要了解 grpc 框架的一些概念，这边引用网上的一张图
![Alt text](/img/blobs/etcdclient.png)

### Resolver

提供一个用户自定义的解析、修改地址的方法，使得用户可以自己去实现地址解析的逻辑、做服务发现、地址更新等等功能。

1. 将 Endpoints 里的 ETCD 服务器地址(127.0.0.1:2379 这种格式)做一次转换传给 grpc 框架。也可以自己重新写此 resolver，做服务发现功能。例如 etcd 服务器地址写 nacos 之类的地址，在 resolver 中写好转换逻辑。
2. 调用 ClientConn 的 ParseServiceConfig 接口告诉 endpoints 的负载策略是轮询

### Balancer

1. 管理 subConns，并收集各 conn 信息，更新状态至 ClientConn
2. 生成 picker(balancer)的快照，从而 ClientConn 可以选择发送 rpc 请求的 subConn

此处 etcd client 没有实现 balancer，默认使用 grpc 提供的轮询的 balancer

### 重试策略

与一般的 c-s 模型不同，etcd client 的重试是针对集群的重试。单个节点的断连不会造成所有节点的重连。

#### 重试机制

一般的重试是对同一个节点进行重试，但 etcd client 的自动重试不会在 ETCD 集群的同一节点上进行，是轮询重试集群的每个节点。重试时不会重新建连，而是使用 balancer 提供的 transport。transport 的状态更新与这一块的重试是通过 balancer 解耦的。

#### 重试条件

##### etcd unary 拦截器

拦截器类似 http 里的中间件的概念，在发送实际请求之前对报文进行篡改。一般用来添加认证，日志记录，缓存之类的功能。

此处 etcd 的一元拦截器主要做了自动重试的功能，且只会重试一些特定的错误(DeadlineExceeded, Canceled,ErrInvalidAuthToken)

```go
func (c *Client) unaryClientInterceptor(optFuncs ...retryOption) grpc.UnaryClientInterceptor {

    ...
if isContextError(lastErr) {
				if ctx.Err() != nil {
					// its the context deadline or cancellation.
					return lastErr
				}
				// its the callCtx deadline or cancellation, in which case try again.
				continue
			}
    if callOpts.retryAuth && rpctypes.Error(lastErr) == rpctypes.ErrInvalidAuthToken {
				// clear auth token before refreshing it.
				// call c.Auth.Authenticate with an invalid token will always fail the auth check on the server-side,
				// if the server has not apply the patch of pr #12165 (https://github.com/etcd-io/etcd/pull/12165)
				// and a rpctypes.ErrInvalidAuthToken will recursively call c.getToken until system run out of resource.
				c.authTokenBundle.UpdateAuthToken("")

				gterr := c.getToken(ctx)
				if gterr != nil {
					c.GetLogger().Warn(
						"retrying of unary invoker failed to fetch new auth token",
						zap.String("target", cc.Target()),
						zap.Error(gterr),
					)
					return gterr // lastErr must be invalid auth token
				}
				continue
			}
    ...
}
```

#### 重试次数

此处 Invoke 的大循环里，默认 callOpts.max 是 0，也就是说尝试一次 Invoke 后就会 return 错误

```go
var (
   defaultOptions = &options{
      retryPolicy: nonRepeatable,
      max:         0, // disable
      backoffFunc: backoffLinearWithJitter(50*time.Millisecond /*jitter*/, 0.10),
      retryAuth:   true,
   }
)
```

#### 重试时间

若重试次数已经达到 quorum，则真正的计算间隔时长，间隔时长(25ms 左右)到期后，才进行重试。

否则，直接返回 0，也就是马上重试。

```go
func (c *Client) roundRobinQuorumBackoff(waitBetween time.Duration,
    jitterFraction float64) backoffFunc {

	return func(attempt uint) time.Duration {
		n := uint(len(c.Endpoints()))
		quorum := (n/2 + 1)
		if attempt%quorum == 0 {
			c.lg.Debug("backoff", zap.Uint("attempt", attempt), zap.Uint("quorum", quorum), zap.Duration("waitBetween", waitBetween), zap.Float64("jitterFraction", jitterFraction))
			return jitterUp(waitBetween, jitterFraction)
		}
		c.lg.Debug("backoff skipped", zap.Uint("attempt", attempt), zap.Uint("quorum", quorum))
		return 0
	}
}
```

### 问题分析

根据以上基础，分析 etcd client 启动后修改 hosts 文件产生错误的原因

#### 测试环境

etcd 集群 1：10.50.44.196（未对 client 开放端口），10.50.44.174（未对 client 开放端口）， 10.50.44.170

etcd 集群 2：10.50.44.65，10.50.44.69，10.50.44.53

其中 etcd 集群 1 的 10.50.44.196 与 10.50.44.174 未对 etcd client 所在的机器开放端口

#### 初始阶段

最开始时，/etc/hosts 文件配置的地址如下

```
10.50.44.196 etcdserver1.com
10.50.44.174 etcdserver2.com
10.50.44.170 etcdserver3.com
```

此时从日志中可以看到未开放端口的两个节点对应的 subchannel 一直处于 TRANSIENT_FAILURE 和 CONNECTING 状态。

```shell
time="2022-10-20 14:06:05.93148" level=warning msg="[core] grpc: addrConn.createTransport failed to connect to {etcdserver3.com:42379 etcdserver3.com <nil> 0 <nil>}. Err:
connection error: desc = \"transport: Error while dialing dial tcp 10.50.44.174:42379: connect: connection refused\". Reconnecting..." logger=grpc-server
time="2022-10-20 14:06:05.93152" level=info msg="[core] Subchannel Connectivity change to TRANSIENT_FAILURE" logger=grpc-server
time="2022-10-20 14:06:10.33892" level=info msg="[core] Subchannel Connectivity change to CONNECTING" logger=grpc-server
time="2022-10-20 14:06:10.33898" level=info msg="[core] Subchannel picks a new address \"etcdserver2.com:42379\" to connect" logger=grpc-server
time="2022-10-20 14:06:10.33946" level=warning msg="[core] grpc: addrConn.createTransport failed to connect to {etcdserver2.com:42379 etcdserver2.com <nil> 0 <nil>}. Err:
connection error: desc = \"transport: Error while dialing dial tcp 10.50.44.196:42379: connect: connection refused\". Reconnecting..." logger=grpc-server
time="2022-10-20 14:06:10.33949" level=info msg="[core] Subchannel Connectivity change to TRANSIENT_FAILURE" logger=grpc-server
time="2022-10-20 14:06:24.52740" level=info msg="[core] Subchannel Connectivity change to CONNECTING" logger=grpc-server
time="2022-10-20 14:06:24.52750" level=info msg="[core] Subchannel picks a new address \"etcdserver3.com:42379\" to connect" logger=grpc-server
time="2022-10-20 14:06:24.52810" level=warning msg="[core] grpc: addrConn.createTransport failed to connect to {etcdserver3.com:42379 etcdserver3.com <nil> 0 <nil>}. Err:
connection error: desc = \"transport: Error while dialing dial tcp 10.50.44.174:42379: connect: connection refused\". Reconnecting..." logger=grpc-server
time="2022-10-20 14:06:24.52814" level=info msg="[core] Subchannel Connectivity change to TRANSIENT_FAILURE" logger=grpc-server
time="2022-10-20 14:06:25.98825" level=info msg="[core] Subchannel Connectivity change to CONNECTING" logger=grpc-server
time="2022-10-20 14:06:25.98833" level=info msg="[core] Subchannel picks a new address \"etcdserver2.com:42379\" to connect" logger=grpc-server
time="2022-10-20 14:06:25.98891" level=warning msg="[core] grpc: addrConn.createTransport failed to connect to {etcdserver2.com:42379 etcdserver2.com <nil> 0 <nil>}. Err:
connection error: desc = \"transport: Error while dialing dial tcp 10.50.44.196:42379: connect: connection refused\". Reconnecting..." logger=grpc-server
time="2022-10-20 14:06:25.98897" level=info msg="[core] Subchannel Connectivity change to TRANSIENT_FAILURE" logger=grpc-server
time="2022-10-20 14:06:47.80826" level=info msg="[core] Subchannel Connectivity change to CONNECTING" logger=grpc-server
time="2022-10-20 14:06:47.80834" level=info msg="[core] Subchannel picks a new address \"etcdserver2.com:42379\" to connect" logger=grpc-server
time="2022-10-20 14:06:47.81473" level=info msg="[core] Subchannel Connectivity change to READY" logger=grpc-server
time="2022-10-20 14:06:47.81483" level=info msg="[roundrobin] roundrobinPicker: newPicker called with info: {map[0xc000504330:{{etcdserver1.com:42379 etcdserver1.com <nil>
 0 <nil>}} 0xc0005043d0:{{etcdserver2.com:42379 etcdserver2.com <nil> 0 <nil>}}]}" logger=grpc-server
time="2022-10-20 14:06:51.54254" level=info msg="[core] Subchannel Connectivity change to CONNECTING" logger=grpc-server
time="2022-10-20 14:06:51.54259" level=info msg="[core] Subchannel picks a new address \"etcdserver3.com:42379\" to connect" logger=grpc-server
time="2022-10-20 14:06:51.54962" level=info msg="[core] Subchannel Connectivity change to READY" logger=grpc-server
```

subchannel 的状态在框架内由 addrConn 控制，创建 ClientConn 后会从 balancer 中返回一系列 endpoint 的地址（状态是不可用的）。之后对于每个地址建立连接(addrConn.resetTransport)，并更新状态。balancer 将状态为 READY 的连接（只有 etcdserver3.com）封装成 picker 提供给 client.Conn 来连接。

addrConn.resetTransport 里面是一个大循环，对 endpoint 列表里所有地址重试建立 http2 连接直到成功(newHTTP2Client)。

1. net.dial 建立 tcp 连接，失败的话返回 TransientFailure
2. http2 的流操作：单独启动 goroutine，循环读取所有的 frame，并发送到相应的 stream 中去

默认情况下连接失败会将 addrConn 的状态修改为 TransientFailure，暂时不让 ClientConn 使用，并等待 sleeptime 后重试。整个连接尝试持续 minConnectTimeout(20s)。连接成功后将 addrConn 状态置为 ready，下次就可以从 balancer 中读取到这个地址。

sleeptime 计算规则如下，默认 BaseDelay = 1s，Multiplier=1.6，且有 0.2 的 Jitter

```go
// Backoff returns the amount of time to wait before the next retry given the
// number of retries.
func (bc Exponential) Backoff(retries int) time.Duration {
	if retries == 0 {
		return bc.Config.BaseDelay
	}
	backoff, max := float64(bc.Config.BaseDelay), float64(bc.Config.MaxDelay)
	for backoff < max && retries > 0 {
		backoff *= bc.Config.Multiplier
		retries--
	}
	if backoff > max {
		backoff = max
	}
	// Randomize backoff delays so that if a cluster of requests start at
	// the same time, they won't operate in lockstep.
	backoff *= 1 + bc.Config.Jitter*(grpcrand.Float64()*2-1)
	if backoff < 0 {
		return 0
	}
	return time.Duration(backoff)
}
```

测试可以看到每次重连以 1，2，4，8 的间隔进行重试，且每 20s 使用一个新端口建连。

```shell
02:59:53.590387 IP 10.20.141.244.44904 > 10.50.66.67.42379: Flags [S], seq 2780622427, win 29200, options [mss 1460,sackOK,TS val 5912872 ecr 0,nop,wscale 7], length 0
02:59:54.592062 IP 10.20.141.244.44904 > 10.50.66.67.42379: Flags [S], seq 2780622427, win 29200, options [mss 1460,sackOK,TS val 5913874 ecr 0,nop,wscale 7], length 0
02:59:56.598068 IP 10.20.141.244.44904 > 10.50.66.67.42379: Flags [S], seq 2780622427, win 29200, options [mss 1460,sackOK,TS val 5915880 ecr 0,nop,wscale 7], length 0
03:00:00.606135 IP 10.20.141.244.44904 > 10.50.66.67.42379: Flags [S], seq 2780622427, win 29200, options [mss 1460,sackOK,TS val 5919888 ecr 0,nop,wscale 7], length 0
03:00:08.622076 IP 10.20.141.244.44904 > 10.50.66.67.42379: Flags [S], seq 2780622427, win 29200, options [mss 1460,sackOK,TS val 5927904 ecr 0,nop,wscale 7], length 0
03:00:14.599452 IP 10.20.141.244.44910 > 10.50.66.67.42379: Flags [S], seq 1525024322, win 29200, options [mss 1460,sackOK,TS val 5933881 ecr 0,nop,wscale 7], length 0
03:00:15.602123 IP 10.20.141.244.44910 > 10.50.66.67.42379: Flags [S], seq 1525024322, win 29200, options [mss 1460,sackOK,TS val 5934884 ecr 0,nop,wscale 7], length 0
03:00:17.606062 IP 10.20.141.244.44910 > 10.50.66.67.42379: Flags [S], seq 1525024322, win 29200, options [mss 1460,sackOK,TS val 5936888 ecr 0,nop,wscale 7], length 0
03:00:21.614064 IP 10.20.141.244.44910 > 10.50.66.67.42379: Flags [S], seq 1525024322, win 29200, options [mss 1460,sackOK,TS val 5940896 ecr 0,nop,wscale 7], length 0
03:00:29.630069 IP 10.20.141.244.44910 > 10.50.66.67.42379: Flags [S], seq 1525024322, win 29200, options [mss 1460,sackOK,TS val 5948912 ecr 0,nop,wscale 7], length 0
03:00:35.912848 IP 10.20.141.244.44914 > 10.50.66.67.42379: Flags [S], seq 2900573510, win 29200, options [mss 1460,sackOK,TS val 5955194 ecr 0,nop,wscale 7], length 0
03:00:36.914126 IP 10.20.141.244.44914 > 10.50.66.67.42379: Flags [S], seq 2900573510, win 29200, options [mss 1460,sackOK,TS val 5956196 ecr 0,nop,wscale 7], length 0
03:00:38.918090 IP 10.20.141.244.44914 > 10.50.66.67.42379: Flags [S], seq 2900573510, win 29200, options [mss 1460,sackOK,TS val 5958200 ecr 0,nop,wscale 7], length 0
03:00:42.926125 IP 10.20.141.244.44914 > 10.50.66.67.42379: Flags [S], seq 2900573510, win 29200, options [mss 1460,sackOK,TS val 5962208 ecr 0,nop,wscale 7], length 0
03:00:50.942069 IP 10.20.141.244.44914 > 10.50.66.67.42379: Flags [S], seq 2900573510, win 29200, options [mss 1460,sackOK,TS val 5970224 ecr 0,nop,wscale 7], length 0

```

#### 变更 hosts 地址

第二步，修改 hosts 文件

```shell
10.50.44.65 etcdserver1.com
10.50.44.69 etcdserver2.com
10.50.44.53 etcdserver3.com
```

此时连接建立(grpc.dial)成功，调用 etcd 的 put，get 等请求时本质上是发起 grpc.Invoke 请求。

grpc 请求分为 stream 和 unary，这里属于 unary 请求

1. 调用拦截器 unaryInterceptor
2. 在拦截器里调用 Invoke 处理请求过程
   1. clientConn.getTransport 获取一个连接，出错直接返回
      1. 轮询从 balancer 中获取一个可用的地址
      2. adressConn.Wait 等待连接
   2. sendRequest 发起请求，生成一个 stream 对象
   3. recvResponse 接收响应，阻塞等待

可以发现日志的前一部分报 code = DeadlineExceeded，context deadline exceeded，后一部分开始报 code=InvalidArgument， user name is empty，且之后一直报 user name is empty 这个错误。报错的 endpoint 为之前未连接成功的 etcderver1.com。

```shell
{"level":"warn","ts":"2022-10-20T14:06:53.839+0800","logger":"etcd-client","caller":"v3@v3.5.1/retry_interceptor.go:62","msg":"retrying of unary invoker failed","target":"
etcd-endpoints://0xc000511340/etcdserver1.com:42379","attempt":0,"error":"rpc error: code = Unauthenticated desc = etcdserver: invalid auth token"}
{"level":"warn","ts":"2022-10-20T14:06:53.876+0800","logger":"etcd-client","caller":"v3@v3.5.1/retry_interceptor.go:62","msg":"retrying of unary invoker failed","target":"
etcd-endpoints://0xc000511340/etcdserver1.com:42379","attempt":0,"error":"rpc error: code = InvalidArgument desc = etcdserver: user name is empty"}
time="2022-10-20 14:06:53.87629" level=error msg="etcd get error: etcdserver: user name is empty" logger=etcd
{"level":"warn","ts":"2022-10-20T14:06:57.033+0800","logger":"etcd-client","caller":"v3@v3.5.1/retry_interceptor.go:62","msg":"retrying of unary invoker failed","target":"
etcd-endpoints://0xc000511340/etcdserver1.com:42379","attempt":0,"error":"rpc error: code = DeadlineExceeded desc = context deadline exceeded"}
time="2022-10-20 14:06:57.03316" level=error msg="etcd put error: context deadline exceeded" logger=etcd
{"level":"warn","ts":"2022-10-20T14:07:00.202+0800","logger":"etcd-client","caller":"v3@v3.5.1/retry_interceptor.go:62","msg":"retrying of unary invoker failed","target":"
etcd-endpoints://0xc000511340/etcdserver1.com:42379","attempt":0,"error":"rpc error: code = DeadlineExceeded desc = context deadline exceeded"}
time="2022-10-20 14:07:00.20305" level=error msg="etcd put error: context deadline exceeded" logger=etcd
{"level":"warn","ts":"2022-10-20T14:07:03.236+0800","logger":"etcd-client","caller":"v3@v3.5.1/retry_interceptor.go:62","msg":"retrying of unary invoker failed","target":"
etcd-endpoints://0xc000511340/etcdserver1.com:42379","attempt":0,"error":"rpc error: code = DeadlineExceeded desc = context deadline exceeded"}
time="2022-10-20 14:07:03.23630" level=error msg="etcd put error: context deadline exceeded" logger=etcd
{"level":"warn","ts":"2022-10-20T14:07:06.243+0800","logger":"etcd-client","caller":"v3@v3.5.1/retry_interceptor.go:62","msg":"retrying of unary invoker failed","target":"
etcd-endpoints://0xc000511340/etcdserver1.com:42379","attempt":0,"error":"rpc error: code = Unauthenticated desc = etcdserver: invalid auth token"}
{"level":"warn","ts":"2022-10-20T14:07:06.243+0800","logger":"etcd-client","caller":"v3@v3.5.1/retry_interceptor.go:62","msg":"retrying of unary invoker failed","target":"
etcd-endpoints://0xc000511340/etcdserver1.com:42379","attempt":0,"error":"rpc error: code = DeadlineExceeded desc = context deadline exceeded"}
{"level":"warn","ts":"2022-10-20T14:07:06.243+0800","logger":"etcd-client","caller":"v3@v3.5.1/retry_interceptor.go:85","msg":"retrying of unary invoker failed to fetch ne
w auth token","target":"etcd-endpoints://0xc000511340/etcdserver1.com:42379","error":"context deadline exceeded"}
time="2022-10-20 14:07:06.24336" level=error msg="etcd put error: context deadline exceeded" logger=etcd
{"level":"warn","ts":"2022-10-20T14:07:06.273+0800","logger":"etcd-client","caller":"v3@v3.5.1/retry_interceptor.go:62","msg":"retrying of unary invoker failed","target":"
etcd-endpoints://0xc000511340/etcdserver1.com:42379","attempt":0,"error":"rpc error: code = InvalidArgument desc = etcdserver: user name is empty"}
time="2022-10-20 14:07:06.27355" level=error msg="etcd put error: etcdserver: user name is empty" logger=etcd
{"level":"warn","ts":"2022-10-20T14:07:06.326+0800","logger":"etcd-client","caller":"v3@v3.5.1/retry_interceptor.go:62","msg":"retrying of unary invoker failed","target":"
etcd-endpoints://0xc000511340/etcdserver1.com:42379","attempt":0,"error":"rpc error: code = InvalidArgument desc = etcdserver: user name is empty"}
time="2022-10-20 14:07:06.32669" level=error msg="etcd put error: etcdserver: user name is empty" logger=etcd
{"level":"warn","ts":"2022-10-20T14:07:06.355+0800","logger":"etcd-client","caller":"v3@v3.5.1/retry_interceptor.go:62","msg":"retrying of unary invoker failed","target":"
etcd-endpoints://0xc000511340/etcdserver1.com:42379","attempt":0,"error":"rpc error: code = InvalidArgument desc = etcdserver: user name is empty"}
time="2022-10-20 14:07:06.35588" level=error msg="etcd put error: etcdserver: user name is empty" logger=etcd
{"level":"warn","ts":"2022-10-20T14:07:06.432+0800","logger":"etcd-client","caller":"v3@v3.5.1/retry_interceptor.go:62","msg":"retrying of unary invoker failed","target":"
etcd-endpoints://0xc000511340/etcdserver1.com:42379","attempt":0,"error":"rpc error: code = InvalidArgument desc = etcdserver: user name is empty"}
time="2022-10-20 14:07:06.43230" level=error msg="etcd put error: etcdserver: user name is empty" logger=etcd
```

可以看到每次 retry 时使用的 client 是同一个 0xc000511340，表示没有重新创建 clientConn。每次的 attempt 都是 0，表示每次尝试都只尝试一次直接退出了

前一部分错误是因为 client 使用老的 transport，但修改了 hosts 文件，导致无法建连，因此超过 opTimeout(这里设置的 3s)。客户端代码如下

```go
func (db *BytesConnectionEtcd) Put(key string, binData []byte, opts ...datasync.PutOption) error {
	return putInternal(db.Logger, db.etcdClient, db.lessor, db.opTimeout, db.session, key, binData, opts...)
}

func putInternal(log logging.Logger, kv clientv3.KV, lessor clientv3.Lease, opTimeout time.Duration, session *concurrency.Session, key string,
	binData []byte, opts ...datasync.PutOption) error {

	deadline := time.Now().Add(opTimeout)
	ctx, cancel := context.WithDeadline(context.Background(), deadline)
	defer cancel()

	var etcdOpts []clientv3.OpOption
	for _, o := range opts {
		if withTTL, ok := o.(*datasync.WithTTLOpt); ok && withTTL.TTL > 0 {
			lease, err := lessor.Grant(ctx, int64(withTTL.TTL/time.Second))
			if err != nil {
				return err
			}

			etcdOpts = append(etcdOpts, clientv3.WithLease(lease.ID))
		} else if _, ok := o.(*datasync.WithClientLifetimeTTLOpt); ok && session != nil {
			etcdOpts = append(etcdOpts, clientv3.WithLease(session.Lease()))
		}
	}

	if _, err := kv.Put(ctx, key, string(binData), etcdOpts...); err != nil {
		log.Error("etcd put error: ", err)
		return err
	}

	return nil
}
```

后一部分的 user name is empty，由于使用旧的 token 连接新的集群，首次会报 code = Unauthenticated desc = etcdserver: invalid auth token"。之后走下面分支重新获取 token

```go
if callOpts.retryAuth && rpctypes.Error(lastErr) == rpctypes.ErrInvalidAuthToken {
   // clear auth token before refreshing it.
   // call c.Auth.Authenticate with an invalid token will always fail the auth check on the server-side,
   // if the server has not apply the patch of pr #12165 (https://github.com/etcd-io/etcd/pull/12165)
   // and a rpctypes.ErrInvalidAuthToken will recursively call c.getToken until system run out of resource.
   c.authTokenBundle.UpdateAuthToken("")

   gterr := c.getToken(ctx)
   if gterr != nil {
      c.GetLogger().Warn(
         "retrying of unary invoker failed to fetch new auth token",
         zap.String("target", cc.Target()),
         zap.Error(gterr),
      )
      return gterr // lastErr must be invalid auth token
   }
   continue
}
```

重新获取 token 时，显示 user name 被清空，但走读代码没有发现清空 user name 的部分。该问题先 mark，需要后续 debug 一下代码来分析。
