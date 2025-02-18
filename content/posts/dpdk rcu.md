---
title: "dpdk rcu lib"
date: 2023-12-08T17:48:00+08:00
draft: false
tags: ["dpdk","rcu","高性能网络"]
tags_weight: 66
series: ["dpdk系列"]
series_weight: 96
categories: ["技术介绍"]
categoryes_weight: 96
---


linux的RCU主要针对的数据对象是链表，目的是提高遍历读取数据的效率，为了达到目的使用RCU机制读取数据的时候不对链表进行耗时的加锁操作。这样在同一时间可以有多个线程同时读取该链表，并且允许一个线程对链表进行修改。RCU适用于需要频繁的读取数据，而相应修改数据并不多的情景。

dpdk中由于writer和reader同时访问一段内存，删除元素的时候需要确保

1. 删除时不会将内存put回allocator，而是删掉这段内存的引用。这样确保了新的访问者不会拿到这个元素的引用，而老的访问者不会在访问过程中core掉
2. 只有在元素没有任何引用计数时，才释放掉该元素的内存

静默期是指线程没有持有共享内存的引用的时期，也就是下图绿色的时期

![rcu](https://doc.dpdk.org/guides/_images/rcu_general_info.svg)


上图中，有三个read thread，T1， T2，T3。两条黑色竖线分别代表writer执行delete和free的时刻。

执行delete时，T1和T2还拿着entry1和entry2的reference，此时writer还不能free entry1或entry2的内存，只能删除元素的引用.

writer***必须等到执行delete时，当时引用该元素的的线程，都完成了一个静默期之后***，才可以free这个内存。

writer不需要等T3进入静默期，因为执行delete时，T3还在静默期。

如何实现RCU机制
1. writer需要一直轮询reader的状态，看是否进入静默期。这样会导致一直循环轮询，造成额外的cpu消耗。由于需要等reader的静默期结束，reader的静默期越长，reader的数量越多，writer cpu的消耗会越大，因此我们需要短的grace period。但是如果将reader的critical section减小，虽然writer的轮询变快了，但是reader的报告次数增加，reader的cpu消耗会增加，因此我们需要长的critical section。这两者之间看似矛盾。	
2. 长的critical section：dpdk的lcore一般都是一个while循环。循环的开始和结束必定是静默期。循环的过程中肯定是在访问各种各样的共享内存。因此critical section的粒度可以不要很细，不要每次访问的时候退出静默期，不访问的时候进入静默期，而是将整个循环认为是critical section，只有在循环的开始退出静默期，循环的结束进入静默期。
3. 短的grace period：如果是pipeline模型，并不是所有worker都会使用相同的数据结构。话句话说，同一个元素，只会被部分的worker所引用和读取。因此writer不需要等到所有worker的critical section结束，而是使用该元素的worker结束critical section。这样将grace period粒度变小之后，缩短了writer整体的grace period。这种粒度的控制是通过 qsbr 实现的

## 如何使用rcu库

dpdk-stable-20.11.1/app/test/test_rcu_qsbr.c    test_rcu_qsbr_sw_sv_3qs

先创建出struct rte_rcu_qsbr
```c
    sz = rte_rcu_qsbr_get_memsize(RTE_MAX_LCORE);
    rv = (struct rte_rcu_qsbr *)rte_zmalloc(NULL, sz, RTE_CACHE_LINE_SIZE);
```


再初始化QS variable
```c
    rte_rcu_qsbr_init(rv, RTE_MAX_LCORE);
```


Reader注册自己的线程号，并上线（将自己加到writer的轮询队列里面）
online时会原子读qsbr里的token，并设置到v->qsbr_cnt[thread_id].cnt中
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

rte_rcu_qsbr_quiescent 是将qsbr->token更新到自己thread的token里去v->qsbr_cnt[thread_id].cnt

如果reader线程，需要执行一个阻塞的函数，那么他就无法更新自己的静默状态了，这样会导致writer那边拿不到状态。此时需要先执行rte_rcu_qsbr_thread_offline()，将自己从writer的轮询队列中退出来，执行完阻塞函数后再online加回去。

writer在有数据更新时，调用rte_rcu_qsbr_start()触发reader的状态上报。支持reader往多个writer上报状态

rte_rcu_qsbr_start()其实是将qsbr->token自增1，也就是每次更新一个版本号。

rte_rcu_qsbr_check()用来检测是否所有reader进入了静默期，如果返回ok，则writer可以释放内存

rte_rcu_qsbr_check()其实是判断thread bitmap中所有reader的token是否大于传入的token版本号。如果不是的话就yield，直到满足条件后退出，并更新v->acked_token。下一次可以先直接拿v->acked_token进行判断，满足就退出，不用每次都遍历所有reader，类似fast path。

rte_rcu_qsbr_start()和rte_rcu_qsbr_check()是解耦开来了，也就是说writer可以在两个函数之间做自己的逻辑。比如start自增token之后，在新的内存里写新的数据，check阻塞，等所有reader过了静默期更新完自己的token之后，将reference指向新的数据，然后释放老数据的内存。

**writer删除的过程（单个资源）**
1. 启动grace period，也就是执行delete动作（例如hash表删除key，但不删除value指针指向的内存），调用rte_rcu_qsbr_start()，并记录token
2. 做其他逻辑
3. rte_rcu_qsbr_check(token)阻塞，依次检查资源的reader的状态，过了grace period则开始free，释放内存

**writer删除的过程（多个资源）**
1. rte_rcu_qsbr_dq_create创建qsbr的fifo，这边传入释放函数的函数指针free_fn
2. 启动grace period，批量执行delete动作
3. 将被delete的资源存到自己的fifo里面（defer queue，一次释放很多qsbr）
4. 做其他逻辑
5. 调用rte_rcu_qsbr_dq_reclaim批量释放资源，里面会循环非阻塞调用rte_rcu_qsbr_check，根据结果调用释放函数free_fn（这边和rte_ring耦合了，只能释放rte_ring里分配的内存）
