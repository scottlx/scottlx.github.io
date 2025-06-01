---
title: "dpdk rcu lib"
date: 2023-12-08T17:48:00+08:00
draft: false
tags: ["dpdk", "rcu", "高性能网络"]
tags_weight: 66
series: ["dpdk系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
---

rcu 思路分析

<!-- more -->

linux 的 RCU 主要针对的数据对象是链表，目的是提高遍历读取数据的效率，为了达到目的使用 RCU 机制读取数据的时候不对链表进行耗时的加锁操作。这样在同一时间可以有多个线程同时读取该链表，并且允许一个线程对链表进行修改。RCU 适用于需要频繁的读取数据，而相应修改数据并不多的情景。

dpdk 中由于 writer 和 reader 同时访问一段内存，删除元素的时候需要确保

1. 删除时不会将内存 put 回 allocator，而是删掉这段内存的引用。这样确保了新的访问者不会拿到这个元素的引用，而老的访问者不会在访问过程中 core 掉
2. 只有在元素没有任何引用计数时，才释放掉该元素的内存

静默期是指线程没有持有共享内存的引用的时期，也就是下图绿色的时期

![rcu](https://doc.dpdk.org/guides/_images/rcu_general_info.svg)

上图中，有三个 read thread，T1， T2，T3。两条黑色竖线分别代表 writer 执行 delete 和 free 的时刻。

执行 delete 时，T1 和 T2 还拿着 entry1 和 entry2 的 reference，此时 writer 还不能 free entry1 或 entry2 的内存，只能删除元素的引用.

writer**_必须等到执行 delete 时，当时引用该元素的的线程，都完成了一个静默期之后_**，才可以 free 这个内存。

writer 不需要等 T3 进入静默期，因为执行 delete 时，T3 还在静默期。

如何实现 RCU 机制

1. writer 需要一直轮询 reader 的状态，看是否进入静默期。这样会导致一直循环轮询，造成额外的 cpu 消耗。由于需要等 reader 的静默期结束，reader 的静默期越长，reader 的数量越多，writer cpu 的消耗会越大，因此我们需要短的 grace period。但是如果将 reader 的 critical section 减小，虽然 writer 的轮询变快了，但是 reader 的报告次数增加，reader 的 cpu 消耗会增加，因此我们需要长的 critical section。这两者之间看似矛盾。
2. 长的 critical section：dpdk 的 lcore 一般都是一个 while 循环。循环的开始和结束必定是静默期。循环的过程中肯定是在访问各种各样的共享内存。因此 critical section 的粒度可以不要很细，不要每次访问的时候退出静默期，不访问的时候进入静默期，而是将整个循环认为是 critical section，只有在循环的开始退出静默期，循环的结束进入静默期。
3. 短的 grace period：如果是 pipeline 模型，并不是所有 worker 都会使用相同的数据结构。话句话说，同一个元素，只会被部分的 worker 所引用和读取。因此 writer 不需要等到所有 worker 的 critical section 结束，而是使用该元素的 worker 结束 critical section。这样将 grace period 粒度变小之后，缩短了 writer 整体的 grace period。这种粒度的控制是通过 qsbr 实现的

## 如何使用 rcu 库

dpdk-stable-20.11.1/app/test/test_rcu_qsbr.c test_rcu_qsbr_sw_sv_3qs

先创建出 struct rte_rcu_qsbr

```c
    sz = rte_rcu_qsbr_get_memsize(RTE_MAX_LCORE);
    rv = (struct rte_rcu_qsbr *)rte_zmalloc(NULL, sz, RTE_CACHE_LINE_SIZE);
```

再初始化 QS variable

```c
    rte_rcu_qsbr_init(rv, RTE_MAX_LCORE);
```

Reader 注册自己的线程号，并上线（将自己加到 writer 的轮询队列里面）
online 时会原子读 qsbr 里的 token，并设置到 v->qsbr_cnt[thread_id].cnt 中

```c
(void)rte_rcu_qsbr_thread_register(rv, lcore_id);
rte_rcu_qsbr_thread_online(rv, lcore_id);
```

每次读取共享数据后，更新自己的静默状态（rte_rcu_qsbr_quiescent）

```c
    do {
        for (i = 0; i < num_keys; i += j) {
            for (j = 0; j < QSBR_REPORTING_INTERVAL; j++)
                rte_hash_lookup(tbl_rwc_test_param.h,
                        keys + i + j);
            /* Update quiescent state counter */
            rte_rcu_qsbr_quiescent(rv, lcore_id);
        }
    } while (!writer_done);
```

rte_rcu_qsbr_quiescent 是将 qsbr->token 更新到自己 thread 的 token 里去 v->qsbr_cnt[thread_id].cnt

如果 reader 线程，需要执行一个阻塞的函数，那么他就无法更新自己的静默状态了，这样会导致 writer 那边拿不到状态。此时需要先执行 rte_rcu_qsbr_thread_offline()，将自己从 writer 的轮询队列中退出来，执行完阻塞函数后再 online 加回去。

writer 在有数据更新时，调用 rte_rcu_qsbr_start()触发 reader 的状态上报。支持 reader 往多个 writer 上报状态

rte_rcu_qsbr_start()其实是将 qsbr->token 自增 1，也就是每次更新一个版本号。

rte_rcu_qsbr_check()用来检测是否所有 reader 进入了静默期，如果返回 ok，则 writer 可以释放内存

rte_rcu_qsbr_check()其实是判断 thread bitmap 中所有 reader 的 token 是否大于传入的 token 版本号。如果不是的话就 yield，直到满足条件后退出，并更新 v->acked_token。下一次可以先直接拿 v->acked_token 进行判断，满足就退出，不用每次都遍历所有 reader，类似 fast path。

rte_rcu_qsbr_start()和 rte_rcu_qsbr_check()是解耦开来了，也就是说 writer 可以在两个函数之间做自己的逻辑。比如 start 自增 token 之后，在新的内存里写新的数据，check 阻塞，等所有 reader 过了静默期更新完自己的 token 之后，将 reference 指向新的数据，然后释放老数据的内存。

**writer 删除的过程（单个资源）**

1. 启动 grace period，也就是执行 delete 动作（例如 hash 表删除 key，但不删除 value 指针指向的内存），调用 rte_rcu_qsbr_start()，并记录 token
2. 做其他逻辑
3. rte_rcu_qsbr_check(token)阻塞，依次检查资源的 reader 的状态，过了 grace period 则开始 free，释放内存

**writer 删除的过程（多个资源）**

1. rte_rcu_qsbr_dq_create 创建 qsbr 的 fifo，这边传入释放函数的函数指针 free_fn
2. 启动 grace period，批量执行 delete 动作
3. 将被 delete 的资源存到自己的 fifo 里面（defer queue，一次释放很多 qsbr）
4. 做其他逻辑
5. 调用 rte_rcu_qsbr_dq_reclaim 批量释放资源，里面会循环非阻塞调用 rte_rcu_qsbr_check，根据结果调用释放函数 free_fn（这边和 rte_ring 耦合了，只能释放 rte_ring 里分配的内存）
