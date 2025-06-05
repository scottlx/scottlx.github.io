---
title: "n8n 1panel部署"
date: 2025-06-05T15:14:00+08:00
draft: false
tags: ["CICD", "大模型", "AI"]
tags_weight: 66
series: ["区块链系列"]
series_weight: 96
categories: ["操作指南"]
categoryes_weight: 96
---

vps搭建n8n

<!-- more -->

### VPS准备

尽量选用国外节点的vps小厂，价格较低，且能直连google等服务：cloudcone, vultr

> 注意
>
> 避雷aws，aws的网络特别复杂，公网ip的域名解析需要将godaddy的dns服务器转移到它自己的route 53产品的dns服务器才可以(收费)，而且1panel不支持aws的dns账户，就不能做到证书自动续签
>
> [[Feature\] Amazon AWS Route53 DNS support · 1Panel-dev/1Panel · Discussion #8367](https://github.com/1Panel-dev/1Panel/discussions/8367)





### 域名指向服务器

域名商控制面板新增A记录，目的选择vps的公网ip

当然，最好多申请几个子域名，下面会用到（也是A记录）



### 1panel安装

比起纯手动，优势是组件都模块化了，省去了很多繁琐的配置

[在线安装 - 1Panel 文档](https://1panel.cn/docs/installation/online_installation/)

> 注意
>
> 使用公网ip直接访问面板，域名用来给真正反代的网站



### 创建容器

可以按照文档直接手动pull image

[Docker | n8n Docs](https://docs.n8n.io/hosting/installation/docker/)

也可以用1panel页面来拉![n8n容器下载](/img/blobs/n8n容器下载.PNG)

**网络**

使用1panel网络

**环境变量**

```shell
N8N_HOST=你的域名
WEBHOOK_URL=https://你的域名
GENERIC_TIMEZONE=Asia/Shanghai
N8N_PROXY_HOPS=1
N8N_RUNNERS_ENABLED=true
N8N_DISABLED_MODULES=insights
```

**端口暴露**

- 5678， tcp
- 5678，udp

**目录挂载**

- 本机目录：`/opt/1panel/apps/n8n` 权限：读写 容器目录：`/home/node/.n8n `
- 本机目录：`/opt/1panel/apps/n8ndata` 权限：读写 容器目录：`/home/node/n8ndata`

> 注意
>
> 这两个目录需要`chown 1000:1000 /opt/1panel/apps/n8n`，否则容器内没有权限读取



### 配置网站

#### 安装openresty

第一次配置网站的时候，会默认装openresty。使用默认配置就好，网络用主机网络

#### 配置反代

使用子域名创建反向代理类型的网站，端口80，代理地址填 http://127.0.0.1:5678/

#### 申请证书（动态域名解析）

创建acme账户和dns账户

dns账户需要用到提供ddns服务的厂商，比如godaddy

这里需要从 https://developer.godaddy.com/keys 申请一个API KEY。ote表示测试用，Production表示正式生产环境使用。这里亲测必须用Production，ote会在申请证书的时候会报错。

进入 1panel 的 `网站>证书>创建证书`，使用dns账号申请，其他域名里加上申请的子域名，点上自动续签（每三个月证书到期）

然后，回到 `网站>网站>n8n网站`，点击右边的配置，进入网站配置界面，再点击 HTTPS 选项卡。

打开 HTTPS，选择 `访问 HTTP 自动跳转到 HTTPS` ，选择刚才申请的证书，其他的部分不用修改，点保存。

这个时候就可以用域名打开网站了

> 注意
>
> 若打不开网站，检查一下是不是防火墙没放开80和443端口

### 初始化n8n

配置账号密码，然后过一个调查问卷就可以用了

要解锁全部社区版本的功能还需要用邮箱验证一下，注册一个免费的license



### 接入oneapi

n8n不支持国产的大模型厂商的api，所以需要一个转换，把国产api的接口转成标准openai的格式

这里演示接入火山方舟的deepseek-r1

#### 安装oneapi

1panel应用商店搜索oneapi，直接安装

之后和部署n8n一样，配置oneapi的反代，需要用一个新的子域名，这里不再赘述



#### 接入字节跳动豆包

字节火山引擎的模型和推理点申请这里不赘述



选择`渠道>添加新的渠道`

类型选择 `字节跳动豆包`，名称随便填

字节将模型抽象成了推理点，所以这边要做一个模型到推理点的映射，需要手动去 [模型推理页面](https://console.volcengine.com/ark/region:ark+cn-beijing/endpoint) 创建推理接入点，以接入点名称作为模型名称，例如：`ep-20240608051426-tkxvl`。

模型手动填入”deepseek-r1“

最后填上密钥

![接入推理点](/img/blobs/接入推理点.PNG)



#### 创建令牌

该令牌是给n8n用的，作为内部的一个token，可以设置额度

选择`令牌>添加新的令牌`，创建完成之后复制令牌



#### n8n中使用oneapi

创建测试工作流，model选择openAI Chat Model

![ds_test](/img/blobs/ds_test.PNG)

之后配置openAI的API KEY和Base URL

API KEY使用前面oneapi创建的key

Base URL填自己部署的openapi的域名，后面加上v1，例如 https://你的子域名/v1

然后就可以测试chat了

