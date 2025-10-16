---
title: "vhost-user协商过程"
date: 2025-06-09T18:01:00+08:00
draft: false
tags: ["虚拟化", "spdk", "qemu", "vhost", "virtio"]
categories: ["技术介绍"]
categoryes_weight: 96
---
vhost-user协商过程
<!-- more -->
qemu进程跟spdk target 通过vhost-user协议进行通信，通过共享内存，实现spdk数据直接搬移到guest中。qemu跟spdk的通知机制通过eventfd完成，由于spdk采用polling 模式，因此不需要guest driver发送io submission notification, 后端通过callfd 向guest driver发送中断请求，通知driver处理io
![Alternative Text](/img/blobs/1749463037383.png)
### vhost-user协商
qemu通过unix socket跟后端spdk进行通信，qemu chardev有重连机制，在socket 断连之后，会重新发起连接。
```bash
host:~# taskset -c 2,3 qemu-system-x86_64 \
  --enable-kvm \
  -cpu host -smp 2 \
  -m 1G -object memory-backend-file,id=mem0,size=1G,mem-path=/dev/hugepages,share=on -numa node,memdev=mem0 \
 -drive file=guest_os_image.qcow2,if=none,id=disk \
 -device ide-hd,drive=disk,bootindex=0 \
 -chardev socket,id=spdk_vhost_scsi0,path=/var/tmp/vhost.0 \
 -device vhost-user-scsi-pci,id=scsi0,chardev=spdk_vhost_scsi0,num_queues=2 \
 -chardev socket,id=spdk_vhost_blk0,path=/var/tmp/vhost.1 \
 -device vhost-user-blk-pci,chardev=spdk_vhost_blk0,num-queues=2
```
vhost-user-blk 协商过程可能发生在设备初始化过程中或者sock open事件之后。协商在vhost_user_blk_connect调用。
 ![Alternative Text](/img/blobs/1749463049348.png)
关键函数vhost_dev_init中
 ![Alternative Text](/img/blobs/1749463057731.png)
vhost backend init主要是初始化vhost protocol 协议，VHOST_USER_F_PROTOCOL_FEATURES 是virtio协议中定义用来进行协议特性的标志位，用来初始化设备是否支持多队列以及最大队列数，内存slot个数等，这一位对于guest而言是不可见，protocol 特性对于guest而言也是不可见的，guest driver不需要关心这些。
 ![Alternative Text](/img/blobs/1749463065111.png)
对应spdk里面的log：
 ![Alternative Text](/img/blobs/1749463072369.png)
set_owner对于vhost_user 是直接返回ok
再接下来的动作是获取设备的特性，对vitqueue 初始化，设置vring_call， 这个是vhost-user后端通知guest事件的fd， 初始化时设置的是masked_notifier, 即这个时候是不能发送中断到guest。
 ![Alternative Text](/img/blobs/1749463086163.png)
接下来的操作是获取blk 配置信息，包括磁盘容量，seg_max, size_max 队列数等。
接下来是vhost_user_blk_start, 这个动作触发的时机guest driver跟device完成probe之后，通过设置status 为VIRTIO_CONFIG_S_DRIVER_OK触发，status变量是控制device一个关键变量，设备的start跟stop都是通过它来实现。
![Alternative Text](/img/blobs/1749463094403.png)
![Alternative Text](/img/blobs/1749463100411.png)
![Alternative Text](/img/blobs/1749463107040.png)
vhost_user_blk_start主要流程, 设置host， guest notifier 分别对应guest kick 后端的通知和后端向guest发irq中断通知，设置inflight io，这个特性主要是保存pending io，方便迁移的时候回滚，启动vhost_dev_start ， 这个是关键函数，最后是完成vring call设置。
 ![Alternative Text](/img/blobs/1749463114961.png)
vhost_dev_start 首先设置vhost features， 然后设置mem_table将mem信息传给spdk后端，然后设置virtqueue相关参数，如queue size， vring base， vring base addr等
 ![Alternative Text](/img/blobs/1749463121296.png)
对应spdk侧打印log
 ![Alternative Text](/img/blobs/1749463126717.png)
### vhost-user 重连
vhost-user 重连是qemu chardev 实现的功能，在链接过程中进行检查，如果发现有问题，就会进行超时重新链接，超时时间即reconnect参数指定。blk_realize_connect 在发起连接之前，首先通过qemu_chr_fe_wait_connected 确保socket已连接，然后再尝试连接。等待socket变成connected这个状态是同步， 如果这个时候后端服务没有起来或者没有创建磁盘，那么虚机启动将卡住。
 ![Alternative Text](/img/blobs/1749463133502.png)
过程栈信息：
```c
  tcp_chr_wait_connected ()
  vhost_user_blk_device_realize ()
  virtio_device_realize ()
  device_set_realized ()
  property_set_bool ()
  object_property_set ()
  object_property_set_qobject ()
  object_property_set_bool ()
  virtio_pci_realize ()
  pci_qdev_realize ()
  device_set_realized ()
  property_set_bool ()
  object_property_set ()
  object_property_set_qobject ()
  object_property_set_bool ()
  qdev_device_add_from_qdict ()
  qmp_x_exit_preconfig ()
  qemu_init ()
  main ()
```
```C
static void check_report_connect_error(Chardev *chr,
                                       Error *err)
{
    SocketChardev *s = SOCKET_CHARDEV(chr);
    if (!s->connect_err_reported) {
        error_reportf_err(err,
                          "Unable to connect character device %s: ",
                          chr->label);
        s->connect_err_reported = true;
    } else {
        error_free(err);
    }
    qemu_chr_socket_restart_timer(chr);
}
static void qemu_chr_socket_connected(QIOTask *task, void *opaque)
{
    QIOChannelSocket *sioc = QIO_CHANNEL_SOCKET(qio_task_get_source(task));
    Chardev *chr = CHARDEV(opaque);
    SocketChardev *s = SOCKET_CHARDEV(chr);
    Error *err = NULL;
    s->connect_task = NULL;
    if (qio_task_propagate_error(task, &err)) {
        tcp_chr_change_state(s, TCP_CHARDEV_STATE_DISCONNECTED);
        if (s->registered_yank) {
            yank_unregister_function(CHARDEV_YANK_INSTANCE(chr->label),
                                     char_socket_yank_iochannel,
                                     QIO_CHANNEL(sioc));
        }
        check_report_connect_error(chr, err);
        goto cleanup;
    }
    s->connect_err_reported = false;
    tcp_chr_new_client(chr, sioc);
cleanup:
    object_unref(OBJECT(sioc));
}
```
另外在socket 断链之后也会进行超时重连
 ![Alternative Text](/img/blobs/1749463145454.png)
socket断链之后发起超时重连
![Alternative Text](/img/blobs/1749463151352.png) 
超时发起重新连接，如果检查到连接出错，会重新发起超时重连
 ![Alternative Text](/img/blobs/1749463161029.png)